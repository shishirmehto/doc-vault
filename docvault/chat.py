"""Chat over a fixed set of documents: start, ask, history.

Owns chats, chat_documents and chat_turns. ask() is the whole RAG path in
one function a reader can follow top to bottom:

  question -> condense with history -> search (hybrid retrieval with a per-document quota, the pages and
           sections the question names, each document's opening passage)
           -> gate -> answer prompt -> ground (verify quotes, resolve pages) -> suggest -> persist turn

A chat's first question is looked up in the answer cache before any of that.

Pure decisions (select_sources, gate, ground, suggestions) are separate functions with
no I/O, so the grounding and refusal rules are unit-tested without a model
or a database.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pgvector import Vector as PgVector
from psycopg import Connection
from psycopg.types.json import Jsonb

from . import insights, pages, prompts
from .ai import USAGE_SQL, TurnOwner, Vector, usage_of
from .answer_text import AnswerField, MarkerFilter
from .cache import Cache
from .context import Ctx
from .db import Db, decode_cursor, next_cursor
from .documents import ready_documents
from .domain import (
    Answer,
    Answered,
    Chat,
    ChatDocument,
    ChatId,
    ChunkId,
    Citation,
    DocRef,
    DocStatus,
    DocumentId,
    ErrorCode,
    LoggedPassage,
    NotFound,
    OutlineEntry,
    PageBasis,
    PageMap,
    PassageKind,
    Purpose,
    RetrievalLog,
    Source,
    Turn,
    TurnId,
    TurnOutcome,
    VaultError,
)
from .prompts import AnswerDraft, CondensedQuery

type Stage = Literal["condensing", "searching", "answering"]

RRF_K = 60
# The full-text rank ORs every question word, so a long broker question matches loosely related passages; at
# full weight it pulled the right passage below 16 on 4 of 53 public-document questions (evals/retrieval).
# Half weight still lifts an exact identifier ("CG 21 47") and lost no sample fact at k = 16.
LEXICAL_WEIGHT = 0.5
CANDIDATES = 50
MAX_SUGGESTIONS = 3
MAX_SUGGESTION_CHARS = 160
UNVERIFIED_TEXT = ("I found passages that look related, but I could not verify an answer against their text, "
                   "so I am not giving one.")
OPENING_DOCS = 3            # a chat of up to this many documents sends each one's opening passage with its k sources
PAGE_PASSAGES_PER_REQUEST = 2   # chunks sent for each page, range or section a question names, per document
PAGE_PASSAGES_PER_DOC = 4   # chunks sent for the pages and sections a question names, per document
MAX_PAGE_SPAN = 10          # "pages 1-500" asks for the first 10
TITLE_LOOKAHEAD = 3         # chunks read past a section's first ones to find the one holding its title
ANSWER_CACHE_FORMAT = 12    # part of every answer key: a change to the cached value's shape or to select_sources bumps it
ANSWER_TTL_S = 30 * 24 * 3600


@dataclass(frozen=True, slots=True)
class Candidate:
    """One row from the retrieval query, before source selection gives it a label."""

    chunk_id: ChunkId
    doc: DocRef
    text: str
    page_map: PageMap
    similarity: float
    rrf: float
    lexical_hit: bool       # chunk.tsv matched websearch_to_tsquery(question), which ANDs the terms; "or" and "-word" become OR and NOT


@dataclass(frozen=True, slots=True)
class PageRequest:
    """Pages a question names, first..last inclusive. A negative number counts from the end, so -1 is the last page."""

    first: int
    last: int

    def resolve(self, page_count: int) -> tuple[int, int] | None:
        """The pages this request means in a document of page_count pages, cut to the pages it has, or None."""
        first, last = (number if number > 0 else page_count + 1 + number for number in (self.first, self.last))
        first, last = max(first, 1), min(last, page_count)
        return (first, last) if first <= last else None


@dataclass(frozen=True, slots=True)
class SectionRequest:
    """An outline entry a question names, and the pages its section spans, first..last inclusive."""

    title: str
    first: int
    last: int


@dataclass(frozen=True, slots=True)
class Found:
    """Every passage one question's search found, before the gate and select_sources choose from them."""

    retrieved: list[Candidate]      # hybrid retrieval: what the gate scores
    on_pages: list[Candidate]       # the parts of chunks on the pages the question names by number
    in_sections: list[Candidate]    # the parts of chunks that open the sections the question names
    openings: list[Candidate]       # each document's opening passage

    def matched(self, min_similarity: float) -> bool:
        """The gate passes, or the question names a page or a section the documents have: that is what the user
        asked for."""
        return gate(self.retrieved, min_similarity) or bool(self.on_pages) or bool(self.in_sections)


# ------------------------------------------------------------------ commands


def start(db: Db, document_ids: Sequence[DocumentId], *, title: str | None = None) -> Chat:
    """Open a chat over a fixed, non-empty document set. Duplicate ids collapse.

    Unknown ids -> DOCUMENT_NOT_FOUND (404) listing them.
    Any id not READY -> DOCUMENTS_NOT_READY (409) listing id, filename and status.
    The set is stored in chat_documents and never changes; a document uploaded
    later needs a new chat. This keeps every turn's scope reproducible.
    """
    wanted = list(dict.fromkeys(document_ids))
    with db.connection() as conn, conn.transaction():
        ready_documents(conn, wanted)
        chat_id = conn.execute("INSERT INTO chats (title) VALUES (%s) RETURNING id", (title,)).fetchone()["id"]
        conn.execute("INSERT INTO chat_documents (chat_id, document_id, filename)"
                     " SELECT %s, id, filename FROM documents WHERE id = ANY(%s)", (chat_id, wanted))
        return _row_to_chat(conn.execute(_CHAT_SQL + "WHERE c.id = %s GROUP BY c.id", (chat_id,)).fetchone())


def get(db: Db, chat_id: ChatId) -> Chat:
    """A chat and its document scope, deleted documents marked. Raises CHAT_NOT_FOUND."""
    with db.connection() as conn:
        row = conn.execute(_CHAT_SQL + "WHERE c.id = %s GROUP BY c.id", (chat_id,)).fetchone()
    if row is None:
        raise _chat_not_found(chat_id)
    return _row_to_chat(row)


def list_chats(db: Db, *, limit: int, cursor: str | None) -> tuple[list[Chat], str | None]:
    """Newest first. Keyset pagination on (created_at DESC, id DESC), the same cursor as list_documents."""
    after = decode_cursor(cursor) if cursor else None
    with db.connection() as conn:
        rows = conn.execute(
            _CHAT_SQL + "WHERE (%(created_at)s::timestamptz IS NULL"
            " OR (c.created_at, c.id) < (%(created_at)s::timestamptz, %(id)s::uuid))"
            " GROUP BY c.id ORDER BY c.created_at DESC, c.id DESC LIMIT %(limit)s",
            {"created_at": after and after[0], "id": after and after[1], "limit": limit + 1}).fetchall()
    return [_row_to_chat(row) for row in rows[:limit]], next_cursor(rows, limit)


