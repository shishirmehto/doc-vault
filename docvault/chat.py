"""Chat over a fixed set of documents: start, ask, history.

Owns chats, chat_documents and chat_turns. ask() is the whole RAG path in
one function a reader can follow top to bottom:

  question -> condense with history -> retrieve (hybrid, scoped, per-document quota)
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
    VaultError,
)
from .prompts import AnswerDraft, CondensedQuery

type Stage = Literal["condensing", "searching", "answering"]

RRF_K = 60
CANDIDATES = 50
MAX_SUGGESTIONS = 3
MAX_SUGGESTION_CHARS = 160
UNVERIFIED_TEXT = ("I found passages that look related, but I could not verify an answer against their text, "
                   "so I am not giving one.")
OPENING_DOCS = 3            # a chat of up to this many documents sends each one's opening passage with its k sources
ANSWER_CACHE_FORMAT = 3     # part of every answer key: a change to the cached value's shape or to select_sources bumps it
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
    lexical_hit: bool       # chunk.tsv matched websearch_to_tsquery(question) (all terms present)


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
    turn marked cached. Only answered turns go into the cache: a refusal asked again should
    get a fresh attempt from a live model, and a cached refusal would pin it until the prompt
    version changes. A follow-up depends on the turns before it, so it is never looked up or stored.

    Over today's AI spending cap it raises VaultError(QUOTA_EXCEEDED) (HTTP 429) before
    any model call. A cache hit still answers.

    When the gate finds nothing, the answer step sees only each document's opening passage, if
    the backend declines what its sources do not answer. That answers a question about the
    document as a whole; anything else is refused with NO_EVIDENCE, and that call's text is not
    streamed, so a decline never shows before the refusal replaces it.

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
        documents = _scope(conn, chat_id)
        past = _turns(conn, chat_id, limit=ctx.settings.history_turns)
        key = None if past else answer_key(ctx, _hashes(conn, documents), question)
    if key is not None and (hit := _cached_answer(ctx.cache, key)) is not None:
        answer, suggested = hit
        if on_text is not None:
            on_text(prompts.REF_MARKER.sub("", answer.text))
        return _store_turn(ctx, turn_id, chat_id, question, question, answer, suggested, started, cached=True)
    ctx.ai.check_budget(Purpose.ANSWER)

    if past:
        stage("condensing")
    standalone = question if not past else ctx.ai.complete(
        prompts.condense(past, question), CondensedQuery, owner=owner).standalone
    stage("searching")
    [q_vec] = ctx.ai.embed([standalone], purpose=Purpose.EMBED_QUERY, owner=owner)
    with ctx.db.connection() as conn:
        candidates = retrieve(conn, documents, q_vec, standalone, ctx.ai.embed_model,
                              per_doc=ctx.settings.retrieval_per_doc_min)
        first = openings(conn, documents, q_vec, ctx.ai.embed_model)
    matched = gate(candidates, ctx.ai.min_similarity)
    fallback = first[:ctx.settings.retrieval_k] if ctx.ai.chat.declines_unanswerable else []
    sources = select_sources(candidates, k=ctx.settings.retrieval_k, per_doc_min=ctx.settings.retrieval_per_doc_min,
                             openings=first) if matched else _labelled(fallback)

    answer: Answer | None = None
    suggested: tuple[str, ...] = ()
    if sources:
        stage("answering")
        provisional = _ProvisionalAnswer(on_text, on_reset or _ignore) if matched and on_text is not None else None
        draft = ctx.ai.complete(prompts.answer(question, standalone, past, sources), AnswerDraft, owner=owner,
                                stream=provisional)
        answer = ground(draft, sources)
        suggested = suggestions(draft.follow_ups, question)
    if answer is None or not (matched or isinstance(answer, Answered)):
        answer = NotFound(TurnOutcome.NO_EVIDENCE, prompts.no_evidence_text(documents))
        with ctx.db.connection() as conn:
            facts = insights.ready_key_facts(conn, [doc.id for doc in documents])
        suggested = fact_suggestions([[fact.label for fact in facts.get(doc.id, ())] for doc in documents], question)

    turn = _store_turn(ctx, turn_id, chat_id, question, standalone, answer, suggested, started, cached=False)
    if key is not None and isinstance(answer, Answered):
        _cache_answer(ctx.cache, key, answer, suggested)
    return turn


def answer_key(ctx: Ctx, documents: Mapping[DocumentId, bytes], question: str) -> str:
    """The answer cache key for a first question over `documents` (id -> sha256): a hash of every input the
    answer depends on, so a change to any of them is a miss. The question is compared without case or extra spaces."""
    inputs = {
        "format": ANSWER_CACHE_FORMAT,
        "documents": sorted([str(doc_id), sha.hex()] for doc_id, sha in documents.items()),
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


def _hashes(conn: Connection, documents: Sequence[DocRef]) -> dict[DocumentId, bytes]:
    rows = conn.execute("SELECT id, sha256 FROM documents WHERE id = ANY(%s)", ([doc.id for doc in documents],))
    return {DocumentId(row["id"]): bytes(row["sha256"]) for row in rows}


def _cached_answer(cache: Cache, key: str) -> tuple[Answered, tuple[str, ...]] | None:
    """The stored answer and suggestions, counting one hit or one miss."""
    [value] = cache.get_many([key])
    cache.count("answer", hits=int(value is not None), misses=int(value is None))
    if value is None:
        return None
    stored = json.loads(value)
    return _answered(stored["text"], stored["citations"]), tuple(stored["suggestions"])


def _cache_answer(cache: Cache, key: str, answer: Answered, suggested: Sequence[str]) -> None:
    cache.set_many({key: json.dumps({"text": answer.text, "citations": [_snapshot(c) for c in answer.citations],
                                     "suggestions": list(suggested)}).encode()}, ANSWER_TTL_S)


def _store_turn(ctx: Ctx, turn_id: TurnId, chat_id: ChatId, question: str, standalone: str, answer: Answer,
                suggested: Sequence[str], started: datetime, *, cached: bool) -> Turn:
    with ctx.db.connection() as conn:
        conn.execute(
            "INSERT INTO chat_turns (id, chat_id, question, standalone_query, outcome, answer, citations,"
            " suggestions, cached, started_at, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (turn_id, chat_id, question, standalone, answer.outcome, answer.text,
             Jsonb([_snapshot(citation) for citation in answer.citations]), Jsonb(list(suggested)), cached, started,
             datetime.now(UTC)))
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


# ------------------------------------------------------------------ rows

# Usage is read back from the ledger rather than kept on the turn, so it counts every attempt the gateway made.
_TURN_SQL = f"""
SELECT t.id, t.chat_id, t.question, t.standalone_query, t.outcome, t.answer, t.citations, t.suggestions,
       t.cached, t.started_at, t.finished_at, {USAGE_SQL}
