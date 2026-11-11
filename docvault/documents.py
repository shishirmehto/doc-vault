"""Documents: upload, lifecycle, the indexing pipeline, and document reads.

Owns tables documents, pages and chunks. Everything that decides a
document's lifecycle state is in this file: the transition table, the
compare-and-swap that applies it, and the one pipeline function that drives
it. Format and page rules live in pages.py; model calls go through ai.py.

Idempotency model:
  - The outputs are the checkpoints. "Has parse facts" means parse is done.
    "Chunks with embedding_model = current" means those chunks are embedded.
    A rerun reads the outputs, skips finished work, and redoes only the gap.
  - Each checkpoint commits its outputs and its state transition in one
    transaction, so a crash leaves both or neither.
  - A per-document advisory lease (db.pipeline_steps) turns duplicate deliveries into no-ops.
  - Transitions are compare-and-swap UPDATEs checked against TRANSITIONS.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import PurePath
from typing import BinaryIO
from uuid import UUID

from pgvector import Vector
from psycopg import Connection
from psycopg.types.json import Jsonb

from . import insights, pages
from .ai import DocOwner
from .context import Ctx
from .db import Db, Lease, PipelineSteps, pipeline_steps
from .domain import (
    Created,
    DocKind,
    DocStatus,
    Document,
    DocumentId,
    Embedding,
    ErrorCode,
    Existing,
    Failed,
    Failure,
    InsightsFailed,
    InsightsPending,
    InsightsState,
    InsightsStatus,
    InsightsUnavailable,
    Lifecycle,
    Page,
    PageBasis,
    Parsed,
    Parsing,
    Purpose,
    Queued,
    Ready,
    Sha256,
    Step,
    UploadOutcome,
    VaultError,
)

log = logging.getLogger(__name__)

# Allowed moves. Re-asserting the current state is also allowed (see transition),
# so a redelivered task can pick up in the state it crashed in.
TRANSITIONS: Mapping[DocStatus, frozenset[DocStatus]] = {
    # QUEUED -> EMBEDDING: a retry of a document that failed after its parse checkpoint.
    DocStatus.QUEUED: frozenset({DocStatus.PARSING, DocStatus.EMBEDDING, DocStatus.FAILED}),
    DocStatus.PARSING: frozenset({DocStatus.EMBEDDING, DocStatus.FAILED}),
    DocStatus.EMBEDDING: frozenset({DocStatus.READY, DocStatus.FAILED}),
    DocStatus.READY: frozenset({DocStatus.EMBEDDING}),   # embedding model changed; reconcile re-embeds
    DocStatus.FAILED: frozenset({DocStatus.QUEUED}),     # POST /documents/{id}/retry
}

STALE_AFTER = timedelta(minutes=2)
ANALYZE_BATCH = 50
MAX_RETRIES = 5   # Celery retries after the first attempt (tasks.py); backoff 2, 4, 8, 16, 32 s

# A chunk still to embed: never embedded, or embedded by a model that is no longer the current one.
_PENDING = "embedding_model IS DISTINCT FROM %s"


def sources_of(target: DocStatus) -> list[DocStatus]:
    """States from which `target` is reachable. Used in the compare-and-swap WHERE clause."""
    return [source for source, targets in TRANSITIONS.items() if target in targets]


# ------------------------------------------------------------------ commands


def upload(ctx: Ctx, stream: BinaryIO, filename: str) -> UploadOutcome:
    """Store bytes, dedupe by content, create the document, enqueue indexing.

    Two concurrent uploads of the same bytes write the same blob path and race on
    the unique index; one gets Created, the other Existing. A crash after the blob
    write leaves an orphan file that the next identical upload reuses.
    A lost enqueue (crash after commit) is caught by reconcile().
    """
    sha, size = ctx.blobs.put(stream, max_bytes=ctx.settings.max_upload_bytes)
    try:
        kind = pages.sniff_kind(ctx.blobs.path(sha))
    except VaultError:
        ctx.blobs.delete(sha)   # a stored document always sniffs, so this blob belongs to nobody
        raise
    with ctx.db.connection() as conn:
        created = conn.execute(
            "INSERT INTO documents (sha256, filename, kind, size_bytes) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (sha256) DO NOTHING RETURNING id",
            (sha, PurePath(filename).name[:255] or "upload", kind, size)).fetchone()
        doc_id = (created or conn.execute("SELECT id FROM documents WHERE sha256 = %s", (sha,)).fetchone())["id"]
    if not created:
        return Existing(get(ctx.db, doc_id))
    ctx.jobs.index(doc_id)
    return Created(get(ctx.db, doc_id))


def retry(ctx: Ctx, doc_id: DocumentId) -> Document:
    """Re-run whichever stage failed. The user never has to know which stage it was.

    A failed document goes back to QUEUED and is indexed again. This is allowed
    for non-retryable failures too; `retryable` only tells the user whether a
    retry can help without a new file. Otherwise failed insights are reset and
    analysed again. Otherwise NOTHING_TO_RETRY.
    """
    with ctx.db.connection() as conn:
        requeued = transition(conn, doc_id, DocStatus.QUEUED, reassert=False)
    if requeued:
        ctx.jobs.index(doc_id)
    elif isinstance(get(ctx.db, doc_id).insights, InsightsFailed):
        insights.reset_failed(ctx.db, doc_id)
        ctx.jobs.analyze(doc_id)
    else:
        raise VaultError(ErrorCode.NOTHING_TO_RETRY, "Nothing failed for this document, so there is nothing to retry.")
    return get(ctx.db, doc_id)


def delete(ctx: Ctx, doc_id: DocumentId) -> None:
    """DELETE the row (pages, chunks and insights cascade), then remove the blob.

    A pipeline task in flight finds its compare-and-swap matches no row and exits.
    step_runs and llm_calls keep their rows, so history and spend outlive the
    document. Chat turns keep their citation snapshots.
    """
    with ctx.db.connection() as conn:
        deleted = conn.execute("DELETE FROM documents WHERE id = %s RETURNING sha256", (doc_id,)).fetchone()
    if not deleted:
        raise _not_found(doc_id)
    ctx.blobs.delete(Sha256(deleted["sha256"]))


# ------------------------------------------------------------------ pipeline


def run_index(ctx: Ctx, doc_id: DocumentId, *, attempt: int = 1) -> None:
    """Drive one document from QUEUED to READY. Safe to call any number of times.

    Raises VaultError(retryable=True) only while attempts remain (attempt runs
    from 1 to MAX_RETRIES + 1); the Celery shim retries it and no status changes.
    Every failure that ends the run writes FAILED before the lease is released:
    a non-retryable VaultError, a retryable one on the last attempt, and any
    other Exception (INTERNAL). Writing it after the release would let a
    re-queued worker take the lease and start parsing a document the user
    already sees as Failed.
    Returns quietly for duplicate deliveries, failed documents and deleted documents.
    """
    with pipeline_steps(ctx.db, doc_id, Lease.INDEX, attempt) as steps:
        if steps is None:
            return
        try:
            _index(ctx, steps)
            return
        except VaultError as exc:
            if exc.failure.retryable and attempt <= MAX_RETRIES:
                raise
            failure = exc.failure
        except Exception:
            log.exception("Indexing document %s failed", doc_id)
            failure = Failure(ErrorCode.INTERNAL, "Processing failed unexpectedly. Retry the document.", retryable=True)
        _move(steps.conn, doc_id, DocStatus.FAILED, sources_of(DocStatus.FAILED), failure)


def _index(ctx: Ctx, steps: PipelineSteps) -> None:
    conn, doc_id = steps.conn, steps.doc_id
    doc = conn.execute(
        "SELECT sha256, filename, kind, status, page_count IS NOT NULL AS parsed,"
        f" EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id AND c.{_PENDING}) AS has_pending_chunks"
        " FROM documents d WHERE id = %s", (ctx.ai.embed_model, doc_id)).fetchone()
    if doc is None or doc["status"] == DocStatus.FAILED:
        return
    if doc["status"] == DocStatus.READY and not doc["has_pending_chunks"]:
        return

    if not doc["parsed"]:
        if not transition(conn, doc_id, DocStatus.PARSING):
            return
        with steps.record(Step.PARSE):
            _parse(ctx, conn, doc_id, DocKind(doc["kind"]), Sha256(doc["sha256"]))
        ctx.jobs.analyze(doc_id)
    elif not transition(conn, doc_id, DocStatus.EMBEDDING):
        return

    with steps.record(Step.EMBED):
        _embed_pending_chunks(ctx, conn, doc_id, doc["filename"])
        transition(conn, doc_id, DocStatus.READY)


def _parse(ctx: Ctx, conn: Connection, doc_id: DocumentId, kind: DocKind, sha: Sha256) -> None:
    """Checkpoint 1: pages, chunks, parse facts and the move to EMBEDDING commit together."""
    text = pages.extract(kind, ctx.blobs.path(sha), max_pages=ctx.settings.max_pages)
    drafts = pages.chunk(text, max_chars=ctx.settings.chunk_max_chars, overlap_chars=ctx.settings.chunk_overlap_chars)
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany("INSERT INTO pages (document_id, number, text) VALUES (%s, %s, %s)",
                        [(doc_id, page.number, page.text) for page in text.pages])
        cur.executemany("INSERT INTO chunks (document_id, ord, text, page_map) VALUES (%s, %s, %s, %s)",
                        [(doc_id, draft.ord, draft.text, Jsonb(draft.page_map.starts)) for draft in drafts])
        cur.execute("UPDATE documents SET page_basis = %s, page_count = %s, chunk_count = %s, empty_pages = %s"
                    " WHERE id = %s",
                    (text.basis, len(text.pages), len(drafts), [p.number for p in text.pages if not p.text], doc_id))
        transition(conn, doc_id, DocStatus.EMBEDDING)


def _embed_pending_chunks(ctx: Ctx, conn: Connection, doc_id: DocumentId, filename: str) -> None:
    """Checkpoint per batch: a crash keeps every batch already written."""
    model = ctx.ai.embed_model
    while batch := conn.execute(
            f"SELECT id, text FROM chunks WHERE document_id = %s AND {_PENDING} ORDER BY ord LIMIT %s", (doc_id, model, ctx.settings.embed_batch_size)).fetchall():
        vectors = ctx.ai.embed([f"{filename}\n{row['text']}" for row in batch],
                               purpose=Purpose.EMBED_CHUNKS, owner=DocOwner(doc_id))
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany("UPDATE chunks SET embedding = %s, embedding_model = %s WHERE id = %s",
                            [(Vector(vector), model, row["id"]) for row, vector in zip(batch, vectors, strict=True)])


def transition(conn: Connection, doc_id: DocumentId, to: DocStatus, *, reassert: bool = True) -> bool:
    """Compare-and-swap to a non-failed state. The table decides what is legal, not the caller.

    Returns False when no row matched (deleted, or another actor moved it), and
    the caller stops. With reassert, a document already in `to` also matches.
    """
    return _move(conn, doc_id, to, sources_of(to) + ([to] if reassert else []), None)


def _move(conn: Connection, doc_id: DocumentId, to: DocStatus, sources: list[DocStatus],
          failure: Failure | None) -> bool:
    """Status and error fields change in one UPDATE, so the row never breaks failed_iff_error."""
    assert (to is DocStatus.FAILED) == (failure is not None)
    moved = conn.execute(
        "UPDATE documents SET status = %s, error_code = %s, error_message = %s, error_retryable = %s,"
        " updated_at = now() WHERE id = %s AND status = ANY(%s) RETURNING id",
        (to, failure and failure.code, failure and failure.message, failure and failure.retryable, doc_id, sources))
    return moved.fetchone() is not None


def reconcile(ctx: Ctx) -> int:
    """Celery beat every 60 s. Returns how many jobs it enqueued.

    Converges the system after lost messages, crashes and config changes:
      1. QUEUED, PARSING or EMBEDDING and untouched for STALE_AFTER -> jobs.index.
         A document still being worked on costs one failed lock attempt.
      2. READY documents with chunks from another embedding model (a reviewer
         adds a provider key after a fake run) -> EMBEDDING, jobs.index.
      3. Parsed documents untouched for STALE_AFTER, with no insights row and
         no analyze step started within STALE_AFTER -> jobs.analyze, at most
         ANALYZE_BATCH per tick. Covers an enqueue lost after the parse
         checkpoint, and documents indexed before insights existed. A job still
         waiting in the queue has no step row, so the batch bounds the duplicates
         a backlog collects; a duplicate returns once the insights exist.
    """
    in_flight = [DocStatus.QUEUED, DocStatus.PARSING, DocStatus.EMBEDDING]
    with ctx.db.connection() as conn:
        stale = [row["id"] for row in conn.execute(
            "SELECT id FROM documents WHERE status = ANY(%s) AND updated_at < now() - %s",
            (in_flight, STALE_AFTER))]
        outdated = [row["id"] for row in conn.execute(
            "SELECT id FROM documents d WHERE status = %s AND EXISTS"
            f" (SELECT 1 FROM chunks c WHERE c.document_id = d.id AND c.{_PENDING})",
            (DocStatus.READY, ctx.ai.embed_model))]
        to_index = stale + [doc_id for doc_id in outdated if transition(conn, doc_id, DocStatus.EMBEDDING)]
        to_analyze = [row["id"] for row in conn.execute(
            "SELECT id FROM documents d WHERE page_count IS NOT NULL AND updated_at < now() - %(stale)s"
            " AND NOT EXISTS (SELECT 1 FROM document_insights i WHERE i.document_id = d.id)"
            " AND NOT EXISTS (SELECT 1 FROM step_runs s WHERE s.document_id = d.id AND s.step = %(analyze)s"
            "                 AND s.started_at > now() - %(stale)s)"
            " ORDER BY updated_at LIMIT %(batch)s",
            {"stale": STALE_AFTER, "analyze": Step.ANALYZE, "batch": ANALYZE_BATCH})]
    for doc_id in to_index:
        ctx.jobs.index(doc_id)
    for doc_id in to_analyze:
        ctx.jobs.analyze(doc_id)
    return len(to_index) + len(to_analyze)


# ------------------------------------------------------------------ reads

# The insights status of documents d LEFT JOIN document_insights i. An absent row reads as pending, or as
# unavailable when the document failed before its pages existed: parsed is the one test, as in run_analyze and
# reconcile. metrics.py counts with the same expression.
INSIGHTS_STATUS_SQL = """CASE WHEN i.status IS NOT NULL THEN i.status
            WHEN d.page_count IS NULL AND d.status = 'failed' THEN 'unavailable'
            ELSE 'pending' END"""

_DOCUMENT_SQL = f"""
SELECT d.id, d.filename, d.kind, d.size_bytes, d.status, d.page_basis, d.page_count, d.chunk_count, d.empty_pages,
       d.error_code, d.error_message, d.error_retryable, d.created_at,
       to_jsonb(i) AS insights, {INSIGHTS_STATUS_SQL} AS insights_status,
       CASE WHEN d.status = 'embedding' THEN
         (SELECT count(*) FROM chunks c WHERE c.document_id = d.id AND c.embedding IS NOT NULL) END AS embedded_chunks,
       CASE WHEN d.status = 'ready' THEN
         (SELECT round(extract(epoch FROM max(s.finished_at) - d.created_at) * 1000)::bigint FROM step_runs s
           WHERE s.document_id = d.id AND s.step = 'embed' AND s.outcome = 'ok') END AS processing_ms
