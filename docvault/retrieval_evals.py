"""Retrieval-only eval over long public documents: embeddings only, no chat model.

  uv run python -m docvault.retrieval_evals --docs DIR [--cases FILE ...] [--out FILE] [--verify]

evals/retrieval/documents.yaml lists the documents and the frozen question files with their sha256s, and a file
whose bytes differ is refused. A run indexes the documents the questions need in its own database,
<DATABASE_URL name>_retrieval, dropped and recreated each run, through documents.upload and documents.run_index
(never insights). It embeds every question in one call and runs chat.search over each question's scope, as
chat.ask does for a first turn. For each question it records:

  hit@k         chat.select_sources at k = 4, 8 and 16 holds a passage of the expected document on an expected page
  fused rank    where the first such passage is among the retrieved candidates by (rrf, similarity), up to 50
  vector rank   the same by similarity alone
  gate          chat.Found.matched: chat.ask would answer from the sources rather than refuse; hits ignore it

MRR@50 averages 1/rank, a miss counting 0. CHUNK_MAX_CHARS and CHUNK_OVERLAP_CHARS set the chunking, so a sweep is
several runs with different env. --out writes the Markdown report and, beside it, the per-question JSON that two
runs are diffed by. --verify instead checks each quoted question against the page text pages.extract reads, with
no database and no API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence, Set
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import yaml
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from . import ai, chat, db, documents, metrics, pages
from .ai import TurnOwner
from .config import Settings, load_settings
from .context import BlobStore, Ctx
from .domain import DocumentId, PageMap, Purpose, Source, TurnId, VaultError
from .evals import Ratio, _commit

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "evals" / "retrieval" / "documents.yaml"
KS = (4, 8, 16)
RANKED = chat.CANDIDATES    # MRR@50: a passage ranked past this counts as not found


# ------------------------------------------------------------------ the manifest and the case files


@dataclass(frozen=True, slots=True)
class Document:
    file: str
    sha256: str
    pages: int
    url: str


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    ask: str
    scope: tuple[str, ...]      # the chat's documents, by file name
    doc: str                    # the one holding the expected pages
    pages: frozenset[int]
    quote: str | None = None
    values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaseFile:
    name: str
    sha256: str
    questions: tuple[Question, ...]


def load(manifest: Path = MANIFEST, chosen: Sequence[Path] = ()) -> tuple[dict[str, Document], tuple[CaseFile, ...]]:
    """The manifest's documents, and its case files or the chosen ones, each checked against its frozen sha256.
    Raises ValueError listing every problem."""
    spec = yaml.safe_load(manifest.read_text())
    listed = {d["file"]: Document(d["file"], d["sha256"], d["pages"], d["url"]) for d in spec["documents"]}
    entries = {(manifest.parent / entry["file"]).resolve(): entry for entry in spec["cases"]}
    if unknown := [str(path) for path in chosen if path.resolve() not in entries]:
        raise ValueError(f"{', '.join(unknown)} is not listed in {manifest}")
    problems: list[str] = []
    case_files: list[CaseFile] = []
    for path in dict.fromkeys(path.resolve() for path in chosen) or entries:
        entry, raw_bytes = entries[path], path.read_bytes()
        if (sha := hashlib.sha256(raw_bytes).hexdigest()) != entry["sha256"]:
            problems.append(f"{entry['file']}: sha256 {sha} is not the frozen {entry['sha256']}")
            continue
        questions = tuple(_question(raw, entry.get("docs"), f"{entry['file']} {raw['id']}", listed, problems)
                          for raw in yaml.safe_load(raw_bytes)["questions"])
        if len({question.id for question in questions}) < len(questions):
            problems.append(f"{entry['file']}: question ids repeat")
        case_files.append(CaseFile(entry["file"], sha, questions))
    if problems:
        raise ValueError("; ".join(problems))
    return listed, tuple(case_files)


def _question(raw: Mapping[str, Any], default: Sequence[str] | None, label: str, listed: Mapping[str, Document],
              problems: list[str]) -> Question:
    scope = tuple(raw.get("docs") or default or ())
    if not scope:
        problems.append(f"{label}: no docs, and the manifest gives the case file none")
    problems += [f"{label}: {name} is not in the manifest" for name in scope if name not in listed]
    doc = raw.get("doc") or (scope[0] if len(scope) == 1 else None)
    if doc is None and len(scope) > 1:
        problems.append(f"{label}: docs holds {len(scope)} documents, so doc must name the one with the expected pages")
    elif doc is not None and doc not in scope:
        problems.append(f"{label}: doc {doc} is not in docs")
    wanted = raw.get("pages")
    if not wanted or not isinstance(wanted, list):
        problems.append(f"{label}: no expected pages")
        wanted = []
    elif doc in listed and not all(type(page) is int and 1 <= page <= listed[doc].pages for page in wanted):
        problems.append(f"{label}: pages {wanted} are not all page numbers of {doc} (1 to {listed[doc].pages})")
    return Question(str(raw["id"]), raw["ask"], scope, doc or "", frozenset(wanted), raw.get("quote"),
                    tuple(str(value) for value in raw.get("values") or ()))


def check_documents(folder: Path, wanted: Iterable[Document]) -> dict[str, Path]:
    """Each document's path in folder. Raises ValueError naming every one that is missing or has other bytes."""
    paths: dict[str, Path] = {}
    problems: list[str] = []
    for document in wanted:
        path = folder / document.file
        if not path.is_file():
            problems.append(f"{document.file}: not in {folder}")
        elif (sha := _sha256(path)) != document.sha256:
            problems.append(f"{document.file}: sha256 {sha} in {folder}, the manifest says {document.sha256}")
        else:
            paths[document.file] = path
    if problems:
        raise ValueError("; ".join(problems))
    return paths


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


