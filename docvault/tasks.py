"""Celery wiring. Three thin shims around the domain pipeline functions.

All pipeline logic is in documents.run_index and insights.run_analyze, which
tests call directly without Celery. This file only decides delivery
semantics and maps VaultError.retryable to Celery retry or terminal failure.

Delivery semantics (each is load-bearing):
  task_acks_late = True              ack after the task body returns, so a crash redelivers
  task_reject_on_worker_lost = True  a SIGKILLed or OOM-killed worker redelivers too
  worker_prefetch_multiplier = 1     a long policy PDF doesn't hold queued work hostage
  visibility_timeout = 7200          Redis redelivers an unacked task after this, so it
                                     exceeds the longest run; the lease makes an early
                                     redelivery harmless anyway
  beat: reconcile every 60 s
  queues: "ingest" (parse + embed) and "llm" (insights), so a 60 s summary
          never sits in front of a parse; one worker consumes both in compose

Each worker process builds its own Ctx on its first task, after the fork,
so no connection pool is shared across processes.
"""

from __future__ import annotations

import functools
import logging
from uuid import UUID

from celery import Celery

from . import documents
from .config import load_settings
from .context import Ctx, build_ctx
from .domain import DocumentId, ErrorCode, Failure, VaultError

log = logging.getLogger(__name__)

MAX_RETRIES = 5   # backoff 2, 4, 8, 16, 32 s

celery_app = Celery("docvault", broker=load_settings().redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 7200},
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,
    task_routes={"docvault.tasks.index_document": {"queue": "ingest"},
                 "docvault.tasks.analyze_document": {"queue": "llm"}},
    beat_schedule={"reconcile": {"task": "docvault.tasks.reconcile", "schedule": 60.0}},
)


class CeleryJobs:
    """context.Jobs implementation. Called only after the enqueuing transaction commits."""

    def index(self, doc_id: DocumentId) -> None:
        index_document.delay(str(doc_id))

    def analyze(self, doc_id: DocumentId) -> None:
        analyze_document.delay(str(doc_id))


@functools.cache
def _ctx() -> Ctx:
    return build_ctx(load_settings(), CeleryJobs())


@celery_app.task(bind=True, max_retries=MAX_RETRIES)
def index_document(self, doc_id: str) -> None:
    """A retryable VaultError becomes a Celery retry until the attempts run out.
    Every other failure ends in a FAILED document, so the user sees a terminal
    state and a reason, never a spinner."""
    ctx, doc = _ctx(), DocumentId(UUID(doc_id))
    try:
        documents.run_index(ctx, doc, attempt=self.request.retries + 1)
    except VaultError as exc:
        if exc.failure.retryable and self.request.retries < MAX_RETRIES:
            raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1)) from exc
        documents.fail(ctx, doc, exc.failure)
    except Exception:
        log.exception("indexing document %s failed", doc_id)
        documents.fail(ctx, doc, Failure(ErrorCode.INTERNAL, "Processing failed unexpectedly. Retry the document.",
                                         retryable=True))


@celery_app.task(bind=True, max_retries=MAX_RETRIES)
def analyze_document(self, doc_id: str) -> None:
    """Same shape as index_document around insights.run_analyze; terminal failure
    upserts a failed document_insights row and leaves the document READY."""
    raise NotImplementedError


@celery_app.task
def reconcile() -> int:
    return documents.reconcile(_ctx())
