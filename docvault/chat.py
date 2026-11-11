"""Chat over a fixed set of documents: start, ask, history.

Owns chats, chat_documents and chat_turns. ask() is the whole RAG path in
one function a reader can follow top to bottom:

  question -> condense with history -> retrieve (hybrid, scoped, per-document quota)
           -> gate -> answer prompt -> ground (verify quotes, resolve pages) -> persist turn

Pure decisions (select_sources, gate, ground) are separate functions with
no I/O, so the grounding and refusal rules are unit-tested without a model
or a database.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector import Vector as PgVector
from psycopg import Connection
from psycopg.types.json import Jsonb

from . import pages, prompts
from .ai import TurnOwner, Vector
from .context import Ctx
from .db import Db
from .domain import (
    Answer,
    Answered,
    Chat,
    ChatId,
    ChunkId,
    Citation,
    DocRef,
    DocStatus,
    DocumentId,
    ErrorCode,
    NotFound,
    PageBasis,
    PageMap,
    Purpose,
    Source,
    Turn,
    TurnId,
    TurnOutcome,
    Usage,
    VaultError,
)
from .prompts import AnswerDraft, CondensedQuery

RRF_K = 60
CANDIDATES = 50
UNVERIFIED_TEXT = ("I found passages that look related, but I could not verify an answer against their text, "
                   "so I am not giving one.")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One row from the retrieval query, before source selection gives it a label."""

    chunk_id: ChunkId
    doc: DocRef
    text: str
    page_map: PageMap
    similarity: float
    rrf: float
    lexical_hit: bool       # chunk.tsv matched websearch_to_tsquery(question) (all terms present)


# ------------------------------------------------------------------ commands


def start(db: Db, document_ids: Sequence[DocumentId], *, title: str | None = None) -> Chat:
    """Open a chat over a fixed, non-empty document set. Duplicate ids collapse.

    Unknown ids -> DOCUMENT_NOT_FOUND (404) listing them.
    Any id not READY -> DOCUMENTS_NOT_READY (409) listing id and status.
    The set is stored in chat_documents and never changes; a document uploaded
    later needs a new chat. This keeps every turn's scope reproducible.
    """
    wanted = list(dict.fromkeys(document_ids))
    with db.connection() as conn, conn.transaction():
        # SHARE holds off a delete or a status change between this check and the chat_documents insert.
        status = {row["id"]: row["status"] for row in conn.execute(
            "SELECT id, status FROM documents WHERE id = ANY(%s) FOR SHARE", (wanted,))}
        if missing := [doc_id for doc_id in wanted if doc_id not in status]:
            raise VaultError(ErrorCode.DOCUMENT_NOT_FOUND, f"{len(missing)} of {len(wanted)} documents do not exist.",
                             details={"missing": [str(doc_id) for doc_id in missing]})
        if not_ready := [{"id": str(doc_id), "status": status[doc_id]} for doc_id in wanted
                         if status[doc_id] != DocStatus.READY]:
            raise VaultError(ErrorCode.DOCUMENTS_NOT_READY,
                             f"{len(not_ready)} of {len(wanted)} documents are not ready.",
                             details={"not_ready": not_ready})
        chat = conn.execute("INSERT INTO chats (title) VALUES (%s) RETURNING id, title, created_at",
                            (title,)).fetchone()
        conn.execute("INSERT INTO chat_documents (chat_id, document_id, filename)"
                     " SELECT %s, id, filename FROM documents WHERE id = ANY(%s)", (chat["id"], wanted))
        documents = _scope(conn, chat["id"])
    return Chat(ChatId(chat["id"]), chat["title"], documents, chat["created_at"])


