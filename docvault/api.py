"""FastAPI routes. Each route parses input, calls one domain function, maps the result.

No business rules live here. The only table in this file maps domain error
codes to HTTP statuses, so the domain never imports HTTP concepts.
GET /events/documents turns an events.Feed into server-sent events. RequestLog gives every
request an id, returns it as X-Request-ID and logs the request once, when it ends.
"""

from __future__ import annotations

import contextvars
import json
import logging
import queue
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

import redis
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import chat, compare, documents, events, logs, metrics, summaries, wire
from .config import load_settings
from .context import Ctx, build_ctx, too_large
from .domain import (
    ChatId,
    ComparisonId,
    Created,
    DocStatus,
    DocumentId,
    ErrorCode,
    Existing,
    Failure,
    SummaryOptions,
    Turn,
    VaultError,
)
from .ratelimit import Action
from .tasks import CeleryJobs

log = logging.getLogger(__name__)
requests_log = logging.getLogger("docvault.requests")

ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.EMPTY_FILE: 422,
    ErrorCode.FILE_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_TYPE: 415,
    ErrorCode.DOCUMENT_NOT_FOUND: 404,
    ErrorCode.PAGE_NOT_FOUND: 404,
    ErrorCode.CHAT_NOT_FOUND: 404,
    ErrorCode.COMPARISON_NOT_FOUND: 404,
    ErrorCode.DOCUMENTS_NOT_READY: 409,
    ErrorCode.DOCUMENTS_DELETED: 409,
    ErrorCode.NOTHING_TO_RETRY: 409,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.INVALID_CURSOR: 400,
    ErrorCode.INVALID_REQUEST: 422,
    ErrorCode.RATE_LIMITED: 429,        # with Retry-After: the seconds until the budget allows one more
    ErrorCode.QUOTA_EXCEEDED: 429,      # with Retry-After: the seconds until midnight UTC, when the daily cap resets
    ErrorCode.TOO_MANY_STREAMS: 503,
    ErrorCode.AI_UNAVAILABLE: 503,      # with Retry-After: 5 when retryable
    ErrorCode.AI_BAD_OUTPUT: 502,
    ErrorCode.INTERNAL: 500,            # an exception no route expected (internal_error_handler); also stored
    # Stored-only codes (ENCRYPTED_PDF, UNREADABLE_FILE, NO_EXTRACTABLE_TEXT, TOO_MANY_PAGES)
    # appear inside a 200 document body, never as a request error.
}


# The multipart envelope around the file (boundaries, part headers, a long filename) on top of the file's bytes.
MULTIPART_SLACK = 64 * 1024


def errors(*codes: ErrorCode) -> dict[int | str, dict[str, Any]]:
    """OpenAPI responses for the error codes a route can return, each in the ErrorOut envelope."""
    by_status: dict[int, list[str]] = {}
    for code in codes:
        by_status.setdefault(ERROR_STATUS[code], []).append(code)
    return {status: {"model": wire.ErrorOut, "description": ", ".join(names)}
            for status, names in sorted(by_status.items())}


def json_errors(*codes: ErrorCode) -> dict[int | str, dict[str, Any]]:
    """errors() for a route whose 200 is not JSON. FastAPI files a `model` under the route's own media type,
    so these name the JSON envelope directly; the other routes keep ErrorOut in the components."""
    return {status: {"description": response["description"],
                     "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorOut"}}}}
            for status, response in errors(*codes).items()}


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


Deps = Annotated[Ctx, Depends(get_ctx)]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    logs.configure(settings.log_format)
    ctx = build_ctx(settings, CeleryJobs())
    app.state.ctx = ctx
    async with events.running(ctx.settings.database_url, ctx.db) as hub:
        app.state.events = hub
        yield
    ctx.db.close()


def error_out(exc: VaultError) -> wire.ErrorOut:
    return wire.ErrorOut(error=wire.error_body(exc.failure, exc.details))