FROM documents d LEFT JOIN document_insights i ON i.document_id = d.id
"""


def get(db: Db, doc_id: DocumentId) -> Document:
    """Raises VaultError(DOCUMENT_NOT_FOUND)."""
    with db.connection() as conn:
        row = conn.execute(_DOCUMENT_SQL + "WHERE d.id = %s", (doc_id,)).fetchone()
    if row is None:
        raise _not_found(doc_id)
    return _row_to_document(row)


def list_documents(db: Db, *, status: DocStatus | None, limit: int,
                   cursor: str | None) -> tuple[list[Document], str | None]:
    """Keyset pagination on (created_at DESC, id DESC). The cursor encodes the last pair."""
    after = _decode_cursor(cursor) if cursor else None
    with db.connection() as conn:
        rows = conn.execute(
            _DOCUMENT_SQL + "WHERE (%(status)s::text IS NULL OR d.status = %(status)s)"
            " AND (%(created_at)s::timestamptz IS NULL OR (d.created_at, d.id) < (%(created_at)s::timestamptz, %(id)s::uuid))"
            " ORDER BY d.created_at DESC, d.id DESC LIMIT %(limit)s",
            {"status": status, "created_at": after and after[0], "id": after and after[1],
             "limit": limit + 1}).fetchall()
    last = rows[limit - 1] if len(rows) > limit else None
    return [_row_to_document(row) for row in rows[:limit]], last and _encode_cursor(last["created_at"], last["id"])


def page(db: Db, doc_id: DocumentId, number: int) -> tuple[PageBasis, Page]:
    """The text a citation points at, and what its number means. Raises PAGE_NOT_FOUND."""
    with db.connection() as conn:
        row = conn.execute(
            "SELECT d.page_basis, d.page_count, p.text FROM documents d"
            " LEFT JOIN pages p ON p.document_id = d.id AND p.number = %s WHERE d.id = %s", (number, doc_id)).fetchone()
    if row is None:
        raise _not_found(doc_id)
    if row["text"] is None:
        available = f"It has {row['page_count']}." if row["page_count"] else "It has not been parsed yet."
        raise VaultError(ErrorCode.PAGE_NOT_FOUND, f"This document has no page {number}. {available}")
    return PageBasis(row["page_basis"]), Page(number, row["text"])


def _not_found(doc_id: DocumentId) -> VaultError:
    return VaultError(ErrorCode.DOCUMENT_NOT_FOUND, f"No document has the id {doc_id}.")


def _encode_cursor(created_at: datetime, doc_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{doc_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        created_at, doc_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(created_at), UUID(doc_id)
    except (ValueError, binascii.Error) as exc:
        raise VaultError(ErrorCode.INVALID_CURSOR, "The cursor is not one this API returned.") from exc


def _row_to_document(row: Mapping[str, object]) -> Document:
    """The database-to-domain boundary for documents.

    The CHECK constraints in schema.sql guarantee every row maps to exactly
    one Lifecycle variant.
    """
    parsed = None if row["page_count"] is None else Parsed(
        PageBasis(row["page_basis"]), row["page_count"], row["chunk_count"], tuple(row["empty_pages"]))
    lifecycle: Lifecycle
    match DocStatus(row["status"]):
        case DocStatus.QUEUED:
            lifecycle = Queued()
        case DocStatus.PARSING:
            lifecycle = Parsing()
        case DocStatus.EMBEDDING:
            lifecycle = Embedding(parsed, row["embedded_chunks"])
        case DocStatus.READY:
            lifecycle = Ready(parsed)
        case DocStatus.FAILED:
            failure = Failure(ErrorCode(row["error_code"]), row["error_message"], row["error_retryable"])
            lifecycle = Failed(failure, parsed)
    return Document(
        id=DocumentId(row["id"]), filename=row["filename"], kind=DocKind(row["kind"]), size_bytes=row["size_bytes"],
        lifecycle=lifecycle, insights=_insights_state(row), created_at=row["created_at"],
        processing_ms=row["processing_ms"])


def _insights_state(row: Mapping[str, object]) -> InsightsState:
    match InsightsStatus(row["insights_status"]):
        case InsightsStatus.PENDING:
            return InsightsPending()
        case InsightsStatus.UNAVAILABLE:
            return InsightsUnavailable()
        case InsightsStatus.READY | InsightsStatus.FAILED:
            return insights.from_row(row["insights"], row["page_count"], PageBasis(row["page_basis"]))
