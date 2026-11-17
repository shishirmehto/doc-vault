"""Retrieval, answer, insights, comparison and summary evals over the sample documents.

  uv run python -m docvault.evals [--calibrate] [--out FILE]

Runs the fixed case set in evals/cases.yaml against the configured provider (the fake when no key is set)
in its own database, <DATABASE_URL name>_evals, dropped and recreated each run. It uploads the samples through
documents.upload, runs run_index and run_analyze in-process (no Celery, no Redis), asks every case
through chat.start and chat.ask, then compares the policy with its renewal quote through compare.create and
summarises each sample at each length through summaries.create. Each number is scored apart, so it shows where a
miss happens:

  retrieval hit rate   the expected document and page are among the sources given to the answer step
  citation accuracy    a citation of the expected document has the expected page and quotes the fact
  not-found accuracy   an out-of-scope question comes back not_found
  key-fact grounding   insights facts kept / (kept + dropped), and the planted facts they recall
  suggestions answered every suggestion under a case's last turn, asked in that chat after the case, comes back
                       answered (a click on a suggestion should never end in a refusal)
  cell grounding       comparison cells kept / (kept + dropped)
  difference recall    the changes planted between the policy and the quote (samples/facts.yaml) come back as a
                       differing row whose two cells sit on the fact's page, quote it, and hold a value from it
  equal fields         the fields planted unchanged come back the same way as a row that does not differ
  summary grounding    summary sources kept / (kept + dropped) per length, over every sample summarised with no
                       focus in the plain tone
  cost and latency     metrics.processing_stats over the run: llm_calls by purpose and model, chat_turns p95

--calibrate prints each turn's best vector similarity and all-terms match instead, then the relevance-gate
threshold that maximises answerable-passed plus out-of-scope-refused. Results print to stdout, and --out
writes them between the results markers of a Markdown file.
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import shlex
import subprocess
import sys
import tempfile
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

import psycopg
import yaml
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel

from . import ai, chat, compare, db, documents, insights, metrics, summaries
from .ai import ChatBackend, EmbedBackend, RawCompletion, Tokens, Vector
from .config import Settings, load_settings
from .context import BlobStore, Ctx
from .domain import (
    Cell,
    Citation,
    DocumentId,
    InsightsReady,
    Row,
    Source,
    SummaryLength,
    SummaryOptions,
    SummaryTone,
    TurnOutcome,
)
from .pages import collapse_whitespace, locate_quote
from .prompts import Prompt

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "cases.yaml"
SAMPLES = ROOT / "samples"
START, END = "<!-- results:start -->", "<!-- results:end -->"


# ------------------------------------------------------------------ the case set


@dataclass(frozen=True, slots=True)
class Fact:
    id: str
    document: str       # sample filename
    page: int
    text: str


@dataclass(frozen=True, slots=True)
class Question:
    ask: str
    expected: tuple[Fact, ...]      # empty: the documents in scope do not answer it


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    scope: tuple[str, ...]          # sample filenames
    questions: tuple[Question, ...]


@dataclass(frozen=True, slots=True)
class ExpectedField:
    field: str
    policy: Fact
    quote: Fact
    differs: bool


@dataclass(frozen=True, slots=True)
class ComparisonCase:
    documents: tuple[str, str]
    fields: tuple[ExpectedField, ...]


@dataclass(frozen=True, slots=True)
class CaseSet:
    documents: tuple[str, ...]      # every sample to upload
    planted: tuple[Fact, ...]       # facts.yaml without the boilerplate repeated on every page
    cases: tuple[Case, ...]
    comparison: ComparisonCase


def load_cases(path: Path = CASES) -> CaseSet:
    """Parse and check the case set against samples/facts.yaml. Raises ValueError listing every problem."""
    truth = yaml.safe_load((SAMPLES / "facts.yaml").read_text())
    spec = yaml.safe_load(path.read_text())
    facts = {f["id"]: Fact(f["id"], f["document"], f["page"], f["text"]) for f in truth["facts"]}
    aliases: dict[str, str] = spec["documents"]
    problems: list[str] = []
    cases: list[Case] = []
    for raw in spec["cases"]:
        problems += [f"{raw['id']}: unknown document {alias!r}" for alias in raw["scope"] if alias not in aliases]
        scope = tuple(aliases[alias] for alias in raw["scope"] if alias in aliases)
        questions = []
        for turn in raw["turns"]:
            expect = () if turn["expect"] == "not_found" else tuple(turn["expect"])
            problems += [f"{raw['id']}: unknown fact {fact_id!r}" for fact_id in expect if fact_id not in facts]
            expected = tuple(facts[fact_id] for fact_id in expect if fact_id in facts)
            problems += [f"{raw['id']}: {fact.id} is in {fact.document}, outside the chat's scope"
                         for fact in expected if fact.document not in scope]
            questions.append(Question(turn["ask"], expected))
        cases.append(Case(raw["id"], scope, tuple(questions)))
    asked = {fact.id for case in cases for question in case.questions for fact in question.expected}
    problems += [f"{fact_id} is neither asked nor listed in not_asked"
                 for fact_id in facts.keys() - asked - spec["not_asked"].keys()]
    out_of_scope = [question.ask.lower() for case in cases for question in case.questions if not question.expected]
    problems += [f"no out-of-scope question asks about {topic!r}" for topic in truth["absent_topics"]
                 if not any(topic in ask for ask in out_of_scope)]
    if len({case.id for case in cases}) < len(cases):
        problems.append("case ids repeat")
    fields: list[ExpectedField] = []
    for key, differs in (("changes", True), ("unchanged", False)):
        for item in truth[key]:
            unknown = [fact_id for fact_id in (item["policy"], item["quote"]) if fact_id not in facts]
            problems += [f"facts.yaml {key} {item['field']!r}: unknown fact {fact_id!r}" for fact_id in unknown]
            if not unknown:
                fields.append(ExpectedField(item["field"], facts[item["policy"]], facts[item["quote"]], differs))
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    planted = tuple(facts[f["id"]] for f in truth["facts"] if "repeated_on" not in f)
    comparison = ComparisonCase((fields[0].policy.document, fields[0].quote.document), tuple(fields))
    return CaseSet(tuple(aliases.values()), planted, tuple(cases), comparison)


# ------------------------------------------------------------------ scoring


class Miss(StrEnum):
    """Why an expected fact was not among the sources the answer step saw."""

    GATE = "gate"       # the relevance gate refused, so there was no answer step
    TEXT = "text"       # no source of the expected document holds the fact's text
    PAGE = "page"       # a source holds the text, but on another page


@dataclass(frozen=True, slots=True)
class Scored:
    fact: Fact
    retrieval: Miss | None      # None: retrieved
    cited: bool


@dataclass(frozen=True, slots=True)
class TurnResult:
    case: str
    question: str
    standalone: str
    outcome: TurnOutcome
    scored: tuple[Scored, ...]  # one per expected fact; empty for an out-of-scope question
    candidates: tuple[chat.Candidate, ...]  # what the gate saw

    @property
    def answerable(self) -> bool:
        return bool(self.scored)

    @property
    def best_similarity(self) -> float:
        return max(c.similarity for c in self.candidates)

    @property
    def lexical_hit(self) -> bool:
        return any(c.lexical_hit for c in self.candidates)

    def passes(self, threshold: float) -> bool:
        return chat.gate(self.candidates, threshold)


def retrieval_miss(fact: Fact, sources: Sequence[Source] | None) -> Miss | None:
    if sources is None:
        return Miss.GATE
    pages = [source.page_map.page_at(span[0]) for source in sources
             if source.doc.filename == fact.document and (span := locate_quote(fact.text, source.text))]
    return None if fact.page in pages else Miss.PAGE if pages else Miss.TEXT


def is_cited(fact: Fact, citations: Sequence[Citation]) -> bool:
    text = collapse_whitespace(fact.text)
    return any(c.filename == fact.document and c.page == fact.page and text in collapse_whitespace(c.quote)
               for c in citations)


class Ratio(NamedTuple):
    hits: int
    total: int

    def __str__(self) -> str:
        return f"{self.hits}/{self.total} ({self.hits / self.total:.0%})" if self.total else "n/a"


def _ratio(flags: Iterable[bool]) -> Ratio:
    flags = list(flags)
    return Ratio(sum(flags), len(flags))


@dataclass(frozen=True, slots=True)
class FieldResult:
    expected: ExpectedField
    row: Row | None

    @property
    def found(self) -> bool:
        return self.row is not None and self.row.differs == self.expected.differs


def match_field(expected: ExpectedField, rows: Sequence[Row]) -> FieldResult:
    return FieldResult(expected, next((row for row in rows if _holds(row.cells[0], expected.policy)
                                       and _holds(row.cells[1], expected.quote)), None))


def _holds(cell: Cell | None, fact: Fact) -> bool:
    text = collapse_whitespace(fact.text)
    return (cell is not None and cell.page == fact.page and text in cell.quote
            and collapse_whitespace(cell.value).casefold() in text.casefold())


@dataclass(frozen=True, slots=True)
class Suggested:
    """One suggestion asked as a question. Kept out of Report.turns: it has no expected facts, so it would count
    as out of scope there and move the gate fit."""

    case: str
    question: str
    outcome: TurnOutcome


@dataclass(frozen=True, slots=True)
class Report:
    setup: str
    threshold: float            # the gate threshold the run used
    turns: tuple[TurnResult, ...]
    suggested: tuple[Suggested, ...]
    grounding: Ratio            # insights key facts kept / (kept + dropped)
    recall: Ratio               # planted facts found in a kept key fact's quote, same document and page
    cell_grounding: Ratio
    fields: tuple[FieldResult, ...]
    summary_grounding: Mapping[SummaryLength, Ratio]
    spend: tuple[metrics.AIUsage, ...]
    turn_p95_ms: int | None

    @property
    def retrieval(self) -> Ratio:
        return _ratio(s.retrieval is None for t in self.turns for s in t.scored)

    @property
    def citation(self) -> Ratio:
        return _ratio(s.cited for t in self.turns for s in t.scored)

    @property
    def not_found(self) -> Ratio:
        return _ratio(t.outcome is not TurnOutcome.ANSWERED for t in self.turns if not t.answerable)

    @property
    def suggestions(self) -> Ratio:
        return _ratio(s.outcome is TurnOutcome.ANSWERED for s in self.suggested)

    @property
    def difference_recall(self) -> Ratio:
        return _ratio(result.found for result in self.fields if result.expected.differs)

    @property
    def equal_kept(self) -> Ratio:
        return _ratio(result.found for result in self.fields if not result.expected.differs)


# ------------------------------------------------------------------ the gate fit


@dataclass(frozen=True, slots=True)
class GateFit:
    threshold: float
    correct: int                    # answerable passed + out-of-scope refused, at threshold
    total: int
    lowest_passed: float | None     # the nearest similarity at or above threshold
    highest_refused: float | None   # the nearest similarity below it


def fit_gate(turns: Sequence[TurnResult]) -> GateFit:
    """The similarity threshold that maximises answerable-passed plus out-of-scope-refused.

    A turn with an all-terms match passes at any threshold, so only the others place the cut. The cut goes
    in the middle of the best-scoring gap between neighbouring similarities; a tie goes to the wider gap.
    """
    similar = [t for t in turns if not t.lexical_hit]
    cuts = sorted({t.best_similarity for t in similar})
    # (threshold, width of its gap): the middle of every gap, then the two ends, which lose every tie.
    options = [(round((lo + hi) / 2, 3), hi - lo) for lo, hi in itertools.pairwise(cuts)]
    options += [(cuts[0], -1.0), (1.0, -1.0)] if cuts else [(1.0, -1.0)]

    def correct(threshold: float) -> int:
        return sum(t.passes(threshold) == t.answerable for t in turns)

    threshold = max(options, key=lambda option: (correct(option[0]), option[1]))[0]
    return GateFit(threshold, correct(threshold), len(turns),
                   min((t.best_similarity for t in similar if t.passes(threshold)), default=None),
                   max((t.best_similarity for t in similar if not t.passes(threshold)), default=None))


# ------------------------------------------------------------------ the run


@dataclass
class _Queue:
    """Jobs without Celery: the pipeline functions run in-process, in the order the pipeline enqueues them."""

    pending: deque[tuple[Callable[[Ctx, DocumentId], None], DocumentId]] = field(default_factory=deque)

    def index(self, doc_id: DocumentId) -> None:
        self.pending.append((documents.run_index, doc_id))

    def analyze(self, doc_id: DocumentId) -> None:
        self.pending.append((insights.run_analyze, doc_id))

    def drain(self, ctx: Ctx) -> None:
        while self.pending:
            job, doc_id = self.pending.popleft()
            job(ctx, doc_id)


class _SourceTap:
    """A ChatBackend that keeps the sources of the last answer prompt and passes every call through."""

    def __init__(self, inner: ChatBackend) -> None:
        self.inner = inner
        self.provider, self.models = inner.provider, inner.models
        self.sources: tuple[Source, ...] | None = None

    def complete_json(self, prompt: Prompt, output: type[BaseModel], *, timeout_s: float,
                      on_text: Callable[[str], None] | None = None) -> tuple[RawCompletion, Tokens]:
        if prompt.name == "answer":
            self.sources = prompt.inputs["sources"]
        return self.inner.complete_json(prompt, output, timeout_s=timeout_s, on_text=on_text)


class _QueryTap:
    """An EmbedBackend that keeps the vectors it last returned, so the eval reruns retrieval without a call."""

    def __init__(self, inner: EmbedBackend) -> None:
        self.inner = inner
        self.provider, self.model, self.dimensions = inner.provider, inner.model, inner.dimensions
        self.min_similarity = inner.min_similarity
        self.vectors: list[Vector] = []

    def embed(self, texts: Sequence[str], *, timeout_s: float) -> tuple[list[Vector], Tokens]:
        self.vectors, tokens = self.inner.embed(texts, timeout_s=timeout_s)
        return self.vectors, tokens


@contextmanager
def _fresh_database(base_url: str) -> Iterator[db.Db]:
    """<name>_evals on base_url's server: dropped, recreated, migrated and pooled. base_url's own database is
    never opened."""
    name = f"{conninfo_to_dict(base_url)['dbname']}_evals"
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


def run(settings: Settings, case_set: CaseSet) -> Report:
    """Upload, index and analyse the samples, ask every case, and score each turn."""
    with _fresh_database(settings.database_url) as pool, tempfile.TemporaryDirectory() as blobs:
        settings = settings.model_copy(update={"storage_dir": Path(blobs)})
        queue = _Queue()
        ctx = Ctx(settings, pool, ai.build(settings, pool), BlobStore(settings.storage_dir), queue)
        ids: dict[str, DocumentId] = {}
        for name in case_set.documents:
            with (SAMPLES / name).open("rb") as stream:
                ids[name] = documents.upload(ctx, stream, name).document.id
        queue.drain(ctx)

        answers, queries = _SourceTap(ctx.ai.chat), _QueryTap(ctx.ai.embedder)
        asking = dataclasses.replace(ctx, ai=dataclasses.replace(ctx.ai, chat=answers, embedder=queries))
        turns, suggested = [], []
        for case in case_set.cases:
            started = chat.start(pool, [ids[name] for name in case.scope], title=case.id)
            for question in case.questions:
                answers.sources = None
                turn = chat.ask(asking, started.id, question.ask)
                [q_vec] = queries.vectors
                with pool.connection() as conn:
                    candidates = chat.retrieve(conn, started.documents, q_vec, turn.standalone_query,
                                               ctx.ai.embed_model, per_doc=settings.retrieval_per_doc_min)
                if chat.gate(candidates, ctx.ai.min_similarity) != (answers.sources is not None):
                    raise RuntimeError(f"{case.id}: the rerun retrieval disagrees with the gate in chat.ask")
                turns.append(TurnResult(
                    case.id, question.ask, turn.standalone_query, turn.answer.outcome,
                    tuple(Scored(fact, retrieval_miss(fact, answers.sources), is_cited(fact, turn.answer.citations))
                          for fact in question.expected),
                    tuple(candidates)))
            # Asked after the case's own turns, so they change no condense call the case is scored on.
            suggested += [Suggested(case.id, ask, chat.ask(asking, started.id, ask).answer.outcome)
                          for ask in turn.suggestions]

        compared = compare.create(ctx, [ids[name] for name in case_set.comparison.documents], None)
        kept = sum(cell is not None for row in compared.rows for cell in row.cells)
        fields = tuple(match_field(expected, compared.rows) for expected in case_set.comparison.fields)
        summary_grounding = {}
        for length in SummaryLength:
            made = [summaries.create(ctx, doc_id, SummaryOptions.of(length, None, SummaryTone.PLAIN))
                    for doc_id in ids.values()]
            sources = sum(len(summary.sources) for summary in made)
            summary_grounding[length] = Ratio(sources, sources + sum(summary.dropped_sources for summary in made))

        grounding, recall = _insights(pool, ids, case_set.planted)
        stats = metrics.processing_stats(pool, timedelta(days=1))   # the database holds this run only
        chat_models = ", ".join(f"{tier} {model}" for tier, model in ctx.ai.chat.models.items())
        setup = (f"chat {ctx.ai.chat.provider} ({chat_models}); embeddings {ctx.ai.embedder.provider} "
                 f"{ctx.ai.embed_model}")
        return Report(setup, ctx.ai.min_similarity, tuple(turns), tuple(suggested), grounding, recall,
                      Ratio(kept, kept + compared.dropped_cells), fields, summary_grounding,
                      tuple(stats.ai.by_purpose_and_model), stats.chat.latency_ms.p95)


def _insights(pool: db.Db, ids: Mapping[str, DocumentId], planted: Sequence[Fact]) -> tuple[Ratio, Ratio]:
    kept: list[tuple[str, int, str]] = []
    dropped = 0
    for name, doc_id in ids.items():
        state = documents.get(pool, doc_id).insights
        if not isinstance(state, InsightsReady):
            raise RuntimeError(f"insights for {name} are not ready: {state}")
        kept += [(name, fact.page, fact.quote) for fact in state.body.key_facts]
        dropped += state.dropped_key_facts
    recall = _ratio(any((name, page) == (fact.document, fact.page) and collapse_whitespace(fact.text) in quote
                        for name, page, quote in kept) for fact in planted)
    return Ratio(len(kept), len(kept) + dropped), recall


# ------------------------------------------------------------------ output


def render(report: Report, command: str) -> str:
    turns = report.turns
    answerable = [t for t in turns if t.answerable]
    lines = [
        f"Run {datetime.now(UTC).date().isoformat()} at commit {_commit()}: `{command}`",
        "",
        f"Provider: {report.setup}; gate min_similarity {report.threshold}.",
        "",
        f"{len({t.case for t in turns})} cases, {len(turns)} turns: {len(answerable)} answerable with "
        f"{sum(len(t.scored) for t in answerable)} expected facts, {len(turns) - len(answerable)} out of scope.",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Retrieval hit rate (expected facts in the answer step's sources) | {report.retrieval} |",
        f"| Citation accuracy (expected facts cited on their page) | {report.citation} |",
        f"| Not-found accuracy (out-of-scope turns not answered) | {report.not_found} |",
        f"| Key-fact grounding (insights facts kept / kept + dropped) | {report.grounding} |",
        f"| Key-fact recall (planted facts in a kept key fact) | {report.recall} |",
        f"| Suggestions answered (each one under a case's last turn, asked in that chat) | {report.suggestions} |",
        f"| Cell grounding (comparison cells kept / kept + dropped) | {report.cell_grounding} |",
        f"| Difference recall (planted changes found as a differing row, both cells quoting the fact) | "
        f"{report.difference_recall} |",
        f"| Equal fields kept equal (planted unchanged fields found as a row that does not differ) | "
        f"{report.equal_kept} |",
        "| Summary source grounding (sources kept / kept + dropped, three samples, by length) | "
        + ", ".join(f"{length} {ratio}" for length, ratio in report.summary_grounding.items()) + " |",
        f"| Gate at {report.threshold}: answerable passed, out-of-scope refused | {_gate_at(turns, report.threshold)} |",
        f"| Best gate threshold on this set | {_fit_line(fit_gate(turns))} |",
        f"| p95 turn latency | {report.turn_p95_ms} ms |",
        "",
        "| Purpose | Model | Calls | Errors | Input tokens | Output tokens | Cost (USD) | p95 latency |",
        "|---|---|---|---|---|---|---|---|",
        *(f"| {u.purpose} | {u.model} | {u.calls} | {u.errors} | {u.input_tokens} | {u.output_tokens} | "
          f"{u.cost_usd}{f' ({u.unpriced_calls} unpriced)' if u.unpriced_calls else ''} | "
          f"{u.latency_ms.p95} ms |" for u in report.spend),
        "",
        f"Comparison of {report.fields[0].expected.policy.document} with "
        f"{report.fields[0].expected.quote.document}, no focus:",
        "",
        "| Expected field | Matched row | Differs | Policy page | Quote page | Result |",
        "|---|---|---|---|---|---|",
        *(_field_line(result) for result in report.fields),
        "",
        "| Case | Question | Interpreted as | Outcome | Retrieval | Citation |",
        "|---|---|---|---|---|---|",
        *(f"| {t.case} | {_cell(t.question)} | {_interpreted(t)} | {t.outcome} | "
          f"{_score(t, [f'{s.fact.id}: {s.retrieval}' for s in t.scored if s.retrieval])} | "
          f"{_score(t, [f'missed {s.fact.id}' for s in t.scored if not s.cited])} |" for t in turns),
    ]
    missed = [s for s in report.suggested if s.outcome is not TurnOutcome.ANSWERED]
    lines += ["", "| Case | Suggestion not answered | Outcome |", "|---|---|---|",
              *(f"| {s.case} | {_cell(s.question)} | {s.outcome} |" for s in missed)] if missed else [
        "", f"All {len(report.suggested)} suggestions came back answered."]
    return "\n".join(lines)


def render_calibration(report: Report) -> str:
    lines = ["| Case | Expect | Standalone query | Best similarity | All terms | Gate |", "|---|---|---|---|---|---|"]
    lines += [f"| {t.case} | {'answer' if t.answerable else 'not_found'} | {_cell(t.standalone)} | "
              f"{t.best_similarity:.3f} | {'yes' if t.lexical_hit else 'no'} | "
              f"{'pass' if t.passes(report.threshold) else 'refuse'} |" for t in report.turns]
    lines += ["", f"At the current threshold {report.threshold}: {_gate_at(report.turns, report.threshold)}.",
              f"Best threshold: {_fit_line(fit_gate(report.turns))}."]
    return "\n".join(lines)


def _field_line(result: FieldResult) -> str:
    expected, row = result.expected, result.row
    verdict = "found" if result.found else "no row" if row is None else "differs" if row.differs else "shown equal"
    return (f"| {expected.field} | {'no row' if row is None else _cell(row.field)} | "
            f"{'-' if row is None else 'yes' if row.differs else 'no'} | {expected.policy.page} | "
            f"{expected.quote.page} | {verdict} |")


def _gate_at(turns: Sequence[TurnResult], threshold: float) -> str:
    answerable = [t for t in turns if t.answerable]
    out_of_scope = [t for t in turns if not t.answerable]
    return (f"{_ratio(t.passes(threshold) for t in answerable)} passed, "
            f"{_ratio(not t.passes(threshold) for t in out_of_scope)} refused")


def _fit_line(fit: GateFit) -> str:
    passed = "none" if fit.lowest_passed is None else f"{fit.lowest_passed:.3f} (+{fit.lowest_passed - fit.threshold:.3f})"
    refused = "none" if fit.highest_refused is None else f"{fit.highest_refused:.3f} (-{fit.threshold - fit.highest_refused:.3f})"
    return (f"{fit.threshold}, {fit.correct}/{fit.total} turns on the right side; nearest similarity passed {passed}, "
            f"nearest refused {refused}")


def _interpreted(turn: TurnResult) -> str:
    return "(unchanged)" if turn.standalone == turn.question else _cell(turn.standalone)


def _score(turn: TurnResult, misses: Sequence[str]) -> str:
    if not turn.answerable:
        return "out of scope"
    return f"{len(turn.scored) - len(misses)}/{len(turn.scored)}" + (f" ({'; '.join(misses)})" if misses else "")


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _commit() -> str:
    """HEAD, marked dirty when the code, cases or samples differ from it."""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
        changed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", "docvault", "evals",
                                  "samples"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{head}-dirty" if changed else head


def write_results(path: Path, results: str) -> None:
    """Replace the text between the results markers. The prose around them is hand-edited and kept."""
    head, start, rest = path.read_text().partition(START)
    _, end, tail = rest.partition(END)
    if not (start and end):
        raise ValueError(f"{path} has no {START} ... {END} block")
    path.write_text(f"{head}{START}\n{results}\n{END}{tail}")


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(prog="python -m docvault.evals", description=__doc__.split("\n\n")[0])
    parser.add_argument("--calibrate", action="store_true",
                        help="print each turn's best similarity and all-terms match, and the best gate threshold")
    parser.add_argument("--out", type=Path, help="write the results between the results markers of this Markdown file")
    parser.add_argument("--cases", type=Path, default=CASES)
    options = parser.parse_args(args)
    report = run(load_settings(), load_cases(options.cases))
    results = render(report, shlex.join(["uv", "run", "python", "-m", "docvault.evals", *args]))
    print(render_calibration(report) if options.calibrate else results)
    if options.out:
        write_results(options.out, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
