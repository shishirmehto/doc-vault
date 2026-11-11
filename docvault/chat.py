"""Chat over a fixed set of documents: start, ask, history.

Owns chats, chat_documents and chat_turns. ask() is the whole RAG path in
one function a reader can follow top to bottom:

  question -> condense with history -> retrieve (hybrid, scoped, per-document quota)
           -> gate -> answer prompt -> ground (verify quotes, resolve pages) -> suggest -> persist turn

Pure decisions (select_sources, gate, ground, suggestions) are separate functions with
no I/O, so the grounding and refusal rules are unit-tested without a model
or a database. A caller that shows progress (the stream route) passes sinks
to ask(); the pipeline is the same with or without them.
"""

from __future__ import annotations

import itertools
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

from . import pages, prompts
from .ai import TurnOwner, Vector
from .answer_text import AnswerField, MarkerFilter
from .context import Ctx
from .db import Db, decode_cursor, encode_cursor
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

type Stage = Literal["condensing", "searching", "answering"]

RRF_K = 60
CANDIDATES = 50
MAX_SUGGESTIONS = 3
MAX_SUGGESTION_CHARS = 160
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


def ready_documents(conn: Connection, document_ids: Sequence[DocumentId]) -> list[DocRef]:
    """The documents in the order given, each held FOR SHARE until the caller's transaction ends.

    Unknown ids -> DOCUMENT_NOT_FOUND (404) listing them.
    Any id not READY -> DOCUMENTS_NOT_READY (409) listing id, filename and status.
    """
    # SHARE holds off a delete or a status change between this check and what the caller's transaction does next.
    found = {row["id"]: row for row in conn.execute(
        "SELECT id, filename, status, page_basis FROM documents WHERE id = ANY(%s) FOR SHARE", (list(document_ids),))}
    if missing := [doc_id for doc_id in document_ids if doc_id not in found]:
        raise documents_not_found(missing, len(document_ids))
    if not_ready := [{"id": str(doc_id), "filename": found[doc_id]["filename"], "status": found[doc_id]["status"]}
                     for doc_id in document_ids if found[doc_id]["status"] != DocStatus.READY]:
        raise VaultError(ErrorCode.DOCUMENTS_NOT_READY,
                         f"{len(not_ready)} of {len(document_ids)} documents are not ready.",
                         details={"not_ready": not_ready})
    return [DocRef(doc_id, found[doc_id]["filename"], PageBasis(found[doc_id]["page_basis"])) for doc_id in document_ids]


def documents_not_found(missing: Sequence[DocumentId], total: int) -> VaultError:
    """DOCUMENT_NOT_FOUND (404) for the ids of `total` that do not exist, listing them."""
    return VaultError(ErrorCode.DOCUMENT_NOT_FOUND, f"{len(missing)} of {total} documents do not exist.",
                      details={"missing": [str(doc_id) for doc_id in missing]})


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
    last = rows[limit - 1] if len(rows) > limit else None
    return [_row_to_chat(row) for row in rows[:limit]], last and encode_cursor(last["created_at"], last["id"])


