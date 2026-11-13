"""AI insights for one document. The only writer of document_insights.

Insights run beside indexing, not inside it: the index pipeline owns
documents.status, this module owns its own row. An insights failure never
blocks chat, and a slow summary never delays READY.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

from psycopg import Connection
from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb

from . import prompts
from .ai import DocOwner
from .context import Ctx
from .db import Db, Lease, pipeline_steps
from .domain import (
    DocumentId,
    ErrorCode,
    Failure,
    InsightsBody,
    InsightsFailed,
    InsightsReady,
    KeyFact,
    Page,
    PageBasis,
    Step,
)
from .pages import find_on_page
from .prompts import INSIGHTS_VERSION, InsightsDraft

_READY = """
INSERT INTO document_insights (document_id, status, prompt_version, model, body, pages_read, dropped_unverified)
VALUES (%s, 'ready', %s, %s, %s, %s, %s)
ON CONFLICT (document_id) DO UPDATE SET
  status = 'ready', prompt_version = excluded.prompt_version, model = excluded.model, body = excluded.body,
  pages_read = excluded.pages_read, dropped_unverified = excluded.dropped_unverified,
  error_code = NULL, error_message = NULL, error_retryable = NULL, updated_at = now()
"""

# Older insights beat an error, so a failure never replaces a ready row.
_FAILED = """
INSERT INTO document_insights (document_id, status, prompt_version, model, error_code, error_message, error_retryable)
VALUES (%s, 'failed', %s, %s, %s, %s, %s)
ON CONFLICT (document_id) DO UPDATE SET
  prompt_version = excluded.prompt_version, model = excluded.model, error_code = excluded.error_code,
  error_message = excluded.error_message, error_retryable = excluded.error_retryable, updated_at = now()
WHERE document_insights.status = 'failed'
"""


def run_analyze(ctx: Ctx, doc_id: DocumentId, *, attempt: int = 1) -> None:
    """Generate, verify and store insights. Safe to run any number of times.

    Raises VaultError: the Celery shim retries a retryable one and calls fail()
    after the last attempt. Returns quietly for duplicate deliveries, deleted
    documents, documents not parsed yet (run_index enqueues analysis after its
    parse checkpoint), insights already ready under the current prompt
    version, and failed insights, which stay failed until
    POST /documents/{id}/retry deletes the row. The document's status is not
    checked: pages are all insights need.
    """
    with pipeline_steps(ctx.db, doc_id, Lease.ANALYZE, attempt) as steps:
        if steps is None:
            return
        conn = steps.conn
        doc = conn.execute(
            "SELECT d.filename, d.page_basis, i.status AS insights_status, i.prompt_version FROM documents d"
            " LEFT JOIN document_insights i ON i.document_id = d.id WHERE d.id = %s", (doc_id,)).fetchone()
        if doc is None or doc["insights_status"] == "failed" or (
                doc["insights_status"] == "ready" and doc["prompt_version"] == INSIGHTS_VERSION):
            return
        pages = [Page(row["number"], row["text"]) for row in conn.execute(
            "SELECT number, text FROM pages WHERE document_id = %s ORDER BY number", (doc_id,))]
        if not pages:
            return
        with steps.record(Step.ANALYZE):
            prompt, pages_read = prompts.insights(doc["filename"], PageBasis(doc["page_basis"]), pages,
                                                  budget_chars=ctx.settings.insights_max_chars)
            draft = ctx.ai.complete(prompt, InsightsDraft, owner=DocOwner(doc_id))
            body, dropped = verify(draft, pages)
            try:
                conn.execute(_READY, (doc_id, prompt.version, ctx.ai.chat.models[prompt.tier],
                                      Jsonb(dataclasses.asdict(body)), pages_read, dropped))
            except ForeignKeyViolation:
                return   # the document was deleted during the model call


def verify(draft: InsightsDraft, pages: Sequence[Page]) -> tuple[InsightsBody, int]:
    """Keep a key fact only when pages.find_on_page finds its quote on the page it cites.

    A kept fact quotes the matched source text, not the model's copy. Returns
    the body and the number of facts dropped; the count is stored so evals and
    metrics can see how often the model cites wrongly.
    """
    kept = [KeyFact(fact.label, fact.value, fact.page, found) for fact in draft.key_facts
            if (found := find_on_page(fact.quote, pages, fact.page))]
    body = InsightsBody(draft.title, draft.document_type, draft.summary, tuple(kept), tuple(draft.tags))
    return body, len(draft.key_facts) - len(kept)


def fail(ctx: Ctx, doc_id: DocumentId, failure: Failure) -> None:
    """Terminal, after the Celery retries. Leaves a ready row and a deleted document alone."""
    try:
        with ctx.db.connection() as conn:
            conn.execute(_FAILED, (doc_id, INSIGHTS_VERSION, ctx.ai.chat.models["main"],
                                   failure.code, failure.message, failure.retryable))
    except ForeignKeyViolation:
        return


def reset_failed(db: Db, doc_id: DocumentId) -> bool:
    """DELETE a failed row so the state reads 'pending' again. Used by documents.retry."""
    with db.connection() as conn:
        return conn.execute("DELETE FROM document_insights WHERE document_id = %s AND status = 'failed'"
                            " RETURNING document_id", (doc_id,)).fetchone() is not None


def ready_key_facts(conn: Connection, document_ids: Sequence[DocumentId]) -> dict[DocumentId, tuple[KeyFact, ...]]:
    """The verified key facts of each document whose insights are ready. Other documents are absent."""
    rows = conn.execute("SELECT document_id, body FROM document_insights WHERE document_id = ANY(%s)"
                        " AND status = 'ready'", (list(document_ids),))
    return {DocumentId(row["document_id"]): tuple(KeyFact(**fact) for fact in row["body"]["key_facts"]) for row in rows}


def from_row(row: Mapping[str, Any], page_count: int, basis: PageBasis) -> InsightsReady | InsightsFailed:
    """One document_insights row, as the jsonb that documents._DOCUMENT_SQL selects."""
    if row["status"] == "failed":
        return InsightsFailed(Failure(ErrorCode(row["error_code"]), row["error_message"], row["error_retryable"]))
    body = row["body"]
    return InsightsReady(
        body=InsightsBody(title=body["title"], document_type=body["document_type"], summary=body["summary"],
                          key_facts=tuple(KeyFact(**fact) for fact in body["key_facts"]), tags=tuple(body["tags"])),
        pages_read=row["pages_read"], page_count=page_count, page_basis=basis,
        dropped_key_facts=row["dropped_unverified"], model=row["model"], prompt_version=row["prompt_version"])