def ask(ctx: Ctx, chat_id: ChatId, question: str) -> Turn:
    """Answer one question with citations, or refuse honestly. Persists exactly one turn.

    Every model call is billed to the turn id, which exists before the first
    call. A provider failure raises VaultError(AI_UNAVAILABLE) (HTTP 503 with
    Retry-After) and writes no turn; the failed calls stay in llm_calls. No
    connection or transaction is held across a model call.
    """
    turn_id = TurnId(uuid4())
    owner = TurnOwner(turn_id)
    started = datetime.now(UTC)
    with ctx.db.connection() as conn:
        _require_chat(conn, chat_id)
        documents = _scope(conn, chat_id)
        past = _turns(conn, chat_id, limit=ctx.settings.history_turns)

    standalone = question if not past else ctx.ai.complete(
        prompts.condense(past, question), CondensedQuery, owner=owner).standalone
    [q_vec] = ctx.ai.embed([standalone], purpose=Purpose.EMBED_QUERY, owner=owner)
    with ctx.db.connection() as conn:
        candidates = retrieve(conn, documents, q_vec, standalone, ctx.ai.embed_model,
                              per_doc=ctx.settings.retrieval_per_doc_min)

    answer: Answer
    if gate(candidates, ctx.ai.min_similarity):
        sources = select_sources(candidates, k=ctx.settings.retrieval_k, per_doc_min=ctx.settings.retrieval_per_doc_min)
        draft = ctx.ai.complete(prompts.answer(question, standalone, past, sources), AnswerDraft, owner=owner)
        answer = ground(draft, sources)
    else:
        answer = NotFound(TurnOutcome.NO_EVIDENCE, prompts.no_evidence_text(documents))

    with ctx.db.connection() as conn:
        conn.execute(
            "INSERT INTO chat_turns (id, chat_id, question, standalone_query, outcome, answer, citations,"
            " started_at, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (turn_id, chat_id, question, standalone, answer.outcome, answer.text,
             Jsonb([_snapshot(citation) for citation in answer.citations]), started, datetime.now(UTC)))
        return _row_to_turn(conn.execute(_TURN_SQL + "WHERE t.id = %s GROUP BY t.id", (turn_id,)).fetchone())


def history(db: Db, chat_id: ChatId, *, limit: int = 50) -> list[Turn]:
    """The latest `limit` turns, oldest first, each with its citation snapshot and usage. Raises CHAT_NOT_FOUND."""
    with db.connection() as conn:
        _require_chat(conn, chat_id)
        return _turns(conn, chat_id, limit=limit)


# ------------------------------------------------------------------ rows

# Usage is read back from the ledger rather than kept on the turn, so it counts every attempt the gateway made.
# count(cost_usd) skips NULLs: when it falls short of count(id), some call was unpriced and the total is unknown.
_TURN_SQL = """
SELECT t.id, t.chat_id, t.question, t.standalone_query, t.outcome, t.answer, t.citations, t.started_at,
       t.finished_at, count(l.id) AS calls, coalesce(sum(l.input_tokens), 0) AS input_tokens,
       coalesce(sum(l.output_tokens), 0) AS output_tokens,
       CASE WHEN count(l.cost_usd) = count(l.id) THEN coalesce(sum(l.cost_usd), 0) END AS cost_usd
FROM chat_turns t LEFT JOIN llm_calls l ON l.turn_id = t.id
"""


def _require_chat(conn: Connection, chat_id: ChatId) -> None:
    if conn.execute("SELECT 1 FROM chats WHERE id = %s", (chat_id,)).fetchone() is None:
        raise VaultError(ErrorCode.CHAT_NOT_FOUND, f"No chat has the id {chat_id}.")


def _scope(conn: Connection, chat_id: ChatId) -> tuple[DocRef, *tuple[DocRef, ...]]:
    """The chat's documents in one fixed order, for the API body and the refusal text.

    Raises DOCUMENTS_DELETED when any of them was deleted after the chat started:
    answering from the rest would quietly change what the chat was asked about.
    """
    rows = conn.execute(
        "SELECT cd.document_id, cd.filename, d.page_basis FROM chat_documents cd"
        " LEFT JOIN documents d ON d.id = cd.document_id WHERE cd.chat_id = %s ORDER BY cd.filename, cd.document_id",
        (chat_id,)).fetchall()
    if deleted := [{"id": str(row["document_id"]), "filename": row["filename"]} for row in rows
                   if row["page_basis"] is None]:
        raise VaultError(ErrorCode.DOCUMENTS_DELETED,
                         f"{len(deleted)} of {len(rows)} documents in this chat were deleted. "
                         "Start a new chat with the documents you want to ask about.", details={"deleted": deleted})
    first, *rest = (DocRef(DocumentId(row["document_id"]), row["filename"], PageBasis(row["page_basis"]))
                    for row in rows)
    return first, *rest


def _turns(conn: Connection, chat_id: ChatId, *, limit: int) -> list[Turn]:
    rows = conn.execute(_TURN_SQL + "WHERE t.chat_id = %s GROUP BY t.id ORDER BY t.started_at DESC, t.id DESC LIMIT %s",
                        (chat_id, limit)).fetchall()
    return [_row_to_turn(row) for row in reversed(rows)]