def ask(ctx: Ctx, chat_id: ChatId, question: str, *, on_stage: Callable[[Stage], None] | None = None,
        on_text: Callable[[str], None] | None = None, on_reset: Callable[[], None] | None = None) -> Turn:
    """Answer one question with citations, or refuse honestly. Persists exactly one turn.

    Every model call is billed to the turn id, which exists before the first
    call. A provider failure raises VaultError(AI_UNAVAILABLE) (HTTP 503, with
    Retry-After when retryable) and writes no turn; the failed calls stay in llm_calls. No
    connection or transaction is held across a model call.

    A chat's first question is looked up in the answer cache under answer_key. A hit makes
    no model call of any kind, sends no stage, sends its text once to on_text, and stores a
    turn marked cached with the retrieval log of the turn that answered first. Only answered
    turns go into the cache: a refusal asked again should get a fresh attempt from a live model,
    and a cached refusal would pin it until the prompt version changes. A follow-up depends on
    the turns before it, so it is never looked up or stored.

    Over today's AI spending cap it raises VaultError(QUOTA_EXCEEDED) (HTTP 429) before
    any model call. A cache hit still answers.

    A question that names a page of a PDF (page_requests) or a section in a document's outline (section_requests)
    counts as matched when the document has it, and the parts of chunks on it lead the sources. When nothing matches,
    the answer step sees only each document's opening passage, if the backend declines what its sources do not
    answer. That answers a question about the document as a whole; anything else is refused with NO_EVIDENCE, and
    that call's text is not streamed, so a decline never shows before the refusal replaces it.

    The optional sinks report progress. on_stage hears each step as it starts, after the
    chat, its scope, the cache and the spending cap are checked, so a missing chat, a deleted document
    or a used-up cap raises before any of them. on_text gets provisional answer text, markers removed, while
    the answer model writes it, and on_reset means "discard the text sent so far"
    (a retry or repair follows). The returned turn replaces all provisional text.
    """
    stage = on_stage or _ignore
    turn_id = TurnId(uuid4())
    owner = TurnOwner(turn_id)
    started = datetime.now(UTC)
    with ctx.db.connection() as conn:
        _require_chat(conn, chat_id)
        documents = scope(conn, chat_id)
        past = _turns(conn, chat_id, limit=ctx.settings.history_turns)
        key = None if past else answer_key(ctx, _hashes(conn, documents), question)
    if key is not None and (hit := _cached_answer(ctx.cache, key)) is not None:
        answer, suggested, logged = hit
        if on_text is not None:
            on_text(prompts.REF_MARKER.sub("", answer.text))
        return _store_turn(ctx, turn_id, chat_id, question, question, answer, suggested, started, logged,
                           cached=True)
    ctx.ai.check_budget(Purpose.ANSWER)

    if past:
        stage("condensing")
    standalone = question if not past else ctx.ai.complete(
        prompts.condense(past, question), CondensedQuery, owner=owner).standalone
    stage("searching")
    [q_vec] = ctx.ai.embed([standalone], purpose=Purpose.EMBED_QUERY, owner=owner)
    with ctx.db.connection() as conn:
        found = search(conn, documents, q_vec, standalone, ctx.ai.embed_model,
                       per_doc=ctx.settings.retrieval_per_doc_min, k=ctx.settings.retrieval_k)
    matched = found.matched(ctx.ai.min_similarity)
    fallback = found.openings[:ctx.settings.retrieval_k] if ctx.ai.chat.declines_unanswerable else []
    picked = _pick(found.retrieved, k=ctx.settings.retrieval_k, per_doc_min=ctx.settings.retrieval_per_doc_min,
                   on_pages=found.on_pages, in_sections=found.in_sections,
                   openings=found.openings) if matched else fallback
    sources = _labelled(picked)
    logged = _log_json(retrieval_log(standalone, found, picked, min_similarity=ctx.ai.min_similarity,
                                     k=ctx.settings.retrieval_k))

    answer: Answer | None = None
    suggested: tuple[str, ...] = ()
    if sources:
        stage("answering")
        provisional = _ProvisionalAnswer(on_text, on_reset or _ignore) if matched and on_text is not None else None
        draft = ctx.ai.complete(prompts.answer(question, standalone, past, sources), AnswerDraft, owner=owner,
                                stream=provisional)
        answer = ground(draft, sources)
        suggested = suggestions(draft.follow_ups, question)
    # Past a failed gate the model saw only opening passages, and only an answer drawn from them stands.
    if answer is None or (not matched and not isinstance(answer, Answered)):
        answer = NotFound(TurnOutcome.NO_EVIDENCE, prompts.no_evidence_text(documents))
        with ctx.db.connection() as conn:
            facts = insights.ready_key_facts(conn, [doc.id for doc in documents])
        suggested = fact_suggestions([[fact.label for fact in facts.get(doc.id, ())] for doc in documents], question)

    turn = _store_turn(ctx, turn_id, chat_id, question, standalone, answer, suggested, started, logged,
                       cached=False)
    if key is not None and isinstance(answer, Answered):
        _cache_answer(ctx.cache, key, answer, suggested, logged)
    return turn


def answer_key(ctx: Ctx, documents: Mapping[DocumentId, tuple[bytes, str | None]], question: str) -> str:
    """The answer cache key for a first question over `documents` (id -> sha256 and the chunk settings it was cut
    with): a hash of every input the answer depends on, so a change to any of them is a miss. A document's own
    chunking, not the current setting, so a document still waiting to be cut again keys apart from its new cut.
    The question is compared without case or extra spaces."""
    inputs = {
        "format": ANSWER_CACHE_FORMAT,
        "documents": sorted([str(doc_id), sha.hex(), cut] for doc_id, (sha, cut) in documents.items()),
        "question": _same_question(question),
        "prompt_version": prompts.ANSWER_VERSION,
        "chat_model": ctx.ai.chat.models["main"],
        "reasoning_effort": ctx.settings.openai_chat_reasoning_effort,
        "embed_model": ctx.ai.embed_model,
        "retrieval_k": ctx.settings.retrieval_k,
        "retrieval_per_doc_min": ctx.settings.retrieval_per_doc_min,
        "min_similarity": ctx.ai.min_similarity,
    }
    return "docvault:answer:" + hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def _hashes(conn: Connection, documents: Sequence[DocRef]) -> dict[DocumentId, tuple[bytes, str | None]]:
    rows = conn.execute("SELECT id, sha256, chunking FROM documents WHERE id = ANY(%s)", ([doc.id for doc in documents],))
    return {DocumentId(row["id"]): (bytes(row["sha256"]), row["chunking"]) for row in rows}


