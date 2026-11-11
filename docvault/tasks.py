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

Each worker process builds its own Ctx at worker_process_init, after the
fork, so no connection pool is shared across processes.
"""

from __future__ import annotations

from celery import Celery

from .config import load_settings
from .domain import DocumentId

MAX_RETRIES = 5   # backoff 2, 4, 8, 16, 32 s with jitter

celery_app = Celery("docvault", broker=load_settings().redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 7200},
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,
)


class CeleryJobs:
    """context.Jobs implementation. Called only after the enqueuing transaction commits."""

    def index(self, doc_id: DocumentId) -> None:
        index_document.delay(str(doc_id))

    def analyze(self, doc_id: DocumentId) -> None:
        analyze_document.delay(str(doc_id))


@celery_app.task(bind=True, max_retries=MAX_RETRIES, queue="ingest")
def index_document(self, doc_id: str) -> None:
    """documents.run_index(ctx, id, attempt=self.request.retries + 1)

    except VaultError as e:
        if e.failure.retryable and self.request.retries < MAX_RETRIES:
            raise self.retry(countdown=backoff(self.request.retries))
        documents.fail(ctx, id, e.failure)
    Any other exception is a bug: logged with the doc id and traceback, then
    fail() with INTERNAL (retryable) so the user sees a terminal state, not a spinner.
    """
    raise NotImplementedError


@celery_app.task(bind=True, max_retries=MAX_RETRIES, queue="llm")
def analyze_document(self, doc_id: str) -> None:
    """Same shape as index_document around insights.run_analyze; terminal failure
    upserts a failed document_insights row and leaves the document READY."""
    raise NotImplementedError


@celery_app.task
def reconcile() -> int:
    """documents.reconcile(ctx)"""
    raise NotImplementedError