def ask(ctx: Ctx, chat_id: ChatId, question: str, *, on_stage: Callable[[Stage], None] | None = None,
        on_text: Callable[[str], None] | None = None, on_reset: Callable[[], None] | None = None) -> Turn:
    """Answer one question with citations, or refuse honestly. Persists exactly one turn.

    Every model call is billed to the turn id, which exists before the first
    call. A provider failure raises VaultError(AI_UNAVAILABLE) (HTTP 503, with
    Retry-After when retryable) and writes no turn; the failed calls stay in llm_calls. No
    connection or transaction is held across a model call.

    Over today's AI spending cap it raises VaultError(QUOTA_EXCEEDED) (HTTP 429) before
    any model call, even one the gate would have saved.

    The optional sinks report progress. on_stage hears each step as it starts, after the
    chat, its scope and the spending cap are checked, so a missing chat, a deleted document
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

    answer: Answer
    if gate(candidates, ctx.ai.min_similarity):
        sources = select_sources(candidates, k=ctx.settings.retrieval_k, per_doc_min=ctx.settings.retrieval_per_doc_min)
        stage("answering")
        draft = ctx.ai.complete(prompts.answer(question, standalone, past, sources), AnswerDraft, owner=owner,
                                stream=None if on_text is None else _ProvisionalAnswer(on_text, on_reset or _ignore))
        answer = ground(draft, sources)
        suggested = suggestions(draft.follow_ups, question)
    else:
        answer = NotFound(TurnOutcome.NO_EVIDENCE, prompts.no_evidence_text(documents))
        with ctx.db.connection() as conn:
            suggested = fact_suggestions(_key_fact_labels(conn, documents), question)

    with ctx.db.connection() as conn:
        conn.execute(
            "INSERT INTO chat_turns (id, chat_id, question, standalone_query, outcome, answer, citations,"
            " suggestions, started_at, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (turn_id, chat_id, question, standalone, answer.outcome, answer.text,
             Jsonb([_snapshot(citation) for citation in answer.citations]), Jsonb(list(suggested)), started,
             datetime.now(UTC)))
        return _row_to_turn(conn.execute(_TURN_SQL + "WHERE t.id = %s GROUP BY t.id", (turn_id,)).fetchone())


def _ignore(*_: object) -> None:
    pass


class _ProvisionalAnswer:
    """The ai.TextSink for a streamed answer: the JSON's answer field with its [S#] markers removed.

    Each provider attempt starts afresh. When the attempt before it had already sent
    text, the caller hears on_reset first, so text from a failed or repaired attempt
    never mixes with the next one.
    """

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
# count(cost_usd) skips NULLs: when it falls short of count(id), some call was unpriced and the total is unknown.
_TURN_SQL = """
SELECT t.id, t.chat_id, t.question, t.standalone_query, t.outcome, t.answer, t.citations, t.suggestions,
       t.started_at, t.finished_at, count(l.id) AS calls, coalesce(sum(l.input_tokens), 0) AS input_tokens,
       coalesce(sum(l.output_tokens), 0) AS output_tokens,
       CASE WHEN count(l.cost_usd) = count(l.id) THEN coalesce(sum(l.cost_usd), 0) END AS cost_usd
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
        citations = [_citation(snapshot) for snapshot in row["citations"]]
        answer = Answered(row["answer"], (citations[0], *citations[1:]))
    else:
        answer = NotFound(outcome, row["answer"])
    return Turn(id=TurnId(row["id"]), chat_id=ChatId(row["chat_id"]), question=row["question"],
                standalone_query=row["standalone_query"], answer=answer, started_at=row["started_at"],
                finished_at=row["finished_at"],
                usage=Usage(row["calls"], row["input_tokens"], row["output_tokens"], row["cost_usd"]),
                suggestions=tuple(row["suggestions"]))


def _key_fact_labels(conn: Connection, documents: Sequence[DocRef]) -> list[list[str]]:
    """The labels of each document's verified key facts, in scope order. A document without ready insights has none."""
    rows = conn.execute("SELECT document_id, body FROM document_insights WHERE document_id = ANY(%s)"
                        " AND status = 'ready'", ([doc.id for doc in documents],)).fetchall()
    labels = {row["document_id"]: [fact["label"] for fact in row["body"]["key_facts"]] for row in rows}
    return [labels.get(doc.id, []) for doc in documents]


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


# ------------------------------------------------------------------ suggestions


def suggestions(follow_ups: Sequence[str], question: str) -> tuple[str, ...]:
    """The follow-up questions worth offering under a turn. Pure.

    Keeps, in order, at most MAX_SUGGESTIONS questions that end in "?" and are
    at most MAX_SUGGESTION_CHARS long. Blanks, repeats and the question just
    asked (compared without case or extra spaces) are dropped.
    """
    seen = {_same_question(question)}
    kept: list[str] = []
    for raw in follow_ups:
        text = " ".join(raw.split())
        if text.endswith("?") and len(text) <= MAX_SUGGESTION_CHARS and _same_question(text) not in seen:
            seen.add(_same_question(text))
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
    return " ".join(text.split()).casefold()


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