# ------------------------------------------------------------------ scoring


@dataclass(frozen=True, slots=True)
class Scored:
    hits: tuple[bool, ...]          # one per KS
    fused_rank: int | None
    vector_rank: int | None
    top: tuple[tuple[str, tuple[int, ...]], ...]    # the first three candidates by fused rank: file name, pages
    gate: bool                      # chat.ask would answer from the sources; otherwise it sends only the openings


def score(found: chat.Found, doc: DocumentId, wanted: Set[int], *, per_doc_min: int, min_similarity: float) -> Scored:
    """Pure. A passage counts when it is from doc and any page it holds is wanted. hits ignore the relevance gate,
    so a question chat.ask would refuse can still hit; gate says whether it would."""
    def expected(passage: chat.Candidate | Source) -> bool:
        return passage.doc.id == doc and not wanted.isdisjoint(_pages(passage.page_map))

    hits = tuple(any(map(expected, chat.select_sources(
        found.retrieved, k=k, per_doc_min=per_doc_min, on_pages=found.on_pages, in_sections=found.in_sections,
        openings=found.openings))) for k in KS)
    fused = sorted(found.retrieved, key=lambda candidate: (candidate.rrf, candidate.similarity), reverse=True)
    by_similarity = sorted(found.retrieved, key=lambda candidate: candidate.similarity, reverse=True)
    return Scored(hits, _rank(fused, expected), _rank(by_similarity, expected),
                  tuple((candidate.doc.filename, _pages(candidate.page_map)) for candidate in fused[:3]),
                  found.matched(min_similarity))


def _pages(page_map: PageMap) -> tuple[int, ...]:
    return tuple(page for _, page in page_map.starts)


def _rank(ranked: Sequence[chat.Candidate], expected: Callable[[chat.Candidate], bool]) -> int | None:
    return next((rank for rank, candidate in enumerate(ranked[:RANKED], start=1) if expected(candidate)), None)


def mrr(ranks: Sequence[int | None]) -> float:
    return sum(1 / rank for rank in ranks if rank) / len(ranks) if ranks else 0.0


@dataclass(frozen=True, slots=True)
class Summary:
    n: int
    hits: tuple[Ratio, ...]         # one per KS
    mrr_fused: float
    mrr_vector: float
    gate: Ratio


def summarise(scored: Sequence[Scored]) -> Summary:
    return Summary(len(scored), tuple(Ratio(sum(s.hits[at] for s in scored), len(scored)) for at in range(len(KS))),
                   mrr([s.fused_rank for s in scored]), mrr([s.vector_rank for s in scored]),
                   Ratio(sum(s.gate for s in scored), len(scored)))


# ------------------------------------------------------------------ the run


@dataclass(frozen=True, slots=True)
class Result:
    case_file: str
    question: Question
    scored: Scored


@dataclass(frozen=True, slots=True)
class Indexed:
    file: str
    pages: int
    chunks: int
    parse_s: float
    embed_s: float