def _row_to_turn(row: Mapping[str, Any]) -> Turn:
    """The database-to-domain boundary for turns. answered_iff_cited guarantees an answered row has citations."""
    outcome = TurnOutcome(row["outcome"])
    answer: Answer
    if outcome is TurnOutcome.ANSWERED:
        citations = [_citation(snapshot) for snapshot in row["citations"]]
        answer = Answered(row["answer"], (citations[0], *citations[1:]))
    else:
        answer = NotFound(outcome, row["answer"])
    return Turn(id=TurnId(row["id"]), chat_id=ChatId(row["chat_id"]), question=row["question"],
                standalone_query=row["standalone_query"], answer=answer, started_at=row["started_at"],
                finished_at=row["finished_at"],
                usage=Usage(row["calls"], row["input_tokens"], row["output_tokens"], row["cost_usd"]))


def _snapshot(citation: Citation) -> dict[str, object]:
    return {"document_id": str(citation.document_id), "filename": citation.filename, "page": citation.page,
            "page_basis": citation.page_basis, "quote": citation.quote, "chunk_id": str(citation.chunk_id)}


def _citation(snapshot: Mapping[str, Any]) -> Citation:
    return Citation(DocumentId(UUID(snapshot["document_id"])), snapshot["filename"], snapshot["page"],
                    PageBasis(snapshot["page_basis"]), snapshot["quote"], ChunkId(UUID(snapshot["chunk_id"])))


# ------------------------------------------------------------------ retrieval

# A chat's scope is small (a few documents, a few thousand chunks), so every
# scoped chunk is scored exactly; filtering can never starve the top-k, and
# MATERIALIZED keeps any future vector index out of this plan.
# The full-text rank uses any query term (websearch_to_tsquery ANDs them all,
# and a natural question rarely has every term in one chunk); lexical_hit, the
# relevance gate's arm, still needs all of them.
# Both rankings and the per-document rank come from window functions over the
# same scored set, so hybrid fusion costs one query. doc_rank only makes sure
# every document reaches select_sources, which picks by fused score.
EXACT_SQL = """
WITH query AS (
  SELECT websearch_to_tsquery('english', %(qtext)s) AS q_all,
         replace(websearch_to_tsquery('english', %(qtext)s)::text, '&', '|')::tsquery AS q_any
), scored AS MATERIALIZED (
  SELECT c.id, c.document_id, c.text, c.page_map,
         c.embedding <=> %(q)s AS distance,
         c.tsv @@ query.q_all AS lexical_hit,
         ts_rank_cd(c.tsv, query.q_any) AS lex
  FROM chunks c, query
  WHERE c.document_id = ANY(%(doc_ids)s) AND c.embedding_model = %(model)s
), ranked AS (
  SELECT *,
         row_number() OVER (ORDER BY distance)                       AS vec_rank,
         row_number() OVER (ORDER BY lex DESC, distance)             AS lex_rank,
         row_number() OVER (PARTITION BY document_id ORDER BY distance) AS doc_rank
  FROM scored
)
SELECT id, document_id, text, page_map, 1 - distance AS similarity, lexical_hit,
       1.0 / (%(rrf_k)s + vec_rank) + CASE WHEN lex > 0 THEN 1.0 / (%(rrf_k)s + lex_rank) ELSE 0 END AS rrf
FROM ranked
WHERE vec_rank <= %(n)s OR (lex > 0 AND lex_rank <= %(n)s) OR doc_rank <= %(per_doc)s
ORDER BY rrf DESC
"""

def retrieve(conn: Connection, documents: Sequence[DocRef], q_vec: Vector, query_text: str,
             embed_model: str, *, per_doc: int) -> list[Candidate]:
    """Run EXACT_SQL over the chat's documents.

    Only chunks embedded with the current model are searchable, so a model
    switch can never compare vectors from two embedding spaces.
    """
    by_id = {doc.id: doc for doc in documents}
    rows = conn.execute(EXACT_SQL, {"qtext": query_text, "q": PgVector(q_vec), "doc_ids": list(by_id),
                                    "model": embed_model, "rrf_k": RRF_K, "n": CANDIDATES, "per_doc": per_doc})
    return [Candidate(ChunkId(row["id"]), by_id[row["document_id"]], row["text"],
                      PageMap(tuple((offset, page) for offset, page in row["page_map"])),
                      row["similarity"], float(row["rrf"]), row["lexical_hit"]) for row in rows]


