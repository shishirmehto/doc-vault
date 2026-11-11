"""Documents: upload, lifecycle, the indexing pipeline, and document reads.

Owns tables documents, pages and chunks. Everything that decides a
document's lifecycle state is in this file: the transition table, the
compare-and-swap that applies it, and the one pipeline function that drives
it. Format and page rules live in pages.py; model calls go through ai.py.

Idempotency model:
  - The outputs are the checkpoints. "Has pages" means parse is done.
    "Chunks with embedding_model = current" means those chunks are embedded.
    A rerun reads the outputs, skips finished work, and redoes only the gap.
  - Each checkpoint commits its outputs and its state transition in one
    transaction, so a crash leaves both or neither.
  - A per-document advisory lease (db.lease) turns duplicate deliveries into no-ops.
  - Transitions are compare-and-swap UPDATEs checked against TRANSITIONS.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import BinaryIO

from psycopg import Connection

from .context import Ctx
from .db import Db
from .domain import DocStatus, Document, DocumentId, Failure, Page, UploadOutcome

# Allowed moves. A self-move (PARSING -> PARSING) is always allowed so a
# redelivered task can re-assert the state it crashed in.
TRANSITIONS: Mapping[DocStatus, frozenset[DocStatus]] = {
    DocStatus.QUEUED: frozenset({DocStatus.PARSING, DocStatus.FAILED}),
    DocStatus.PARSING: frozenset({DocStatus.EMBEDDING, DocStatus.FAILED}),
    DocStatus.EMBEDDING: frozenset({DocStatus.READY, DocStatus.FAILED}),
    DocStatus.READY: frozenset({DocStatus.EMBEDDING}),   # embedding model changed; reconcile re-embeds
    DocStatus.FAILED: frozenset({DocStatus.QUEUED}),     # POST /documents/{id}/retry
}


def sources_of(target: DocStatus) -> frozenset[DocStatus]:
    """States from which `target` is reachable, plus target itself. Used in the CAS WHERE clause."""
    return frozenset({s for s, nxt in TRANSITIONS.items() if target in nxt} | {target})


# ------------------------------------------------------------------ commands


def upload(ctx: Ctx, stream: BinaryIO, filename: str) -> UploadOutcome:
    """Store bytes, dedupe by content, create the document, enqueue indexing.

    TODO:
      sha, size, head = ctx.blobs.put(stream, max_bytes=ctx.settings.max_upload_bytes)
      kind = pages.sniff_kind(head, filename)     # UNSUPPORTED_TYPE -> 415, before any row exists
      INSERT INTO documents (sha256, filename, kind, size_bytes) VALUES (...)
        ON CONFLICT (sha256) DO NOTHING RETURNING id
      no row  -> Existing(get(ctx.db, id_by_sha))         # HTTP 200, nothing enqueued
      new row -> ctx.jobs.index(id) after commit; Created(doc)  # HTTP 202
    Two concurrent uploads of the same bytes write the same blob path and race on
    the unique index; one gets Created, the other Existing. A crash after the blob
    write leaves an orphan file that the next identical upload reuses.
    A lost enqueue (crash after commit) is caught by reconcile().
    """
    raise NotImplementedError


def retry(ctx: Ctx, doc_id: DocumentId) -> Document:
    """Re-run whichever stage failed. The user never has to know which stage it was.

    Document FAILED -> CAS FAILED -> QUEUED, clear error fields, ctx.jobs.index(doc_id).
      Allowed for non-retryable failures too; `retryable` only tells the user
      whether a retry can help without a new file.
    Else insights failed -> insights.reset_failed(doc_id), ctx.jobs.analyze(doc_id).
    Else -> VaultError(NOTHING_TO_RETRY), HTTP 409.
    """
    raise NotImplementedError


def delete(ctx: Ctx, doc_id: DocumentId) -> None:
    """DELETE the row (pages, chunks, insights, chat_documents cascade), then remove the blob.

    A pipeline task in flight finds its CAS update matches no row and exits.
    step_runs and llm_calls keep their rows, so history and spend outlive the
    document. Chat turns keep their citation snapshots.
    """
    raise NotImplementedError


# ------------------------------------------------------------------ pipeline


def run_index(ctx: Ctx, doc_id: DocumentId, *, attempt: int = 1) -> None:
    """Drive one document from QUEUED to READY. Safe to call any number of times.

    Raises VaultError(retryable=True) for transient faults; the Celery shim
    retries with backoff and calls fail() after the last attempt. Raises
    VaultError(retryable=False) for bad files; the shim calls fail() at once.
    Returns quietly for duplicate deliveries and deleted documents.

    TODO:
      with lease(ctx.db, f"index:{doc_id}") as conn:
          if conn is None: return                                       # another worker owns it
          close_abandoned(conn, doc_id, Step.PARSE, Step.EMBED)   # left open by a crashed run
          doc = load row; if missing or FAILED: return
          if READY and no stale chunks: return                          # duplicate delivery

          if not has_pages(conn, doc_id):
              if not transition(conn, doc_id, PARSING): return
              with step(conn, doc_id, Step.PARSE, attempt):
                  text = pages.extract(doc.kind, ctx.blobs.path(doc.sha), max_pages=...)
                  drafts = pages.chunk(text, max_chars=..., overlap_chars=...)
                  with conn.transaction():                        # checkpoint 1: all or nothing
                      INSERT pages; INSERT chunks ON CONFLICT (document_id, ord) DO NOTHING
                      UPDATE documents SET page_basis, page_count, chunk_count, empty_pages
                      transition(conn, doc_id, EMBEDDING)
          elif not transition(conn, doc_id, EMBEDDING): return    # self-move after a crash

          if insights row absent: ctx.jobs.analyze(doc_id)              # analyze is idempotent too

          with step(conn, doc_id, Step.EMBED, attempt):
              loop:
                  batch = SELECT id, text FROM chunks WHERE document_id = $1
                          AND (embedding IS NULL OR embedding_model <> ctx.ai.embed_model)
                          ORDER BY ord LIMIT embed_batch_size * embed_concurrency
                  if not batch: break
                  vectors = ctx.ai.embed([f"{doc.filename}\\n{c.text}" for c in batch],
                                               purpose=EMBED_CHUNKS, owner=DocOwner(doc_id))
                  UPDATE chunks SET embedding, embedding_model FROM unnest(ids, vectors)   # checkpoint per batch
              transition(conn, doc_id, READY)
    """
    raise NotImplementedError


def fail(ctx: Ctx, doc_id: DocumentId, failure: Failure) -> None:
    """Terminal. One UPDATE: status FAILED and the three error fields, WHERE status is not READY or FAILED."""
    raise NotImplementedError


def transition(conn: Connection, doc_id: DocumentId, to: DocStatus) -> bool:
    """UPDATE documents SET status = to, updated_at = now()
       WHERE id = $1 AND status = ANY(sources_of(to)).

    Returns False when no row matched (deleted, or another actor moved it), and
    the caller stops. The table decides what is legal, not the caller.
    """
    raise NotImplementedError


def reconcile(ctx: Ctx) -> int:
    """Celery beat every 60 s, and once at worker start. Returns how many jobs it enqueued.

    Converges the system after lost messages, crashes and config changes:
      1. QUEUED, PARSING or EMBEDDING with updated_at older than 2 min -> jobs.index.
         A document still being worked on costs one failed lock attempt.
      2. READY documents with chunks whose embedding_model differs from ctx.ai.embed_model
         (a reviewer adds an OpenAI key after a fake run): CAS READY -> EMBEDDING, jobs.index.
      3. EMBEDDING or READY documents with no document_insights row, older than 2 min -> jobs.analyze.
    """
    raise NotImplementedError


# ------------------------------------------------------------------ reads


def get(db: Db, doc_id: DocumentId) -> Document:
    """One query: documents LEFT JOIN document_insights, the embedded-chunk count when
    EMBEDDING, and processing_ms from step_runs. _row_to_document parses the row into
    the Lifecycle and InsightsState unions. Raises VaultError(DOCUMENT_NOT_FOUND)."""
    raise NotImplementedError


def list_documents(db: Db, *, status: DocStatus | None, limit: int,
                         cursor: str | None) -> tuple[list[Document], str | None]:
    """Keyset pagination on (created_at DESC, id DESC). The cursor encodes the last pair."""
    raise NotImplementedError


def page(db: Db, doc_id: DocumentId, number: int) -> tuple[Document, Page]:
    """The text a citation points at. Raises PAGE_NOT_FOUND."""
    raise NotImplementedError


def _row_to_document(row: Mapping[str, object]) -> Document:
    """The database-to-domain boundary for documents.

    Lifecycle from (status, parsed fields, error fields). InsightsState from
    (insights row or NULL, document status): no row and document FAILED reads
    as InsightsUnavailable, no row otherwise as InsightsPending. The CHECK
    constraints in schema.sql guarantee every row maps to exactly one variant.
    """
    raise NotImplementedError