@dataclass(frozen=True, slots=True)
class Run:
    commit: str                 # read when the run starts, so a commit or a written report cannot change it
    settings: Settings
    embed_provider: str
    embed_model: str
    min_similarity: float
    case_files: tuple[CaseFile, ...]
    indexed: tuple[Indexed, ...]
    results: tuple[Result, ...]
    spend: tuple[metrics.AIUsage, ...]
    wall_s: float


@dataclass
class _IndexOnly:
    """Jobs without Celery that keep the index jobs and drop analyze, so no chat model is called."""

    pending: list[DocumentId] = field(default_factory=list)

    def index(self, doc_id: DocumentId) -> None:
        self.pending.append(doc_id)

    def analyze(self, doc_id: DocumentId) -> None:
        pass


@contextmanager
def _fresh_database(base_url: str) -> Iterator[db.Db]:
    """<name>_retrieval on base_url's server: dropped, recreated, migrated and pooled, as in evals."""
    name = f"{conninfo_to_dict(base_url)['dbname']}_retrieval"
    with psycopg.connect(make_conninfo(base_url, dbname="postgres"), autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_conninfo(base_url, dbname=name)
    db.migrate(url)
    pool = db.open_pool(url, max_size=4)
    try:
        yield pool
    finally:
        pool.close()


INDEXED_SQL = """
SELECT d.filename, d.status, d.error_code, d.page_count, d.chunk_count,
       extract(epoch FROM sum(s.finished_at - s.started_at) FILTER (WHERE s.step = 'parse')) AS parse_s,
       extract(epoch FROM sum(s.finished_at - s.started_at) FILTER (WHERE s.step = 'embed')) AS embed_s
FROM documents d LEFT JOIN step_runs s ON s.document_id = d.id AND s.outcome = 'ok'
GROUP BY d.id ORDER BY d.filename
"""


def run(settings: Settings, paths: Mapping[str, Path], case_files: Sequence[CaseFile]) -> Run:
    """Index the documents in paths, search every question over its scope, and score it at each k."""
    started, commit = time.monotonic(), _commit()
    asked = [(case_file.name, question) for case_file in case_files for question in case_file.questions]
    with _fresh_database(settings.database_url) as pool, tempfile.TemporaryDirectory() as blobs:
        settings = settings.model_copy(update={"storage_dir": Path(blobs)})
        jobs = _IndexOnly()
        ctx = Ctx(settings, pool, ai.build(settings, pool), BlobStore(settings.storage_dir), jobs)
        print(f"Embeddings: {ctx.ai.embedder.provider} {ctx.ai.embed_model}", file=sys.stderr)
        ids: dict[str, DocumentId] = {}
        for name, path in paths.items():
            with path.open("rb") as stream:
                ids[name] = documents.upload(ctx, stream, name).document.id
        for doc_id in jobs.pending:
            _index(ctx, doc_id)
        with pool.connection() as conn:
            rows = conn.execute(INDEXED_SQL).fetchall()
        if failed := [f"{row['filename']} is {row['status']} ({row['error_code']})" for row in rows
                      if row["status"] != "ready"]:
            raise RuntimeError("Indexing did not finish: " + "; ".join(failed))
        vectors = ctx.ai.embed([question.ask for _, question in asked], purpose=Purpose.EMBED_QUERY,
                               owner=TurnOwner(TurnId(uuid4())))
        chats = {scope: chat.start(pool, [ids[name] for name in scope]).id
                 for scope in dict.fromkeys(question.scope for _, question in asked)}
        results = []
        with pool.connection() as conn:
            scopes = {scope: chat.scope(conn, chat_id) for scope, chat_id in chats.items()}
            for (case_file, question), q_vec in zip(asked, vectors, strict=True):
                found = chat.search(conn, scopes[question.scope], q_vec, question.ask, ctx.ai.embed_model,
                                    per_doc=settings.retrieval_per_doc_min, k=settings.retrieval_k)
                results.append(Result(case_file, question, score(
                    found, ids[question.doc], question.pages, per_doc_min=settings.retrieval_per_doc_min,
                    min_similarity=ctx.ai.min_similarity)))
        spend = tuple(metrics.processing_stats(pool, timedelta(days=1)).ai.by_purpose_and_model)
    indexed = tuple(Indexed(row["filename"], row["page_count"], row["chunk_count"], float(row["parse_s"]),
                            float(row["embed_s"])) for row in rows)
    return Run(commit, settings, ctx.ai.embedder.provider, ctx.ai.embed_model, ctx.ai.min_similarity, tuple(case_files),
               indexed, tuple(results), spend, time.monotonic() - started)


def _index(ctx: Ctx, doc_id: DocumentId) -> None:
    """run_index with the Celery task's retries: one rate-limited embedding batch should not end a long run.
    run_index raises only a retryable error, and only while attempts remain; the last attempt writes FAILED."""
    for attempt in range(1, documents.MAX_RETRIES + 2):
        try:
            documents.run_index(ctx, doc_id, attempt=attempt)
            return
        except VaultError:
            time.sleep(2 ** attempt)


# ------------------------------------------------------------------ --verify


def verify(paths: Mapping[str, Path], listed: Mapping[str, Document], case_files: Sequence[CaseFile], *,
           max_pages: int) -> list[str]:
    """Every problem found: a document read with another page count than the manifest's, a quote on none of its
    question's expected pages, or a value on none of them. Reads the documents with pages.extract only."""
    texts: dict[str, dict[int, str]] = {}
    failures: list[str] = []
    for case_file in case_files:
        for question in case_file.questions:
            if question.quote is None:
                continue
            if question.doc not in texts:
                path = paths[question.doc]
                extracted = pages.extract(pages.sniff_kind(path), path, max_pages=max_pages)
                texts[question.doc] = {page.number: page.text for page in extracted.pages}
                if len(extracted.pages) != listed[question.doc].pages:
                    failures.append(f"{question.doc}: {len(extracted.pages)} pages, "
                                    f"the manifest says {listed[question.doc].pages}")
            label, wanted = f"{case_file.name} {question.id}", sorted(question.pages)
            on = [texts[question.doc].get(number, "") for number in wanted]
            if not any(pages.locate_quote(question.quote, text) for text in on):
                failures.append(f"{label}: quote not on pages {wanted}")
            failures += [f"{label}: {value} not on pages {wanted}" for value in question.values
                         if not any(pages.locate_quote(value, text) for text in on)]
    return failures


# ------------------------------------------------------------------ output


def render(result: Run, command: str) -> str:
    settings = result.settings
    by_file = [(case_file.name, [r.scored for r in result.results if r.case_file == case_file.name])
               for case_file in result.case_files]
    misses = [r for r in result.results if not r.scored.hits[-1]]
    lines = [
        f"Run {datetime.now(UTC).date().isoformat()} at commit {result.commit}: `{command}`",
        "",
        f"Embeddings {result.embed_provider} {result.embed_model}; chunk_max_chars {settings.chunk_max_chars}, "
        f"chunk_overlap_chars {settings.chunk_overlap_chars}, retrieval_k {settings.retrieval_k}, "
        f"retrieval_per_doc_min {settings.retrieval_per_doc_min}, CANDIDATES {chat.CANDIDATES}, gate min_similarity "
        f"{result.min_similarity}. Wall time {result.wall_s:.0f} s.",
        *(["", "**The fake embedder ran: these numbers say nothing about retrieval quality.**"]
          if result.embed_provider == "fake" else []),
        "",
        "| Case file | sha256 | Questions |",
        "|---|---|---|",
        *(f"| {case_file.name} | {case_file.sha256} | {len(case_file.questions)} |" for case_file in result.case_files),
        "",
        "| Document | Pages | Chunks | Parse s | Embed s |",
        "|---|---|---|---|---|",
        *(f"| {d.file} | {d.pages} | {d.chunks} | {d.parse_s:.1f} | {d.embed_s:.1f} |" for d in result.indexed),
        "",
        "| Purpose | Model | Calls | Input tokens | Cost (USD) |",
        "|---|---|---|---|---|",
        *(f"| {u.purpose} | {u.model} | {u.calls} | {u.input_tokens} | {u.cost_usd}"
          f"{f' ({u.unpriced_calls} unpriced)' if u.unpriced_calls else ''} |" for u in result.spend),
        "",
        "| Case file | n | " + " | ".join(f"hit@{k}" for k in KS)
        + f" | MRR@{RANKED} fused | MRR@{RANKED} vector only | Gate passed |",
        "|---|---|" + "---|" * len(KS) + "---|---|---|",
        *(_summary_line(name, summarise(scored)) for name, scored in
          [*by_file, ("All", [r.scored for r in result.results])]),
        "",
    ]
    if not misses:
        return "\n".join([*lines, f"No misses at {KS[-1]}."])
    return "\n".join([
        *lines,
        f"Misses at {KS[-1]}:",
        "",
        "| Case file | Question | Expected pages | Top 3 by fused rank | Fused rank | Vector rank |",
        "|---|---|---|---|---|---|",
        *(f"| {r.case_file} | {r.question.id} | {', '.join(map(str, sorted(r.question.pages)))} | "
          f"{'; '.join(_where(name, pages_held, r.question) for name, pages_held in r.scored.top)} | "
          f"{r.scored.fused_rank or '-'} | {r.scored.vector_rank or '-'} |" for r in misses),
    ])


def _summary_line(name: str, summary: Summary) -> str:
    return (f"| {name} | {summary.n} | " + " | ".join(map(str, summary.hits))
            + f" | {summary.mrr_fused:.3f} | {summary.mrr_vector:.3f} | {summary.gate} |")


def _where(name: str, pages_held: tuple[int, ...], question: Question) -> str:
    span = f"p. {pages_held[0]}" + (f"-{pages_held[-1]}" if len(pages_held) > 1 else "")
    return span if len(question.scope) == 1 else f"{name} {span}"


def as_json(result: Run, command: str) -> dict[str, Any]:
    settings = result.settings
    return {
        "command": command,
        "commit": result.commit,
        "embed_model": f"{result.embed_provider} {result.embed_model}",
        "min_similarity": result.min_similarity,
        "settings": {"chunk_max_chars": settings.chunk_max_chars, "chunk_overlap_chars": settings.chunk_overlap_chars,
                     "retrieval_k": settings.retrieval_k, "retrieval_per_doc_min": settings.retrieval_per_doc_min,
                     "candidates": chat.CANDIDATES},
        "case_files": {case_file.name: case_file.sha256 for case_file in result.case_files},
        "questions": [{"case_file": r.case_file, "id": r.question.id, "doc": r.question.doc,
                       "pages": sorted(r.question.pages),
                       **{f"hit@{k}": hit for k, hit in zip(KS, r.scored.hits, strict=True)},
                       "fused_rank": r.scored.fused_rank, "vector_rank": r.scored.vector_rank, "gate": r.scored.gate,
                       "top3": [{"doc": name, "pages": list(held)} for name, held in r.scored.top]}
                      for r in result.results],
    }


def write(result: Run, command: str, path: Path) -> None:
    """The Markdown report at path, and the per-question JSON beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(result, command) + "\n")
    path.with_suffix(".json").write_text(json.dumps(as_json(result, command), indent=1) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(prog="python -m docvault.retrieval_evals", description=__doc__.split("\n\n")[0])
    parser.add_argument("--docs", type=Path, required=True, help="the folder holding the manifest's documents")
    parser.add_argument("--cases", type=Path, nargs="+", default=[],
                        help=f"case files listed in {MANIFEST.relative_to(ROOT)}; default: all of them")
    parser.add_argument("--out", type=Path, help="write the Markdown report here and the per-question JSON beside it")
    parser.add_argument("--verify", action="store_true",
                        help="check each quoted question against the extracted page text; no database, no API")
    options = parser.parse_args(args)
    if options.out and options.out.suffix == ".json":
        parser.error("--out names the Markdown report; the JSON is written beside it")
    try:
        listed, case_files = load(MANIFEST, options.cases)
        needed = {name for case_file in case_files for question in case_file.questions for name in question.scope}
        paths = check_documents(options.docs, [document for name, document in listed.items() if name in needed])
    except ValueError as exc:
        parser.error(str(exc))
    settings = load_settings()
    if options.verify:
        failures = verify(paths, listed, case_files, max_pages=settings.max_pages)
        quoted = sum(question.quote is not None for case_file in case_files for question in case_file.questions)
        print("\n".join([*failures, f"{quoted} quoted questions checked: {len(failures)} problems."]))
        return 1 if failures else 0
    command = shlex.join(["uv", "run", "python", "-m", "docvault.retrieval_evals", *args])
    result = run(settings, paths, case_files)
    print(render(result, command))
    if options.out:
        write(result, command, options.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