def gate(candidates: Sequence[Candidate], min_similarity: float) -> bool:
    """Is there anything worth sending to the answer model?

    True when the best vector similarity reaches min_similarity, or when any
    chunk contains every query term (lexical_hit). The lexical arm keeps exact
    identifiers ("CG 20 10", a policy number) from being refused by a
    semantic threshold. False means refuse with NO_EVIDENCE and spend no
    answer call. min_similarity belongs to the embedding backend because
    similarity scales differ per model.
    """
    return any(candidate.lexical_hit or candidate.similarity >= min_similarity for candidate in candidates)


def select_sources(candidates: Sequence[Candidate], *, k: int, per_doc_min: int) -> list[Source]:
    """Choose the k sources the model sees, labelled S1..Sk in fused (rrf) order.

    Each document first gets its best per_doc_min chunks by rrf, then the rest
    fill to k by rrf. "Compare the deductibles in these three quotes" then sees
    all three quotes even when one dominates the scores. With one document this
    is the top k. An exact rrf tie goes to the higher similarity.
    """
    ranked = sorted(candidates, key=lambda candidate: (candidate.rrf, candidate.similarity), reverse=True)
    taken: Counter[DocumentId] = Counter()
    rounds: list[tuple[int, int]] = []
    for at, candidate in enumerate(ranked):
        rounds.append((min(taken[candidate.doc.id], per_doc_min), at))
        taken[candidate.doc.id] += 1
    # The quota goes round by round (every document's best, then every second best), so when more documents
    # than k / per_doc_min are in scope, as many of them as fit still get a source.
    chosen = sorted(at for _, at in sorted(rounds)[:k])
    return [Source(f"S{n}", c.chunk_id, c.doc, c.text, c.page_map, c.similarity, c.rrf)
            for n, c in enumerate((ranked[at] for at in chosen), start=1)]


# ------------------------------------------------------------------ grounding

# A run of adjacent markers ("[S2]", "[S2][S3]", "[S2, S3]") and the spaces before it.
_MARKERS = re.compile(r"(?P<pad>[ \t]*)(?P<run>(?:\[S\d+(?:\s*,\s*S\d+)*\])+)")
_LABEL = re.compile(r"S\d+")


def ground(draft: AnswerDraft, sources: Sequence[Source]) -> Answer:
    """Turn the model's draft into an Answer the user can trust. Pure.

    An evidence quote is kept only when pages.locate_quote finds it in the
    source its label names; a paraphrase, a fabrication or an unknown label is
    dropped. A kept citation quotes the source span, not the model's copy, and
    takes the page where that span starts inside the chunk, so a chunk spanning
    pages 11-12 cites 12 when the quoted words are on 12. Citations are numbered
    1..n in evidence order, and each [S#] marker becomes the numbers of its
    label's citations. Nothing verified -> UNVERIFIED.
    """
    if not draft.supported:
        return NotFound(TurnOutcome.MODEL_DECLINED, _rewrite_markers(draft.answer, {}))
    by_label = {source.label: source for source in sources}
    citations: list[Citation] = []
    refs: dict[str, list[int]] = {}
    seen: set[tuple[str, int, int]] = set()
    for evidence in draft.evidence:
        source = by_label.get(evidence.source)
        span = None if source is None else pages.locate_quote(evidence.quote, source.text)
        if span is None or (evidence.source, *span) in seen:
            continue
        seen.add((evidence.source, *span))
        start, end = span
        citations.append(Citation(source.doc.id, source.doc.filename, source.page_map.page_at(start),
                                  source.doc.page_basis, pages.collapse_whitespace(source.text[start:end]),
                                  source.chunk_id))
        refs.setdefault(evidence.source, []).append(len(citations))
    if not citations:
        return NotFound(TurnOutcome.UNVERIFIED, UNVERIFIED_TEXT)
    return Answered(_rewrite_markers(draft.answer, refs), (citations[0], *citations[1:]))


def _rewrite_markers(text: str, refs: Mapping[str, Sequence[int]]) -> str:
    """Each marker run becomes "[1][2]" for its labels' verified citations. A run with none is removed with the
    spaces before it, so "limit [S9]." reads "limit." rather than "limit ."."""
    def replace(match: re.Match[str]) -> str:
        cited = dict.fromkeys(ref for label in _LABEL.findall(match["run"]) for ref in refs.get(label, ()))
        return match["pad"] + "".join(f"[{ref}]" for ref in cited) if cited else ""

    return _MARKERS.sub(replace, text).strip()