FROM chat_turns t LEFT JOIN llm_calls l ON l.turn_id = t.id
"""


# A chat with its scope in the fixed order _scope uses. A document deleted since the chat started has no
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


def _scope(conn: Connection, chat_id: ChatId) -> tuple[DocRef, *tuple[DocRef, ...]]:
    """The chat's documents in one fixed order, for the API body and the refusal text.

    Raises DOCUMENTS_DELETED when any of them was deleted after the chat started:
    answering from the rest would quietly change what the chat was asked about.
    """
    rows = conn.execute(
        "SELECT cd.document_id, cd.filename, d.page_basis FROM chat_documents cd"
        " LEFT JOIN documents d ON d.id = cd.document_id WHERE cd.chat_id = %s ORDER BY cd.filename, cd.document_id",
        (chat_id,)).fetchall()
    if deleted := [{"id": str(row["document_id"]), "filename": row["filename"], "status": "deleted"} for row in rows
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
    semantic threshold. False means nothing matched, and ask refuses with
    NO_EVIDENCE unless the opening passages answer the question. min_similarity
    belongs to the embedding backend because
    similarity scales differ per model.
    """
    return any(candidate.lexical_hit or candidate.similarity >= min_similarity for candidate in candidates)


OPENINGS_SQL = """
SELECT DISTINCT ON (document_id) id, document_id, text, page_map, 1 - (embedding <=> %(q)s) AS similarity
FROM chunks
WHERE document_id = ANY(%(doc_ids)s) AND embedding_model = %(model)s
ORDER BY document_id, ord
"""


def openings(conn: Connection, documents: Sequence[DocRef], q_vec: Vector, embed_model: str) -> list[Candidate]:
    """Each document's opening passage, its first chunk, in scope order.

    The opening holds the title, cover or declarations page, which say what the document is. A question about
    the document as a whole ("What is this about?") scores as low against it as an off-topic question does,
    so retrieval does not find it, and the answer step gets it from here instead.
    """
    rows = conn.execute(OPENINGS_SQL, {"q": PgVector(q_vec), "doc_ids": [doc.id for doc in documents],
                                       "model": embed_model})
    by_doc = {row["document_id"]: row for row in rows}
    return [Candidate(ChunkId(row["id"]), doc, row["text"],
                      PageMap(tuple((offset, page) for offset, page in row["page_map"])), row["similarity"], 0.0, False)
            for doc in documents if (row := by_doc.get(doc.id)) is not None]


def select_sources(candidates: Sequence[Candidate], *, k: int, per_doc_min: int,
                   openings: Sequence[Candidate] = ()) -> list[Source]:
    """Choose the k sources the model sees, labelled S1..Sk in fused (rrf) order, then the opening passages.

    Each document is first guaranteed its best chunks by rrf, up to per_doc_min,
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
    ranked = sorted(candidates, key=lambda candidate: (candidate.rrf, candidate.similarity), reverse=True)
    quota = min(per_doc_min, k // 2 // max(1, len({candidate.doc.id for candidate in candidates})))
    taken: Counter[DocumentId] = Counter()
    rounds: list[tuple[int, int]] = []
    for at, candidate in enumerate(ranked):
        rounds.append((min(taken[candidate.doc.id], quota), at))
        taken[candidate.doc.id] += 1
    picked = [ranked[at] for at in sorted(at for _, at in sorted(rounds)[:k])]
    if len(openings) <= OPENING_DOCS:
        held = {candidate.chunk_id for candidate in picked}
        picked += [opening for opening in openings if opening.chunk_id not in held]
    return _labelled(picked)


def _labelled(candidates: Sequence[Candidate]) -> list[Source]:
    return [Source(f"S{n}", c.chunk_id, c.doc, c.text, c.page_map, c.similarity, c.rrf)
            for n, c in enumerate(candidates, start=1)]


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
    """Suggestions for a turn the gate refused, so no model wrote any. Pure and deterministic.

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