def vault_error_handler(request: Request, exc: VaultError) -> JSONResponse:
    failure = exc.failure
    body = error_out(exc)
    retry_after = 5 if failure.code is ErrorCode.AI_UNAVAILABLE and failure.retryable else exc.details.get("retry_after_s")
    headers = None if retry_after is None else {"Retry-After": str(retry_after)}
    return JSONResponse(body.model_dump(mode="json"), status_code=ERROR_STATUS.get(failure.code, 500), headers=headers)


def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """An exception no route expected: the usual envelope and the request id, so a report can be matched to the
    log. Starlette sends this response from outside every middleware, so RequestLog cannot add the header."""
    log.exception("%s %s failed unexpectedly", request.method, request.url.path, exc_info=exc)
    response = vault_error_handler(request, VaultError(ErrorCode.INTERNAL, "The request failed unexpectedly.",
                                                       retryable=True))
    if (rid := logs.request_id.get()) is not None:
        response.headers["X-Request-ID"] = rid
    return response


def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Malformed input gets the same envelope as every other error, naming each field that failed."""
    problems = [{"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
                for error in exc.errors()]
    message = "; ".join(f"{problem['field']}: {problem['message']}" for problem in problems)
    return vault_error_handler(request, VaultError(ErrorCode.INVALID_REQUEST, f"The request is not valid. {message}",
                                                   details={"errors": problems}))


class RequestLog:
    """Pure ASGI middleware: the request id, and one docvault.requests line per HTTP request.

    The id is the client's X-Request-ID when logs.accept allows it, otherwise a fresh one, and it goes back on the
    response. The line is written when the response body ends, however it ends, so a stream is logged once with
    its whole duration. A BaseHTTPMiddleware gets the response when it starts, before a stream's body runs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = logs.accept(Headers(scope=scope).get("x-request-id"))
        # Not reset afterwards: internal_error_handler runs after this returns and needs the id.
        logs.request_id.set(rid)
        started = time.monotonic()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", rid.encode())]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            client = scope.get("client")
            requests_log.info("request", extra={
                "method": scope["method"], "route": getattr(scope.get("route"), "path", None), "status": status,
                "latency_ms": round((time.monotonic() - started) * 1000), "client": client[0] if client else None})


