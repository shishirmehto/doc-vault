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

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import uuid4  # noqa: F401  (turn ids are generated before any AI call)

from psycopg import Connection

from .ai import Vector
from .context import Ctx
from .db import Db
from .domain import Answer, Chat, ChatId, DocRef, DocumentId, Source, Turn, TurnId
from .prompts import AnswerDraft

RRF_K = 60
CANDIDATES = 50


@dataclass(frozen=True, slots=True)
class Scope:
    documents: tuple[DocRef, *tuple[DocRef, ...]]


@dataclass(frozen=True, slots=True)
class Candidate:
    """One row from the retrieval query, before source selection."""

    source: Source
    lexical_hit: bool       # chunk.tsv matched websearch_to_tsquery(question) (all terms present)


# ------------------------------------------------------------------ commands


def start(db: Db, document_ids: Sequence[DocumentId] | None, *, title: str | None = None) -> Chat:
    """Open a chat over a fixed document set.

    None or empty -> every READY document at this moment; none ready -> NO_READY_DOCUMENTS (409).
    Unknown ids -> DOCUMENT_NOT_FOUND (404) listing them.
    Any id not READY -> DOCUMENTS_NOT_READY (409) listing id and status.
    The set is stored in chat_documents and never changes; a document uploaded
    later needs a new chat. This keeps every turn's scope reproducible.
    """
    raise NotImplementedError


def ask(ctx: Ctx, chat_id: ChatId, question: str) -> Turn:
    """Answer one question with citations, or refuse honestly. Persists exactly one turn.

    TODO:
      turn_id = TurnId(uuid4()); owner = TurnOwner(turn_id); started = now()
      chat, scope = load chat + chat_documents (CHAT_NOT_FOUND)
      history = last ctx.settings.history_turns turns, oldest first

      standalone = question if not history else
                   (ctx.ai.complete(prompts.condense(history, question), CondensedQuery, owner=owner)).standalone

      [q_vec] = ctx.ai.embed([standalone], purpose=EMBED_QUERY, owner=owner)
      with ctx.db.connection() as conn:
          candidates = retrieve(conn, scope, q_vec, standalone, ctx.ai.embed_model)

      if not gate(candidates, ctx.ai.min_similarity):
          answer = NotFound(NO_EVIDENCE, prompts.no_evidence_text(scope.documents))
      else:
          sources = select_sources(candidates, k=ctx.settings.retrieval_k,
                                   per_doc_min=ctx.settings.retrieval_per_doc_min)
          draft = ctx.ai.complete(prompts.answer(question, standalone, history, sources), AnswerDraft, owner=owner)
          answer = ground(draft, sources)

      INSERT chat_turns (id=turn_id, ..., outcome, answer text, citations snapshot, started, now())
      return Turn(..., usage=SELECT sum(...) FROM llm_calls WHERE turn_id = turn_id)

    Provider failure -> VaultError(AI_UNAVAILABLE) -> HTTP 503 with Retry-After; no
    turn is written (the failed call is still in llm_calls). No DB transaction is
    held across any AI call.
    """
    raise NotImplementedError


def history(db: Db, chat_id: ChatId, *, limit: int = 50, before: TurnId | None = None) -> list[Turn]:
    """Turns oldest first, each with its citation snapshot and usage from llm_calls (one GROUP BY join)."""
    raise NotImplementedError


# ------------------------------------------------------------------ retrieval