def _cached_answer(cache: Cache, key: str) -> tuple[Answered, tuple[str, ...], dict[str, Any]] | None:
    """The stored answer, suggestions and retrieval log (as _log_json wrote it), counting one hit or one miss."""
    [value] = cache.get_many([key])
    cache.count("answer", hits=int(value is not None), misses=int(value is None))
    if value is None:
        return None
    stored = json.loads(value)
    return _answered(stored["text"], stored["citations"]), tuple(stored["suggestions"]), stored["retrieval"]


def _cache_answer(cache: Cache, key: str, answer: Answered, suggested: Sequence[str], logged: dict[str, Any]) -> None:
    cache.set_many({key: json.dumps({"text": answer.text, "citations": [_snapshot(c) for c in answer.citations],
                                     "suggestions": list(suggested), "retrieval": logged}).encode()},
                   ANSWER_TTL_S)


def _store_turn(ctx: Ctx, turn_id: TurnId, chat_id: ChatId, question: str, standalone: str, answer: Answer,
                suggested: Sequence[str], started: datetime, logged: dict[str, Any], *, cached: bool) -> Turn:
    with ctx.db.connection() as conn:
        conn.execute(
            "INSERT INTO chat_turns (id, chat_id, question, standalone_query, outcome, answer, citations,"
            " suggestions, cached, retrieval, started_at, finished_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (turn_id, chat_id, question, standalone, answer.outcome, answer.text,
             Jsonb([_snapshot(citation) for citation in answer.citations]), Jsonb(list(suggested)), cached,
             Jsonb(logged), started, datetime.now(UTC)))
        return _row_to_turn(conn.execute(_TURN_SQL + "WHERE t.id = %s GROUP BY t.id", (turn_id,)).fetchone())


def _ignore(*_: object) -> None:
    pass


class _ProvisionalAnswer:
    def __init__(self, on_text: Callable[[str], None], on_reset: Callable[[], None]) -> None:
        self._on_text, self._on_reset = on_text, on_reset
        self._field, self._markers = AnswerField(), MarkerFilter()
        self._sent = False

    def attempt(self) -> None:
        if self._sent:
            self._on_reset()
        self._field, self._markers, self._sent = AnswerField(), MarkerFilter(), False

    def text(self, chunk: str) -> None:
        delta = self._markers.feed(self._field.feed(chunk))
        if self._field.done:
            delta += self._markers.flush()
        if delta:
            self._sent = True
            self._on_text(delta)


def history(db: Db, chat_id: ChatId, *, limit: int = 50) -> list[Turn]:
    """The latest `limit` turns, oldest first, each with its citation snapshot and usage. Raises CHAT_NOT_FOUND."""
    with db.connection() as conn:
        _require_chat(conn, chat_id)
        return _turns(conn, chat_id, limit=limit)


def turn_retrieval(db: Db, chat_id: ChatId, turn_id: TurnId) -> RetrievalLog:
    """What one turn's search found, with scores, and which passages its answer step was sent. Raises
    CHAT_NOT_FOUND, and TURN_NOT_FOUND for a turn of another chat or one asked before turns kept a log."""
    with db.connection() as conn:
        _require_chat(conn, chat_id)
        row = conn.execute("SELECT retrieval FROM chat_turns WHERE id = %s AND chat_id = %s",
                           (turn_id, chat_id)).fetchone()
    if row is None:
        raise VaultError(ErrorCode.TURN_NOT_FOUND, f"Chat {chat_id} has no turn with the id {turn_id}.")
    if row["retrieval"] is None:
        raise VaultError(ErrorCode.TURN_NOT_FOUND,
                         f"Turn {turn_id} has no retrieval log: it was asked before turns kept one.")
    return _log_of(row["retrieval"])


def sent_sources(conn: Connection, documents: Sequence[DocRef], log: RetrievalLog) -> list[Source]:
    """The sources a turn's answer step was sent, S1 first, rebuilt from its retrieval log and the chunks it names.

    A page or section passage is cut again to its own pages with pages.part_on_pages: page numbers strictly increase
    in a chunk, so that gives back the same part. Any other passage is its whole chunk. A chunk sent both cut and whole
    comes back as both.
    """
    sent = sorted((p for p in log.passages if p.label is not None), key=lambda p: int(p.label[1:]))
    rows = {row["id"]: row for row in conn.execute("SELECT id, text, page_map FROM chunks WHERE id = ANY(%s)",
                                                     ([p.chunk_id for p in sent],))}
    by_id = {doc.id: doc for doc in documents}
    sources: list[Source] = []
    for p in sent:
        text, page_map = rows[p.chunk_id]["text"], _page_map(rows[p.chunk_id]["page_map"])
        if p.kind in (PassageKind.PAGE, PassageKind.SECTION):
            text, page_map = pages.part_on_pages(text, page_map, p.first_page, p.last_page)
        sources.append(Source(p.label, p.chunk_id, by_id[p.document_id], text, page_map, p.similarity, p.rrf))
    return sources


# ------------------------------------------------------------------ rows

# Usage is read back from the ledger rather than kept on the turn, so it counts every attempt the gateway made.
_TURN_SQL = f"""
SELECT t.id, t.chat_id, t.question, t.standalone_query, t.outcome, t.answer, t.citations, t.suggestions,
       t.cached, t.started_at, t.finished_at, {USAGE_SQL}
FROM chat_turns t LEFT JOIN llm_calls l ON l.turn_id = t.id
"""


# A chat with its scope in the fixed order scope uses. A document deleted since the chat started has no
# documents row; its filename comes from the snapshot.
_CHAT_SQL = """
SELECT c.id, c.title, c.created_at,
       jsonb_agg(jsonb_build_object('id', cd.document_id, 'filename', cd.filename, 'deleted', d.id IS NULL)
                 ORDER BY cd.filename, cd.document_id) AS documents
FROM chats c JOIN chat_documents cd ON cd.chat_id = c.id LEFT JOIN documents d ON d.id = cd.document_id
"""


def _row_to_chat(row: Mapping[str, Any]) -> Chat:
    first, *rest = (ChatDocument(DocumentId(UUID(doc["id"])), doc["filename"], doc["deleted"])
                    for doc in row["documents"])
    return Chat(ChatId(row["id"]), row["title"], (first, *rest), row["created_at"])


def _chat_not_found(chat_id: ChatId) -> VaultError:
    return VaultError(ErrorCode.CHAT_NOT_FOUND, f"No chat has the id {chat_id}.")