async def refuse_oversized_uploads(request: Request,
                                   call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """413 for POST /documents before any of the body is read, when Content-Length already says the file is
    too large. A body without Content-Length is stopped by BlobStore.put while it streams."""
    length = request.headers.get("content-length", "")
    if request.method == "POST" and request.url.path == "/documents" and length.isdigit():
        limit = get_ctx(request).settings.max_upload_bytes
        if int(length) > limit + MULTIPART_SLACK:
            return vault_error_handler(request, too_large(limit))
    return await call_next(request)


def _ping_db(ctx: Ctx) -> None:
    with ctx.db.connection(timeout=2) as conn:
        conn.execute("SELECT 1")


def _probe(check: Callable[[], object]) -> Literal["ok", "unreachable"]:
    try:
        check()
    except Exception:
        log.exception("health probe failed")
        return "unreachable"
    return "ok"


def rate_limited(action: Action) -> Any:
    """A route dependency that spends one of the client's per-minute budget for `action`, or answers 429."""
    def check(request: Request, ctx: Deps) -> None:
        per_minute = ctx.settings.uploads_per_minute if action == "upload" else ctx.settings.questions_per_minute
        wait = ctx.limiter.take(request.client.host if request.client else "", action, per_minute)
        if wait is not None:
            raise VaultError(ErrorCode.RATE_LIMITED, f"Too many requests. Try again in {wait} seconds.",
                             retryable=True, details={"retry_after_s": wait})
    return Depends(check)


router = APIRouter()


@router.post("/documents", status_code=202, response_model=wire.DocumentOut, dependencies=[rate_limited("upload")],
             responses={200: {"model": wire.DocumentOut, "description": "Same bytes uploaded before; existing document"},
                        **errors(ErrorCode.DOCUMENT_NOT_FOUND, ErrorCode.FILE_TOO_LARGE, ErrorCode.UNSUPPORTED_TYPE,
                                 ErrorCode.EMPTY_FILE, ErrorCode.INVALID_REQUEST, ErrorCode.VERSION_CONFLICT,
                                 ErrorCode.RATE_LIMITED)})
def upload(file: UploadFile, response: Response, ctx: Deps,
           replaces: Annotated[UUID | None, Query(description=(
               "A document id. New bytes become the next version of its family, whatever their name. Without it, "
               "new bytes whose name matches a stored document's become the next version of that document's family."))] = None,
           ) -> wire.DocumentOut:
    outcome = documents.upload(ctx, file.file, file.filename or "upload",
                               replaces=None if replaces is None else DocumentId(replaces))
    match outcome:
        case Created(doc):
            response.headers["Location"] = f"/documents/{doc.id}"
            return wire.document_out(doc)
        case Existing(doc):
            response.status_code = 200
            return wire.document_out(doc)


@router.get("/documents", response_model=wire.DocumentPage,
            responses=errors(ErrorCode.INVALID_CURSOR, ErrorCode.INVALID_REQUEST))
def list_documents(ctx: Deps, status: DocStatus | None = None,
                         latest: Annotated[bool, Query(description="Only the latest version of each document")] = False,
                         limit: Annotated[int, Query(ge=1, le=100)] = 20,
                         cursor: str | None = None) -> wire.DocumentPage:
    docs, next_cursor = documents.list_documents(ctx.db, status=status, latest=latest, limit=limit, cursor=cursor)
    return wire.DocumentPage(items=[wire.document_out(doc) for doc in docs], next_cursor=next_cursor)


@router.get("/documents/{doc_id}", response_model=wire.DocumentOut,
            responses=errors(ErrorCode.DOCUMENT_NOT_FOUND, ErrorCode.INVALID_REQUEST))
def get_document(doc_id: UUID, ctx: Deps) -> wire.DocumentOut:
    return wire.document_out(documents.get(ctx.db, DocumentId(doc_id)))


@router.get("/documents/{doc_id}/versions", response_model=wire.VersionList,
            responses=errors(ErrorCode.DOCUMENT_NOT_FOUND, ErrorCode.INVALID_REQUEST))
def get_versions(doc_id: UUID, ctx: Deps) -> wire.VersionList:
    """Every version of this document, oldest first. Deleting one leaves the others and their numbers."""
    return wire.VersionList(items=[wire.document_out(doc) for doc in documents.versions(ctx.db, DocumentId(doc_id))])


@router.get("/documents/{doc_id}/insights", response_model=wire.InsightsOut,
            responses=errors(ErrorCode.DOCUMENT_NOT_FOUND, ErrorCode.INVALID_REQUEST))
def get_insights(doc_id: UUID, ctx: Deps):
    return wire.insights_out(documents.get(ctx.db, DocumentId(doc_id)))


@router.get("/documents/{doc_id}/pages/{number}", response_model=wire.PageOut,
            responses=errors(ErrorCode.DOCUMENT_NOT_FOUND, ErrorCode.PAGE_NOT_FOUND, ErrorCode.INVALID_REQUEST))
def get_page(doc_id: UUID, number: int, ctx: Deps) -> wire.PageOut:
    basis, page = documents.page(ctx.db, DocumentId(doc_id), number)
    return wire.page_out(doc_id, basis, page)


@router.post("/documents/{doc_id}/retry", status_code=202, response_model=wire.DocumentOut,
             responses=errors(ErrorCode.DOCUMENT_NOT_FOUND, ErrorCode.NOTHING_TO_RETRY, ErrorCode.INVALID_REQUEST))
def retry(doc_id: UUID, ctx: Deps) -> wire.DocumentOut:
    return wire.document_out(documents.retry(ctx, DocumentId(doc_id)))


@router.delete("/documents/{doc_id}", status_code=204,
               responses=errors(ErrorCode.DOCUMENT_NOT_FOUND, ErrorCode.INVALID_REQUEST))
def delete_document(doc_id: UUID, ctx: Deps) -> None:
    documents.delete(ctx, DocumentId(doc_id))


@router.post("/chats", status_code=201, response_model=wire.ChatOut,
             responses=errors(ErrorCode.DOCUMENT_NOT_FOUND, ErrorCode.DOCUMENTS_NOT_READY, ErrorCode.INVALID_REQUEST))
def start_chat(body: wire.StartChatIn, ctx: Deps) -> wire.ChatOut:
    return wire.chat_out(chat.start(ctx.db, [DocumentId(doc_id) for doc_id in body.document_ids], title=body.title))


@router.get("/chats", response_model=wire.ChatPage, responses=errors(ErrorCode.INVALID_CURSOR, ErrorCode.INVALID_REQUEST))
def list_chats(ctx: Deps, limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: str | None = None) -> wire.ChatPage:
    chats, next_cursor = chat.list_chats(ctx.db, limit=limit, cursor=cursor)
    return wire.ChatPage(items=[wire.chat_out(c) for c in chats], next_cursor=next_cursor)


@router.get("/chats/{chat_id}", response_model=wire.ChatOut,
            responses=errors(ErrorCode.CHAT_NOT_FOUND, ErrorCode.INVALID_REQUEST))
def get_chat(chat_id: UUID, ctx: Deps) -> wire.ChatOut:
    return wire.chat_out(chat.get(ctx.db, ChatId(chat_id)))


@router.post("/chats/{chat_id}/messages", response_model=wire.TurnOut, dependencies=[rate_limited("ask")],
             responses=errors(ErrorCode.CHAT_NOT_FOUND, ErrorCode.DOCUMENTS_DELETED, ErrorCode.AI_UNAVAILABLE,
                              ErrorCode.AI_BAD_OUTPUT, ErrorCode.INVALID_REQUEST, ErrorCode.RATE_LIMITED,
                              ErrorCode.QUOTA_EXCEEDED))
def ask(chat_id: UUID, body: wire.AskIn, ctx: Deps) -> wire.TurnOut:
    return wire.turn_out(chat.ask(ctx, ChatId(chat_id), body.question))


class EventStreamResponse(StreamingResponse):
    media_type = "text/event-stream"


# no-cache keeps any cache from holding the stream back, and X-Accel-Buffering: no turns off nginx-style proxy
# buffering. no-transform stops a proxy from compressing it: the web UI's Next.js rewrite gzips text/event-stream
# by default and so delivers every event at once when the answer ends.
STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
UNEXPECTED = Failure(ErrorCode.INTERNAL, "Answering failed unexpectedly. Try again.", retryable=True)


def sse(event: str, data: BaseModel | dict[str, object]) -> str:
    body = data.model_dump_json() if isinstance(data, BaseModel) else json.dumps(data)
    return f"event: {event}\ndata: {body}\n\n"


class AskInThread:
    def __init__(self, ctx: Ctx, chat_id: ChatId, question: str) -> None:
        self._events: queue.SimpleQueue[str | Turn | Exception] = queue.SimpleQueue()
        # A new thread starts with an empty context; the copy carries the request id to the ledger rows.
        self.thread = threading.Thread(target=contextvars.copy_context().run, args=(self._run, ctx, chat_id, question),
                                       daemon=True, name=f"ask-{chat_id}")
        self.thread.start()

    def _run(self, ctx: Ctx, chat_id: ChatId, question: str) -> None:
        put = self._events.put
        try:
            put(chat.ask(ctx, chat_id, question, on_stage=lambda stage: put(sse("stage", {"stage": stage})),
                         on_text=lambda text: put(sse("delta", {"text": text})),
                         on_reset=lambda: put(sse("reset", {}))))
        except VaultError as exc:
            put(exc)
        except Exception as exc:
            log.exception("a streamed ask failed")
            put(exc)

    def first(self) -> str | Turn | Exception:
        return self._events.get()

    def frames(self, first: str | Turn | Exception) -> Iterator[str]:
        item = first
        while isinstance(item, str):
            yield item
            item = self._events.get()
        match item:
            case VaultError():
                yield sse("error", error_out(item))
            case Exception():
                yield sse("error", wire.ErrorOut(error=wire.error_body(UNEXPECTED)))
            case _:
                yield sse("turn", wire.turn_out(item))


STREAM_EVENTS = """\
Asks like `POST /chats/{chat_id}/messages` and spends from the same rate-limit budget, but answers with
server-sent events while the answer is written. A refusal found before the stream starts (404, 409, 422, 429,
including `quota_exceeded` when today's AI spending cap is used up) is the usual status with the error envelope.
A chat's first question that was answered before on the same documents comes from the answer cache, over the cap
too: its events are one `delta` with the whole answer, markers removed, then `turn` with `cached: true`.
After a 200, the events are:

- `stage`, data `{"stage": "condensing" | "searching" | "answering"}`, as each step starts. `condensing` comes
  only when the chat has earlier turns.
- `delta`, data `{"text": "..."}`: provisional answer text with every `[S#]` marker removed. Append it.
- `reset`, data `{}`: discard the provisional text. Sent before a retry or repair that follows text already sent.
- `turn`, data: the same body `POST /chats/{chat_id}/messages` returns. It is grounded and final, replaces all
  provisional text, and ends the stream.
- `error`, data: the error envelope (`ai_unavailable`, `ai_bad_output`, `internal_error`, or `quota_exceeded` when
  the spending cap is reached after the stream started). It ends the stream.

If the client disconnects, the answer still finishes and is stored, so `GET /chats/{chat_id}/messages` shows it."""


@router.post("/chats/{chat_id}/messages/stream", response_class=EventStreamResponse, dependencies=[rate_limited("ask")],
             description=STREAM_EVENTS,
             responses={200: {"description": "stage, delta, reset, then one turn or error event",
                              "content": {"text/event-stream": {"schema": {"type": "string"}}}},
                        **json_errors(ErrorCode.CHAT_NOT_FOUND, ErrorCode.DOCUMENTS_DELETED,
                                      ErrorCode.INVALID_REQUEST, ErrorCode.RATE_LIMITED, ErrorCode.QUOTA_EXCEEDED)})
def ask_stream(chat_id: UUID, body: wire.AskIn, ctx: Deps) -> EventStreamResponse:
    asking = AskInThread(ctx, ChatId(chat_id), body.question)
    first = asking.first()
    if isinstance(first, Exception):
        raise first
    return EventStreamResponse(asking.frames(first), headers=STREAM_HEADERS)


async def document_frames(feed: events.Feed) -> AsyncIterator[str]:
    # retry: browsers reconnect after a drop in 1 s instead of their default 3 to 5, before the UI says it paused.
    yield ": connected\nretry: 1000\n\n"
    while True:
        match await feed.next():
            case None:
                yield ": keepalive\n\n"
            case events.Changed(doc):
                yield sse("document", wire.document_out(doc))
            case events.Deleted(doc_id):
                yield sse("deleted", {"id": str(doc_id)})
            case events.Resync():
                yield sse("resync", {})


class FeedResponse(EventStreamResponse):
    """Frees the feed's slot however the response ends. A generator's own finally would not run for a client
    that left before the body started, and that slot would stay taken for good."""

    def __init__(self, feed: events.Feed) -> None:
        super().__init__(document_frames(feed), headers=STREAM_HEADERS)
        self.feed = feed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.feed.close()


DOCUMENT_EVENTS = """\
Live status of every document, as server-sent events that never end. Each event is about one document whose
`GET /documents/{id}` body may have changed:

- `document`, data: that document's `GET /documents/{id}` body, read after the change was committed.
- `deleted`, data `{"id": "..."}`: the document no longer exists.
- `resync`, data `{}`: events may have been missed, so reload the list with `GET /documents`. Sent when the
  server reconnects to the database, when a read fails, and to a client that fell more than 1000 events behind.

The stream opens with the comment `: connected` and `retry: 1000`, and sends `: keepalive` after 15 s with nothing
else to send.
Past 100 open streams per API process the request is refused with 503 `too_many_streams` and
`Retry-After`, before the stream starts."""


@router.get("/events/documents", response_class=EventStreamResponse, description=DOCUMENT_EVENTS,
            responses={200: {"description": "document, deleted and resync events, and keepalive comments",
                             "content": {"text/event-stream": {"schema": {"type": "string"}}}},
                       **json_errors(ErrorCode.TOO_MANY_STREAMS)})
async def document_events(request: Request) -> FeedResponse:
    return FeedResponse(request.app.state.events.subscribe())


@router.get("/chats/{chat_id}/messages", response_model=wire.HistoryOut,
            responses=errors(ErrorCode.CHAT_NOT_FOUND, ErrorCode.INVALID_REQUEST))
def history(chat_id: UUID, ctx: Deps, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> wire.HistoryOut:
    return wire.HistoryOut(chat_id=chat_id,
                           turns=[wire.turn_out(turn) for turn in chat.history(ctx.db, ChatId(chat_id), limit=limit)])


@router.post("/comparisons", status_code=201, response_model=wire.ComparisonOut, dependencies=[rate_limited("ask")],
             responses=errors(ErrorCode.DOCUMENT_NOT_FOUND, ErrorCode.DOCUMENTS_NOT_READY, ErrorCode.INVALID_REQUEST,
                              ErrorCode.RATE_LIMITED, ErrorCode.QUOTA_EXCEEDED, ErrorCode.AI_UNAVAILABLE,
                              ErrorCode.AI_BAD_OUTPUT))
def create_comparison(body: wire.CompareIn, ctx: Deps) -> wire.ComparisonOut:
    """Compare 2 to 5 ready documents field by field, each cell checked against the page it cites."""
    return wire.comparison_out(compare.create(ctx, [DocumentId(doc_id) for doc_id in body.document_ids], body.focus))


@router.get("/comparisons", response_model=wire.ComparisonPage,
            responses=errors(ErrorCode.INVALID_CURSOR, ErrorCode.INVALID_REQUEST))
def list_comparisons(ctx: Deps, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                     cursor: str | None = None) -> wire.ComparisonPage:
    """Stored comparisons, newest first, a page at a time."""
    comparisons, next_cursor = compare.list_comparisons(ctx.db, limit=limit, cursor=cursor)
    return wire.ComparisonPage(items=[wire.comparison_out(c) for c in comparisons], next_cursor=next_cursor)


@router.get("/comparisons/{comparison_id}", response_model=wire.ComparisonOut,
            responses=errors(ErrorCode.COMPARISON_NOT_FOUND, ErrorCode.INVALID_REQUEST))
def get_comparison(comparison_id: UUID, ctx: Deps) -> wire.ComparisonOut:
    """One stored comparison. A document deleted since keeps its column, its cells and their page labels."""
    return wire.comparison_out(compare.get(ctx.db, ComparisonId(comparison_id)))


@router.post("/documents/{doc_id}/summaries", status_code=201, response_model=wire.SummaryOut,
             dependencies=[rate_limited("ask")],
             responses={200: {"model": wire.SummaryOut,
                              "description": "Summarised before with the same options; the stored summary, cached"},
                        **errors(ErrorCode.DOCUMENT_NOT_FOUND, ErrorCode.DOCUMENTS_NOT_READY, ErrorCode.INVALID_REQUEST,
                                 ErrorCode.RATE_LIMITED, ErrorCode.QUOTA_EXCEEDED, ErrorCode.AI_UNAVAILABLE,
                                 ErrorCode.AI_BAD_OUTPUT)})
def create_summary(doc_id: UUID, body: wire.SummaryIn, response: Response, ctx: Deps) -> wire.SummaryOut:
    """Summarise a ready document at the length, focus and tone asked for, each source checked on its page."""
    summary = summaries.create(ctx, DocumentId(doc_id), SummaryOptions.of(body.length, body.focus, body.tone))
    if summary.cached:
        response.status_code = 200
    return wire.summary_out(summary)


@router.get("/documents/{doc_id}/summaries", response_model=wire.SummaryPage,
            responses=errors(ErrorCode.DOCUMENT_NOT_FOUND, ErrorCode.INVALID_CURSOR, ErrorCode.INVALID_REQUEST))
def list_summaries(doc_id: UUID, ctx: Deps, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                   cursor: str | None = None) -> wire.SummaryPage:
    """A document's stored summaries, newest first, a page at a time."""
    items, next_cursor = summaries.list_summaries(ctx.db, DocumentId(doc_id), limit=limit, cursor=cursor)
    return wire.SummaryPage(items=[wire.summary_out(summary) for summary in items], next_cursor=next_cursor)


@router.get("/metrics/documents", response_model=wire.DocumentMetricsOut)
def document_metrics(ctx: Deps) -> wire.DocumentMetricsOut:
    return wire.document_metrics_out(metrics.document_stats(ctx.db))


# At most four digits, so the timedelta never overflows; a leading zero would allow an empty window.
Window = Annotated[str, Query(pattern=r"^[1-9]\d{0,3}[hd]$", description="Hours or days back from now: 1h, 24h, 7d.")]
WINDOW_UNITS = {"h": "hours", "d": "days"}


@router.get("/metrics/processing", response_model=wire.ProcessingMetricsOut, responses=errors(ErrorCode.INVALID_REQUEST))
def processing_metrics(ctx: Deps, window: Window = "24h") -> wire.ProcessingMetricsOut:
    span = timedelta(**{WINDOW_UNITS[window[-1]]: int(window[:-1])})
    return wire.processing_metrics_out(window, metrics.processing_stats(ctx.db, span), ctx.cache.stats())


@router.get("/health", response_model=wire.HealthOut,
            responses={503: {"model": wire.HealthOut, "description": "Postgres or Redis is unreachable"}})
def health(ctx: Deps, response: Response) -> wire.HealthOut:
    """503 when Postgres or Redis is unreachable, so compose and load balancers can gate on it. The cache never
    makes it 503: the app answers without it."""
    db = _probe(lambda: _ping_db(ctx))
    broker = _probe(lambda: redis.Redis.from_url(ctx.settings.redis_url, socket_timeout=2).ping())
    if "unreachable" in (db, broker):
        response.status_code = 503
    # The spend is read from Postgres, so it is left out rather than failing the probe when Postgres is down.
    budget = ctx.ai.budget() if db == "ok" else None
    return wire.HealthOut(db=db, redis=broker, ai_mode=ctx.ai.mode, embed_model=ctx.ai.embed_model,
                          max_upload_bytes=ctx.settings.max_upload_bytes, cache=ctx.cache.probe(),
                          ai_budget=None if budget is None else wire.AIBudgetOut(limit_usd=float(budget[0]),
                                                                                 spent_today_usd=float(budget[1])))


app = FastAPI(title="Doc Vault", version="1.0", lifespan=lifespan)
app.add_exception_handler(VaultError, vault_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(Exception, internal_error_handler)
app.middleware("http")(refuse_oversized_uploads)
app.add_middleware(RequestLog)   # added last, so outermost: it also logs and tags refuse_oversized_uploads' 413
app.include_router(router)