# A chat's scope is small (a few documents, a few thousand chunks).
# MATERIALIZED stops the planner from using the HNSW index, so every scoped
# chunk is scored exactly; filtering can never starve the top-k.
# Both rankings and the per-document rank come from window functions over the
# same scored set, so hybrid fusion costs one query.
EXACT_SQL = """
WITH scored AS MATERIALIZED (
  SELECT c.id, c.document_id, c.text, c.page_map,
         c.embedding <=> %(q)s AS distance,
         c.tsv @@ websearch_to_tsquery('english', %(qtext)s) AS lexical_hit,
         ts_rank_cd(c.tsv, websearch_to_tsquery('english', %(qtext)s)) AS lex
  FROM chunks c
  WHERE c.document_id = ANY(%(doc_ids)s) AND c.embedding_model = %(model)s
), ranked AS (
  SELECT *,
         row_number() OVER (ORDER BY distance)                       AS vec_rank,
         row_number() OVER (ORDER BY lex DESC, distance)             AS lex_rank,
         row_number() OVER (PARTITION BY document_id ORDER BY distance) AS doc_rank
  FROM scored
)
SELECT id, document_id, text, page_map, 1 - distance AS similarity, lexical_hit, doc_rank,
       1.0 / (%(rrf_k)s + vec_rank) + CASE WHEN lex > 0 THEN 1.0 / (%(rrf_k)s + lex_rank) ELSE 0 END AS rrf
FROM ranked
WHERE vec_rank <= %(n)s OR (lex > 0 AND lex_rank <= %(n)s) OR doc_rank <= %(per_doc)s
ORDER BY rrf DESC
"""

def retrieve(conn: Connection, scope: Scope, q_vec: Vector, query_text: str,
             embed_model: str) -> list[Candidate]:
    """Run EXACT_SQL over the chat's documents.

    Only chunks embedded with the current model are searchable, so a model
    switch can never compare vectors from two embedding spaces. Rows are
    parsed into Candidates here (PageMap from the jsonb page_map).
    """
    raise NotImplementedError


def gate(candidates: Sequence[Candidate], min_similarity: float) -> bool:
    """Is there anything worth sending to the answer model?

    True when the best vector similarity reaches min_similarity, or when any
    chunk contains every query term (lexical_hit). The lexical arm keeps exact
    identifiers ("CG 20 10", a policy number) from being refused by a
    semantic threshold. False means refuse with NO_EVIDENCE and spend no
    answer call. min_similarity belongs to the embedding backend because
    similarity scales differ per model.
    """
    raise NotImplementedError


def select_sources(candidates: Sequence[Candidate], *, k: int, per_doc_min: int) -> list[Source]:
    """Choose the k sources the model sees, labelled S1..Sk in rrf order.

    Single-document scope: top k by rrf.
    Multi-document scope: first take each document's best per_doc_min chunks
    (doc_rank), then fill to k by rrf. "Compare the deductibles in these three
    quotes" then sees all three quotes even when one dominates the scores.
    """
    raise NotImplementedError


def ground(draft: AnswerDraft, sources: Sequence[Source]) -> Answer:
    """Turn the model's draft into an Answer the user can trust. Pure.

    TODO:
      if not draft.supported: return NotFound(MODEL_DECLINED, draft.answer)
      by_label = {s.label: s for s in sources}
      citations, ref_of = [], {}
      for ev in draft.evidence:
          src = by_label.get(ev.source)                      # unknown label -> drop
          span = pages.locate_quote(ev.quote, src.text)      # None -> drop (fabricated or paraphrased)
          if span is None: continue
          start, end = span
          citations.append(Citation(document_id=src.doc.id, filename=src.doc.filename,
                                    page=src.page_map.page_at(start), page_basis=src.doc.page_basis,
                                    quote=src.text[start:end], chunk_id=src.chunk_id))
          ref_of.setdefault(ev.source, len(citations))      # first verified quote per label
      if not citations: return NotFound(UNVERIFIED, "I found related passages but could not verify an answer in them. ...")
      text = rewrite markers: [S3] -> [n] via ref_of; markers for labels without a verified quote are removed
      return Answered(text, (citations[0], *citations[1:]))

    The page comes from where the quote sits inside the chunk, not from the
    chunk's first page, so a chunk spanning pages 11-12 cites 12 when the
    quoted words are on 12.
    """
    raise NotImplementedError