def _require_chat(conn: Connection, chat_id: ChatId) -> None:
    if conn.execute("SELECT 1 FROM chats WHERE id = %s", (chat_id,)).fetchone() is None:
        raise _chat_not_found(chat_id)


def scope(conn: Connection, chat_id: ChatId) -> tuple[DocRef, *tuple[DocRef, ...]]:
    """The chat's documents in one fixed order, as the model, the citations and the refusal text name them.

    Two versions of one file share its name, so each is then named "<filename> (version N)".
    Raises DOCUMENTS_DELETED when any of them was deleted after the chat started:
    answering from the rest would quietly change what the chat was asked about.
    Raises DOCUMENTS_NOT_READY while any is indexed again (new chunk settings or embedding model), because its
    chunks are then partly embedded, and an answer from them could also land in the answer cache.
    """
    rows = conn.execute(
        "SELECT cd.document_id, CASE WHEN count(*) OVER (PARTITION BY cd.filename) > 1 AND d.version IS NOT NULL"
        "   THEN cd.filename || ' (version ' || d.version || ')' ELSE cd.filename END AS filename, d.page_basis,"
        " d.page_count, d.outline, d.status"
        " FROM chat_documents cd LEFT JOIN documents d ON d.id = cd.document_id WHERE cd.chat_id = %s"
        " ORDER BY cd.filename, cd.document_id", (chat_id,)).fetchall()
    if deleted := [{"id": str(row["document_id"]), "filename": row["filename"], "status": "deleted"} for row in rows
                   if row["page_basis"] is None]:
        raise VaultError(ErrorCode.DOCUMENTS_DELETED,
                         f"{len(deleted)} of {len(rows)} documents in this chat were deleted. "
                         "Start a new chat with the documents you want to ask about.", details={"deleted": deleted})
    if not_ready := [{"id": str(row["document_id"]), "filename": row["filename"], "status": row["status"]}
                     for row in rows if row["status"] != DocStatus.READY]:
        raise VaultError(ErrorCode.DOCUMENTS_NOT_READY,
                         f"{len(not_ready)} of {len(rows)} documents in this chat are being indexed. "
                         "Ask again when indexing finishes.", details={"not_ready": not_ready})
    first, *rest = (DocRef(DocumentId(row["document_id"]), row["filename"], PageBasis(row["page_basis"]),
                           row["page_count"], tuple(OutlineEntry(entry["title"], entry["level"], entry["page"])
                                                    for entry in row["outline"] or ())) for row in rows)
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
        answer = _answered(row["answer"], row["citations"])
    else:
        answer = NotFound(outcome, row["answer"])
    return Turn(id=TurnId(row["id"]), chat_id=ChatId(row["chat_id"]), question=row["question"],
                standalone_query=row["standalone_query"], answer=answer, started_at=row["started_at"],
                finished_at=row["finished_at"],
                usage=usage_of(row),
                suggestions=tuple(row["suggestions"]), cached=row["cached"])


def _answered(text: str, snapshots: Sequence[Mapping[str, Any]]) -> Answered:
    """An answer from its stored citation snapshots, of which an answered turn always has at least one."""
    first, *rest = (_citation(snapshot) for snapshot in snapshots)
    return Answered(text, (first, *rest))


def _snapshot(citation: Citation) -> dict[str, object]:
    return {"document_id": str(citation.document_id), "filename": citation.filename, "page": citation.page,
            "page_basis": citation.page_basis, "quote": citation.quote, "chunk_id": str(citation.chunk_id)}


def _citation(snapshot: Mapping[str, Any]) -> Citation:
    return Citation(DocumentId(UUID(snapshot["document_id"])), snapshot["filename"], snapshot["page"],
                    PageBasis(snapshot["page_basis"]), snapshot["quote"], ChunkId(UUID(snapshot["chunk_id"])))


def _log_json(log: RetrievalLog) -> dict[str, Any]:
    """The one JSON form of a retrieval log, in chat_turns.retrieval and in a cached answer. Floats are kept as they
    are: jsonb numeric gives Python's repr back exactly, and the eval's gate fit needs the exact similarities."""
    return {**asdict(log), "passages": [
        {**asdict(p), "chunk_id": str(p.chunk_id), "document_id": str(p.document_id)}
        for p in log.passages]}


def _log_of(stored: Mapping[str, Any]) -> RetrievalLog:
    return RetrievalLog(**{**stored, "passages": tuple(
        LoggedPassage(**{**p, "chunk_id": ChunkId(UUID(p["chunk_id"])), "document_id": DocumentId(UUID(p["document_id"])),
                         "kind": PassageKind(p["kind"])}) for p in stored["passages"])})


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
       1.0 / (%(rrf_k)s + vec_rank) + CASE WHEN lex > 0 THEN %(lex_weight)s / (%(rrf_k)s + lex_rank) ELSE 0 END AS rrf
