"""Celery wiring. Three thin shims around the domain pipeline functions.

All pipeline logic is in documents.run_index and insights.run_analyze, which
tests call directly without Celery. This file only decides delivery
semantics. run_index writes its own terminal failures under its lease, so a
VaultError it raises always means retry. For insights this file maps
VaultError.retryable to a Celery retry or insights.fail.

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

The request id travels as a task header: set when the API enqueues, set again
before the task runs, so the worker's log lines and ledger rows name the request.
The worker and beat log through logs.configure, in the API's format.
"""

from __future__ import annotations

import functools
import logging
from uuid import UUID

from celery import Celery, Task
from celery.signals import before_task_publish, setup_logging, task_prerun

from . import documents, insights, logs
from .config import load_settings
from .context import Ctx, build_ctx
from .documents import MAX_RETRIES
from .domain import DocumentId, ErrorCode, Failure, VaultError

log = logging.getLogger(__name__)

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


@setup_logging.connect
def _configure_logging(**_: object) -> None:
    logs.configure(load_settings().log_format)


@before_task_publish.connect
def _send_request_id(headers: dict[str, object], **_: object) -> None:
    if (rid := logs.request_id.get()) is not None:
        headers["request_id"] = rid


@task_prerun.connect
def _take_request_id(task: Task, **_: object) -> None:
    """None when the task carries no id, so a beat task never keeps the id of the task before it."""
    logs.request_id.set(task.request.get("request_id"))


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
    """run_index ends every failed run in a FAILED document with a reason, so
    the user never sees a spinner. It raises only when another attempt is due."""
    try:
        documents.run_index(_ctx(), DocumentId(UUID(doc_id)), attempt=self.request.retries + 1)
    except VaultError as exc:
        raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1)) from exc


@celery_app.task(bind=True, max_retries=MAX_RETRIES)
def analyze_document(self, doc_id: str) -> None:
    """A retryable VaultError becomes a Celery retry until the attempts run out.
    A terminal failure records failed insights and leaves the document as it
    is, still searchable."""
    ctx, doc = _ctx(), DocumentId(UUID(doc_id))
    try:
        insights.run_analyze(ctx, doc, attempt=self.request.retries + 1)
    except VaultError as exc:
        if exc.failure.retryable and self.request.retries < MAX_RETRIES:
            raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1)) from exc
        insights.fail(ctx, doc, exc.failure)
    except Exception:
        log.exception("%s for document %s failed", self.name, doc_id)
        insights.fail(ctx, doc, Failure(ErrorCode.INTERNAL, "Insights failed unexpectedly. Retry the document to try again.",
                                        retryable=True))


@celery_app.task
def reconcile() -> int:
    return documents.reconcile(_ctx())