FROM ranked
WHERE vec_rank <= %(n)s OR (lex > 0 AND lex_rank <= %(n)s) OR doc_rank <= %(per_doc)s
ORDER BY rrf DESC
"""


def search(conn: Connection, documents: Sequence[DocRef], q_vec: Vector, query_text: str, embed_model: str, *,
           per_doc: int, k: int) -> Found:
    """Every passage a question can be answered from: retrieve, page_passages and openings, at most one statement
    each."""
    return Found(retrieve(conn, documents, q_vec, query_text, embed_model, per_doc=per_doc),
                 *page_passages(conn, documents, query_text, q_vec, embed_model, k=k),
                 openings(conn, documents, q_vec, embed_model))


def retrieve(conn: Connection, documents: Sequence[DocRef], q_vec: Vector, query_text: str,
             embed_model: str, *, per_doc: int) -> list[Candidate]:
    """Run EXACT_SQL over the chat's documents.

    Only chunks embedded with the current model are searchable, so a model
    switch can never compare vectors from two embedding spaces.
    """
    by_id = {doc.id: doc for doc in documents}
    rows = conn.execute(EXACT_SQL, {"qtext": query_text, "q": PgVector(q_vec), "doc_ids": list(by_id),
                                    "model": embed_model, "rrf_k": RRF_K, "lex_weight": LEXICAL_WEIGHT, "n": CANDIDATES,
                                    "per_doc": per_doc})
    return [Candidate(ChunkId(row["id"]), by_id[row["document_id"]], row["text"], _page_map(row["page_map"]),
                      row["similarity"], float(row["rrf"]), row["lexical_hit"]) for row in rows]


def _page_map(starts: Sequence[Sequence[int]]) -> PageMap:
    return PageMap(tuple((offset, page) for offset, page in starts))


def gate(candidates: Sequence[Candidate | LoggedPassage], min_similarity: float) -> bool:
    """Is there anything worth sending to the answer model?

    True when the best vector similarity reaches min_similarity, or when any
    chunk contains every query term (lexical_hit). The lexical arm keeps exact
    identifiers ("CG 20 10", a policy number) from being refused by a
    semantic threshold. False means nothing matched, and ask refuses with
    NO_EVIDENCE unless the opening passages answer the question. min_similarity
    belongs to the embedding backend because
    similarity scales differ per model.
    """
    return any(candidate.lexical_hit or candidate.similarity >= min_similarity for candidate in candidates)


_DOCUMENT = (r"(?:the|this|that)\s+(?:\w+\s+)?"
             r"(?:document|policy|handbook|manual|guide|quote|endorsement|contract|agreement|report|file)")
# The end of a clause, or a verb of saying: "the end of the policy period", "the policy end date" and "the ending
# balance" are not the end of the document.
_CLAUSE_END = r"(?=\s*(?:[?.!,;:]|$|(?:say|says|read|reads|cover|covers|contain|contains|mention|mentions)\b))"
_PAGE_REFERENCE = re.compile(
    r"\b(?:(?P<first>first|cover|opening)|(?P<last>last|final|closing))\s+page\b"
    rf"|(?P<ending>\bhow\s+does\s+(?:it|{_DOCUMENT})\s+end\b|\bend\s+of\s+{_DOCUMENT}\b|\bthe\s+ending\b)"
    rf"{_CLAUSE_END}"
    r"|\b(?:pages?|pp?\.|pg\.?)\s*(?P<number>[1-9]\d*)"
    r"(?:\s*(?:(?P<through>-|\u2013|to|through)|and)\s*(?P<other>[1-9]\d*))?",
    re.IGNORECASE)


def page_requests(question: str) -> tuple[PageRequest, ...]:
    """The pages a question names, each once, in the order it names them. Pure.

    "first page", "cover page" and "opening page" are page 1; "last page", "final page" and "closing page" are
    page -1, and so is the end of the document: "How does the policy end?", "the end of the handbook", "the
    ending", each ending its clause or followed by a verb such as "say". "When does the policy end?", "the policy
    end date" and "the end of the policy period" are dates.
    "page 45", "p. 45" and "pg 45" name one page, "pages 10 and 11" two, and "pages 10-12", "pages 10 to 12",
    "pages 10 through 12" and "pp. 10-12" a range of at most MAX_PAGE_SPAN pages. Nothing else names a page, so
    "30 days" and "Form CG 21 47" name none.
    """
    found: list[PageRequest] = []
    for match in _PAGE_REFERENCE.finditer(question):
        if match["first"] or match["last"] or match["ending"]:
            end = 1 if match["first"] else -1
            found.append(PageRequest(end, end))
            continue
        number, other = int(match["number"]), int(match["other"] or match["number"])
        if match["through"]:
            low, high = sorted((number, other))
            found.append(PageRequest(low, min(high, low + MAX_PAGE_SPAN - 1)))
        else:
            found += [PageRequest(number, number), PageRequest(other, other)]
    return tuple(dict.fromkeys(found))


_LABEL = (r"(?:\u00a7|(?:sec|para)\.|(?:paragraph|para|section|part|exhibit|appendix|article|schedule|form|chapter"
          r"|clause|endorsement)\b)")
_FORM_NUMBER = r"[A-Z]{1,4}(?:[ -]?\d{2,4})+"                         # "CG 21 47"
_NUMBER = r"\d+(?:\.\d+)*[A-Za-z]{0,2}(?:\([0-9A-Za-z]{1,4}\))*"     # "10", "903D(1)", "9.4.2", "21A"
_IDENTIFIER = rf"(?:{_FORM_NUMBER}|{_NUMBER}|[A-Z]{{1,4}})"           # the last: "IV", "A"
_SECTION_REFERENCE = re.compile(rf"(?<![\w\u00a7])(?i:(?P<label>{_LABEL}))\s*(?P<id>{_IDENTIFIER})(?![\w(])")
_LIST_MARKER = re.compile(r"\(?(?P<id>[0-9A-Za-z]{1,4})[.)](?!\S)")  # "D.", "(1)", "a)"
_BARE_NUMBER = re.compile(rf"(?P<id>{_FORM_NUMBER}|{_NUMBER})(?![\w(])")  # "903 High-Risk Land", "CG 21 47 ..."
_WORDS = re.compile(r"[^\W_]+")
_CANONICAL_LABEL = {"\u00a7": "section", "sec.": "section", "para.": "paragraph", "para": "paragraph"}
_BARE_LABELS = frozenset({"section", "paragraph", "clause"})   # labels a numbered title leaves out: "903 High-Risk Land"
MIN_TITLE_WORDS = 3         # a title written out in a question names its section from this many words


@dataclass(frozen=True, slots=True)
class _Title:
    """An outline title read as the identifier it starts with and the words after it."""

    label: str | None           # "part" for "Part 10: Units"; None for "903 High-Risk Land" or "D. ..."
    parts: tuple[str, ...]      # ("903", "d", "1") for "903D(1)"; a form number is one part
    marker: bool                # "D." or "(1)": names its section only under its parent ("903D")
    words: tuple[str, ...]      # the title after its identifier, as words, compared without case


def _read_title(title: str) -> _Title:
    if found := _SECTION_REFERENCE.match(title):
        return _Title(_label(found["label"]), _parts(found["id"]), False, _words(title[found.end():]))
    for pattern, marker in ((_LIST_MARKER, True), (_BARE_NUMBER, False)):
        if found := pattern.match(title):
            return _Title(None, _parts(found["id"]), marker, _words(title[found.end():]))
    return _Title(None, (), False, _words(title))


def _label(text: str) -> str:
    return _CANONICAL_LABEL.get(text.lower(), text.lower())


def _parts(identifier: str) -> tuple[str, ...]:
    if re.fullmatch(_FORM_NUMBER, identifier):
        return (" ".join(_WORDS.findall(identifier)).casefold(),)
    return tuple(part.casefold() for part in re.findall(r"\d+|[A-Za-z]+", identifier))


def _words(text: str) -> tuple[str, ...]:
    return tuple(word.casefold() for word in _WORDS.findall(text))


def section_requests(question: str, outline: Sequence[OutlineEntry], page_count: int) -> tuple[SectionRequest, ...]:
    """The sections of one document a question names, each once, in the order it names them. Pure.

    A section is named by a label and an identifier ("Exhibit 23", "Part 10", "section 903", "Para. 903D(1)",
    "Form CG 21 47") or by writing out a title of at least MIN_TITLE_WORDS words after its identifier ("the High-Risk
    Land Exclusion Option"). A labelled identifier matches a title that starts with the same label, or, for section,
    paragraph, clause and form numbers, with the identifier alone ("903 High-Risk Land ..."). Its later parts then walk
    down the outline: "903D(1)" is the "D." under "903 ..." when there is no "(1)" under that. The match that uses the
    most of the reference wins, then a labelled title, then an entry over one it sits under. A reference that matches
    nothing, or two unrelated entries equally well ("section 1" in a document with a Section 1 in every Part), names
    nothing: the answer would be a guess.

    A section runs from its entry's page to the page where the next entry at its level or above starts, which is
    often the page the section ends on, or to the last page.
    """
    if not outline:
        return ()
    titles = [_read_title(entry.title) for entry in outline]
    named = [(found.start(), index) for found in _SECTION_REFERENCE.finditer(question)
             if (index := _numbered(found, outline, titles)) is not None]
    named += _written_out(question, outline, titles)
    return tuple(dict.fromkeys(_section(outline, index, page_count) for _, index in sorted(named)))


def _numbered(reference: re.Match[str], outline: Sequence[OutlineEntry], titles: Sequence[_Title]) -> int | None:
    label, parts = _label(reference["label"]), _parts(reference["id"])
    bare = label in _BARE_LABELS or bool(re.fullmatch(_FORM_NUMBER, reference["id"]))
    scores: dict[int, tuple[int, bool]] = {}
    for index, title in enumerate(titles):
        allowed = title.label == label if title.label is not None else bare
        if title.marker or not title.parts or parts[:len(title.parts)] != title.parts or not allowed:
            continue
        node, used = index, len(title.parts)
        while used < len(parts) and (child := _child(outline, titles, node, parts[used])) is not None:
            node, used = child, used + 1
        scores[node] = max(scores.get(node, (0, False)), (used, title.label is not None))
    return _best(scores, outline)


def _child(outline: Sequence[OutlineEntry], titles: Sequence[_Title], parent: int, part: str) -> int | None:
    """The one entry directly under outline[parent] whose title starts with `part` alone ("D." under "903 ...")."""
    level = outline[parent].level
    below = list(itertools.takewhile(lambda at: outline[at].level > level, range(parent + 1, len(outline))))
    top = min((outline[at].level for at in below), default=level)
    found = [at for at in below if outline[at].level == top and titles[at].label is None and titles[at].parts == (part,)]
    return found[0] if len(found) == 1 else None


def _written_out(question: str, outline: Sequence[OutlineEntry], titles: Sequence[_Title]) -> list[tuple[int, int]]:
    """(where in the question, outline index) of each title the question writes out. A longer title wins the words
    it shares with a shorter one; one written out for several entries names none of them."""
    found = [(word.start(), word.group().casefold()) for word in _WORDS.finditer(question)]
    words = [word for _, word in found]
    spans: dict[tuple[int, int], dict[int, int]] = {}   # (first word, words) -> {outline index: score}
    for index, title in enumerate(titles):
        size = len(title.words)
        if size >= MIN_TITLE_WORDS:
            for start in range(len(words) - size + 1):
                if tuple(words[start:start + size]) == title.words:
                    spans.setdefault((start, size), {})[index] = 0
    taken: set[int] = set()
    named: list[tuple[int, int]] = []
    for start, size in sorted(spans, key=lambda span: (-span[1], span[0])):
        if taken.isdisjoint(range(start, start + size)):
            taken.update(range(start, start + size))
            if (index := _best(spans[start, size], outline)) is not None:
                named.append((found[start][0], index))
    return named


def _best(scores: Mapping[int, Any], outline: Sequence[OutlineEntry]) -> int | None:
    """The outline index with the highest score, or None when there is none or entries in different branches tie.
    Of an entry and one under it, the one under it wins."""
    top = [index for index, score in scores.items() if score == max(scores.values())]
    best = [index for index in top if not any(_under(outline, other, index) for other in top)]
    return best[0] if len(best) == 1 else None


def _under(outline: Sequence[OutlineEntry], child: int, parent: int) -> bool:
    return parent < child and all(entry.level > outline[parent].level for entry in outline[parent + 1:child + 1])


def _section(outline: Sequence[OutlineEntry], index: int, page_count: int) -> SectionRequest:
    entry = outline[index]
    end = next((later.page for later in outline[index + 1:] if later.level <= entry.level), page_count)
    return SectionRequest(entry.title, entry.page, max(entry.page, end))


PAGES_SQL = """
WITH wanted AS (
  SELECT * FROM unnest(%(doc_ids)s::uuid[], %(firsts)s::int[], %(lasts)s::int[], %(takes)s::int[])
    WITH ORDINALITY AS w(document_id, first_page, last_page, take, at)
), hits AS (
  SELECT w.at, w.take, c.id, c.ord, row_number() OVER (PARTITION BY w.at ORDER BY c.ord) AS n
  FROM wanted w JOIN chunks c ON c.document_id = w.document_id
  WHERE c.embedding_model = %(model)s
    AND EXISTS (SELECT 1 FROM jsonb_array_elements(c.page_map) AS s(start)
                WHERE (s.start ->> 1)::int BETWEEN w.first_page AND w.last_page)
)
SELECT hits.at, c.id, c.text, c.page_map, 1 - (c.embedding <=> %(q)s) AS similarity
FROM hits JOIN chunks c USING (id)
WHERE hits.n <= hits.take
ORDER BY hits.at, c.ord
"""


def page_passages(conn: Connection, documents: Sequence[DocRef], question: str, q_vec: Vector, embed_model: str, *,
                  k: int) -> tuple[list[Candidate], list[Candidate]]:
    """The parts of chunks on the pages the question names by number, and the parts that open the sections it names,
    each in scope order, then in the order the question names them, then in reading order.

    Each request gets the first PAGE_PASSAGES_PER_REQUEST chunks holding its pages, so "page 2 and the last page"
    sends both; a document sends at most PAGE_PASSAGES_PER_DOC, and the chat at most k in all. Each part is cut to
    its request's pages (pages.part_on_pages) and keeps its chunk's id, so the model reads "p. 7 of 7" rather than
    a chunk that starts on page 5. Neighbouring chunks overlap, so a part whose text another part of the same
    document holds whole is dropped. Only PDFs have numbered pages: the sections of a Word or text document are cut
    by length, so "page 3" names nothing in one, though its last section is still how it ends. A page past a
    document's last page names nothing in it.

    A section's parts start with the chunk that holds its title (pages.has_title), found among the first
    TITLE_LOOKAHEAD more: a section often starts mid-page, after the end of the one before. Word headings name
    sections too.
    """
    numbered = page_requests(question)
    wanted: list[tuple[DocRef, int, int, str | None]] = []
    for doc in documents:
        wanted += [(doc, *span, None) for request in numbered
                   if (doc.page_basis is PageBasis.PHYSICAL or request.first < 0)
                   and (span := request.resolve(doc.page_count))]
        wanted += [(doc, section.first, section.last, section.title)
                   for section in section_requests(question, doc.outline, doc.page_count)]
    if not wanted:
        return [], []
    rows = conn.execute(PAGES_SQL, {"q": PgVector(q_vec), "doc_ids": [doc.id for doc, *_ in wanted],
                                    "firsts": [first for _, first, _, _ in wanted],
                                    "lasts": [last for _, _, last, _ in wanted],
                                    "takes": [PAGE_PASSAGES_PER_REQUEST + (0 if title is None else TITLE_LOOKAHEAD)
                                              for *_, title in wanted],
                                    "model": embed_model})
    parts: list[tuple[Candidate, bool]] = []
    for at, group in itertools.groupby(rows, key=lambda row: row["at"]):
        doc, first, last, title = wanted[at - 1]
        cut = [Candidate(ChunkId(row["id"]), doc, *part, row["similarity"], 0.0, False) for row in group
               if (part := pages.part_on_pages(row["text"], _page_map(row["page_map"]), first, last))]
        if title is not None:
            cut = cut[next((n for n, part in enumerate(cut) if pages.has_title(title, part.text)), 0):]
        parts += [(part, title is not None) for part in cut[:PAGE_PASSAGES_PER_REQUEST]]
    taken: Counter[DocumentId] = Counter()
    on_pages: list[Candidate] = []
    in_sections: list[Candidate] = []
    for (part, in_section), held in zip(parts, _held([part for part, _ in parts]), strict=True):
        if not held and taken[part.doc.id] < PAGE_PASSAGES_PER_DOC and taken.total() < k:
            taken[part.doc.id] += 1
            (in_sections if in_section else on_pages).append(part)
    return on_pages, in_sections


def _held(passages: Sequence[Candidate]) -> list[bool]:
    """For each passage, whether another passage of the same document holds its text; of equal texts, all but the
    first are held."""
    return [any(other.doc.id == passage.doc.id and passage.text in other.text
                and (len(other.text) > len(passage.text) or there < at)
                for there, other in enumerate(passages) if there != at)
            for at, passage in enumerate(passages)]


# One index lookup per document on (document_id, ord), so no other chunk is read, sorted or scored.
OPENINGS_SQL = """
SELECT c.id, c.document_id, c.text, c.page_map, 1 - (c.embedding <=> %(q)s) AS similarity
FROM unnest(%(doc_ids)s::uuid[]) AS d(id)
CROSS JOIN LATERAL (SELECT id, document_id, text, page_map, embedding FROM chunks
                    WHERE document_id = d.id AND embedding_model = %(model)s ORDER BY ord LIMIT 1) c
"""


def openings(conn: Connection, documents: Sequence[DocRef], q_vec: Vector, embed_model: str) -> list[Candidate]:
    """Each document's opening passage, its first chunk, in scope order.

    The opening holds the title, cover or declarations page, which say what the document is. A question about
    the document as a whole ("What is this about?") scores as low against it as an off-topic question does,
    so retrieval does not find it, and the answer step gets it from here instead. A question about how a document
    ends names its last page (page_requests).
    """
    rows = conn.execute(OPENINGS_SQL, {"q": PgVector(q_vec), "doc_ids": [doc.id for doc in documents],
                                       "model": embed_model})
    by_doc = {row["document_id"]: row for row in rows}
    return [Candidate(ChunkId(row["id"]), doc, row["text"], _page_map(row["page_map"]), row["similarity"], 0.0, False)
            for doc in documents if (row := by_doc.get(doc.id)) is not None]


def select_sources(candidates: Sequence[Candidate], *, k: int, per_doc_min: int, on_pages: Sequence[Candidate] = (),
                   in_sections: Sequence[Candidate] = (), openings: Sequence[Candidate] = ()) -> list[Source]:
    """Choose the sources the model sees, labelled S1, S2, ... in this order: the passages on the pages the
    question names, the passages opening the sections it names, then retrieved candidates in fused (rrf) order up
    to k in all, then the opening passages.

    The page and section passages are what the user asked for, so they lead, and each takes the place of a
    retrieved one, so naming a page or a section never makes the prompt bigger. A chunk a page or section passage
    holds whole is not sent again; one cut to the pages asked about still is, so its other pages stay in the
    prompt. Of the other candidates, each document is first guaranteed its best chunks by rrf, up to per_doc_min,
    then the rest fill to k by rrf. "Compare the deductibles in these three
    quotes" then sees all three quotes even when one dominates the scores. The
    guarantees take at most half of the k slots, so each document gets fewer as
    the chat holds more documents, and none once there are more than k / 2. A
    document's strong second passage is then never crowded out by many weak
    first ones. With one document this is the top k. An exact rrf tie goes to
    the higher similarity.

    In a chat of up to OPENING_DOCS documents, each opening passage not already
    chosen follows the k, so a question about a document as a whole sees its
    title page. A bigger chat sends none: one per document would outgrow the k.
    """
    return _labelled(_pick(candidates, k=k, per_doc_min=per_doc_min, on_pages=on_pages, in_sections=in_sections,
                           openings=openings))


def _pick(candidates: Sequence[Candidate], *, k: int, per_doc_min: int, on_pages: Sequence[Candidate] = (),
          in_sections: Sequence[Candidate] = (), openings: Sequence[Candidate] = ()) -> list[Candidate]:
    named = [*on_pages, *in_sections]
    whole = {(passage.chunk_id, passage.text) for passage in named}
    ranked = _fused(candidate for candidate in candidates if (candidate.chunk_id, candidate.text) not in whole)
    room = max(k - len(named), 0)
    quota = min(per_doc_min, room // 2 // max(1, len({candidate.doc.id for candidate in ranked})))
    taken: Counter[DocumentId] = Counter()
    rounds: list[tuple[int, int]] = []
    for at, candidate in enumerate(ranked):
        rounds.append((min(taken[candidate.doc.id], quota), at))
        taken[candidate.doc.id] += 1
    picked = [*named, *(ranked[at] for at in sorted(at for _, at in sorted(rounds)[:room]))]
    if len(openings) <= OPENING_DOCS:
        held = {(candidate.chunk_id, candidate.text) for candidate in picked}
        picked += [opening for opening in openings if (opening.chunk_id, opening.text) not in held]
    return picked


def _fused(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Best first by fused score; an exact rrf tie goes to the higher similarity."""
    return sorted(candidates, key=lambda candidate: (candidate.rrf, candidate.similarity), reverse=True)


def _labelled(candidates: Sequence[Candidate]) -> list[Source]:
    return [Source(f"S{n}", c.chunk_id, c.doc, c.text, c.page_map, c.similarity, c.rrf)
            for n, c in enumerate(candidates, start=1)]


def retrieval_log(query: str, found: Found, picked: Sequence[Candidate], *, min_similarity: float,
                  k: int) -> RetrievalLog:
    """What one question's search found and which passages its answer step was sent, picked[n - 1] as S{n}. Pure.

    The log keeps every sent passage, the top 2k retrieved in fused order (_fused, as select_sources ranks
    them), and the passages the gate decided on: the retrieved passage with the highest similarity and the best-ranked
    one that matches every query term, so the gate says the same over the logged passages as over all of them.
    Everything else, an opening that was not sent included, is only counted in omitted. Retrieved passages come first in
    rank order, then the page, section and opening passages in the order search found them.
    """
    # By identity: a page part and an opening can be equal by value while only one of them was sent.
    labels = {id(passage): f"S{n}" for n, passage in enumerate(picked, start=1)}
    ranked = _fused(found.retrieved)
    gate_evidence = [max(ranked, key=lambda candidate: candidate.similarity)] if ranked else []
    gate_evidence += [candidate for candidate in ranked if candidate.lexical_hit][:1]
    kept = {*labels, *(id(candidate) for candidate in [*ranked[:2 * k], *gate_evidence])}
    groups = ((PassageKind.RETRIEVED, ranked), (PassageKind.PAGE, found.on_pages),
              (PassageKind.SECTION, found.in_sections), (PassageKind.OPENING, found.openings))
    passages = tuple(
        LoggedPassage(c.chunk_id, c.doc.id, kind, c.page_map.first_page, c.page_map.last_page, c.similarity, c.rrf,
                      c.lexical_hit, rank if kind is PassageKind.RETRIEVED else None, labels.get(id(c)))
        for kind, group in groups for rank, c in enumerate(group, start=1) if id(c) in kept)
    return RetrievalLog(query, found.matched(min_similarity), min_similarity, k, passages,
                        sum(len(group) for _, group in groups) - len(passages))


def suggestions(follow_ups: Sequence[str], question: str) -> tuple[str, ...]:
    """The follow-up questions worth offering under a turn. Pure.

    Keeps, in order, at most MAX_SUGGESTIONS questions that end in "?" and are
    at most MAX_SUGGESTION_CHARS long. Blanks, repeats and the question just
    asked (compared without case or extra spaces) are dropped.
    """
    seen = {_same_question(question)}
    kept: list[str] = []
    for raw in follow_ups:
        text = pages.collapse_whitespace(raw)
        key = text.casefold()
        if text.endswith("?") and len(text) <= MAX_SUGGESTION_CHARS and key not in seen:
            seen.add(key)
            kept.append(text)
    return tuple(kept[:MAX_SUGGESTIONS])


def fact_suggestions(labels: Sequence[Sequence[str]], question: str) -> tuple[str, ...]:
    """Suggestions for a no_evidence turn, whose model suggestions, if any, go with its answer. Pure and deterministic.

    "What is the <label>?" from each document's verified key-fact labels, taken
    round by round (every document's first label, then every second one), so a
    multi-document chat suggests something from each. A label is answerable by
    construction: its quote was found on the page it cites. Labels of one word
    ("Insurer") are skipped, because so short a question also matches every
    passage that mentions the word.
    """
    rounds = itertools.zip_longest(*([label for label in doc if len(label.split()) >= 2] for doc in labels))
    return suggestions([f"What is the {label}?" for row in rounds for label in row if label], question)


def _same_question(text: str) -> str:
    return pages.collapse_whitespace(text).casefold()


# ------------------------------------------------------------------ grounding

# A run of adjacent markers ("[E2]", "[E2][E3]", "[E2, E3]") and the spaces before it. A source marker such as
# [S2], the old form, matches too and cites nothing, so a model that slips into it leaves no stray label.
_MARKERS = re.compile(r"(?P<pad>[ \t]*)(?P<run>(?:\[[ES]\d+(?:\s*,\s*[ES]\d+)*\])+)")
_EVIDENCE_ID = re.compile(r"E\d+")


def ground(draft: AnswerDraft, sources: Sequence[Source]) -> Answer:
    """Turn the model's draft into an Answer the user can trust. Pure.

    An evidence quote is kept only when pages.locate_quote finds it in the
    source its label names; a paraphrase, a fabrication or an unknown label is
    dropped. A kept citation quotes the source span, not the model's copy, and
    takes the page where that span starts inside the chunk, so a chunk spanning
    pages 11-12 cites 12 when the quoted words are on 12. Citations are numbered
    1..n in evidence order, a repeated quote keeping its first number, and each
    [E#] marker becomes the numbers of the evidence items with that id.
    Nothing verified -> UNVERIFIED.
    """
    if not draft.supported:
        return NotFound(TurnOutcome.MODEL_DECLINED, _rewrite_markers(draft.answer, {}))
    by_label = {source.label: source for source in sources}
    citations: list[Citation] = []
    numbers: dict[tuple[str, int, int], int] = {}
    refs: dict[str, list[int]] = {}
    for evidence in draft.evidence:
        source = by_label.get(evidence.source)
        span = None if source is None else pages.locate_quote(evidence.quote, source.text)
        if span is None:
            continue
        quoted = (evidence.source, *span)
        if quoted not in numbers:
            start, end = span
            citations.append(Citation(source.doc.id, source.doc.filename, source.page_map.page_at(start),
                                      source.doc.page_basis, pages.collapse_whitespace(source.text[start:end]),
                                      source.chunk_id))
            numbers[quoted] = len(citations)
        refs.setdefault(_evidence_key(evidence.id), []).append(numbers[quoted])
    if not citations:
        return NotFound(TurnOutcome.UNVERIFIED, UNVERIFIED_TEXT)
    return Answered(_rewrite_markers(draft.answer, refs), (citations[0], *citations[1:]))


def _evidence_key(evidence_id: str) -> str:
    """ "E1" for "E1", "[E1]" or " e1": the id as the answer's markers write it. Any other id matches no marker."""
    found = _EVIDENCE_ID.search(evidence_id.upper())
    return found.group(0) if found else evidence_id


def _rewrite_markers(text: str, refs: Mapping[str, Sequence[int]]) -> str:
    """Each marker run becomes "[1][2]" for its evidence ids' verified citations. A run with none is removed with the
    spaces before it, so "limit [E9]." reads "limit." rather than "limit ."."""
    def replace(match: re.Match[str]) -> str:
        cited = dict.fromkeys(ref for evidence_id in _EVIDENCE_ID.findall(match["run"])
                              for ref in refs.get(evidence_id, ()))
        return match["pad"] + "".join(f"[{ref}]" for ref in cited) if cited else ""

    return _MARKERS.sub(replace, text).strip()
