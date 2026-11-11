"""FastAPI routes. Each route parses input, calls one domain function, maps the result.

No business rules live here. The only table in this file maps domain error
codes to HTTP statuses, so the domain never imports HTTP concepts.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated
from uuid import UUID

import redis
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from . import chat, documents, metrics, wire
from .config import load_settings
from .context import Ctx, build_ctx
from .domain import ChatId, Created, DocStatus, DocumentId, ErrorCode, Existing, VaultError
from .tasks import CeleryJobs

log = logging.getLogger(__name__)

ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.EMPTY_FILE: 422,
    ErrorCode.FILE_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_TYPE: 415,
    ErrorCode.DOCUMENT_NOT_FOUND: 404,
    ErrorCode.PAGE_NOT_FOUND: 404,
    ErrorCode.CHAT_NOT_FOUND: 404,
    ErrorCode.DOCUMENTS_NOT_READY: 409,
    ErrorCode.DOCUMENTS_DELETED: 409,
    ErrorCode.NOTHING_TO_RETRY: 409,
    ErrorCode.INVALID_CURSOR: 400,
    ErrorCode.AI_UNAVAILABLE: 503,      # with Retry-After: 5
    ErrorCode.AI_BAD_OUTPUT: 502,
    # Stored-only codes (ENCRYPTED_PDF, UNREADABLE_FILE, NO_EXTRACTABLE_TEXT, TOO_MANY_PAGES, INTERNAL)
    # appear inside a 200 document body, never as a request error.
}


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


Deps = Annotated[Ctx, Depends(get_ctx)]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    ctx = build_ctx(load_settings(), CeleryJobs())
    app.state.ctx = ctx
    yield
    ctx.db.close()


def vault_error_handler(request: Request, exc: VaultError) -> JSONResponse:
    failure = exc.failure
    body = wire.ErrorOut(error=wire.error_body(failure, exc.details))
    headers = {"Retry-After": "5"} if failure.code is ErrorCode.AI_UNAVAILABLE else None
    return JSONResponse(body.model_dump(mode="json"), status_code=ERROR_STATUS.get(failure.code, 500), headers=headers)


def _ping_db(ctx: Ctx) -> None:
    with ctx.db.connection(timeout=2) as conn:
        conn.execute("SELECT 1")


def _probe(check: Callable[[], object]) -> str:
    try:
        check()
    except Exception:
        log.exception("health probe failed")
        return "unreachable"
    return "ok"


router = APIRouter()


@router.post("/documents", status_code=202, response_model=wire.DocumentOut,
             responses={200: {"description": "Same bytes uploaded before; existing document returned"}})
def upload(file: UploadFile, response: Response, ctx: Deps) -> wire.DocumentOut:
    outcome = documents.upload(ctx, file.file, file.filename or "upload")
    match outcome:
        case Created(doc):
            response.headers["Location"] = f"/documents/{doc.id}"
            return wire.document_out(doc)
        case Existing(doc):
            response.status_code = 200
            return wire.document_out(doc)


@router.get("/documents", response_model=wire.DocumentPage)
def list_documents(ctx: Deps, status: DocStatus | None = None,
                         limit: Annotated[int, Query(ge=1, le=100)] = 20,
                         cursor: str | None = None) -> wire.DocumentPage:
    docs, next_cursor = documents.list_documents(ctx.db, status=status, limit=limit, cursor=cursor)
    return wire.DocumentPage(items=[wire.document_out(doc) for doc in docs], next_cursor=next_cursor)


@router.get("/documents/{doc_id}", response_model=wire.DocumentOut)
def get_document(doc_id: UUID, ctx: Deps) -> wire.DocumentOut:
    return wire.document_out(documents.get(ctx.db, DocumentId(doc_id)))


@router.get("/documents/{doc_id}/insights", response_model=wire.InsightsOut)
def get_insights(doc_id: UUID, ctx: Deps):
    return wire.insights_out(documents.get(ctx.db, DocumentId(doc_id)))


@router.get("/documents/{doc_id}/pages/{number}", response_model=wire.PageOut)
def get_page(doc_id: UUID, number: int, ctx: Deps) -> wire.PageOut:
    basis, page = documents.page(ctx.db, DocumentId(doc_id), number)
    return wire.page_out(doc_id, basis, page)


@router.post("/documents/{doc_id}/retry", status_code=202, response_model=wire.DocumentOut)
def retry(doc_id: UUID, ctx: Deps) -> wire.DocumentOut:
    return wire.document_out(documents.retry(ctx, DocumentId(doc_id)))


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: UUID, ctx: Deps) -> None:
    documents.delete(ctx, DocumentId(doc_id))


@router.post("/chats", status_code=201, response_model=wire.ChatOut)
def start_chat(body: wire.StartChatIn, ctx: Deps) -> wire.ChatOut:
    return wire.chat_out(chat.start(ctx.db, [DocumentId(doc_id) for doc_id in body.document_ids], title=body.title))


@router.post("/chats/{chat_id}/messages", response_model=wire.TurnOut)
def ask(chat_id: UUID, body: wire.AskIn, ctx: Deps) -> wire.TurnOut:
    return wire.turn_out(chat.ask(ctx, ChatId(chat_id), body.question))


@router.get("/chats/{chat_id}/messages", response_model=wire.HistoryOut)
def history(chat_id: UUID, ctx: Deps, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> wire.HistoryOut:
    return wire.HistoryOut(chat_id=chat_id,
                           turns=[wire.turn_out(turn) for turn in chat.history(ctx.db, ChatId(chat_id), limit=limit)])


@router.get("/metrics/documents", response_model=wire.DocumentMetricsOut)
def document_metrics(ctx: Deps) -> wire.DocumentMetricsOut:
    return wire.document_metrics_out(metrics.document_stats(ctx.db))


# At most four digits, so the timedelta never overflows; a leading zero would allow an empty window.
Window = Annotated[str, Query(pattern=r"^[1-9]\d{0,3}[hd]$", description="Hours or days back from now: 1h, 24h, 7d.")]
WINDOW_UNITS = {"h": "hours", "d": "days"}


@router.get("/metrics/processing", response_model=wire.ProcessingMetricsOut)
def processing_metrics(ctx: Deps, window: Window = "24h") -> wire.ProcessingMetricsOut:
    span = timedelta(**{WINDOW_UNITS[window[-1]]: int(window[:-1])})
    return wire.processing_metrics_out(window, metrics.processing_stats(ctx.db, span))


@router.get("/health")
def health(ctx: Deps, response: Response) -> dict[str, str]:
    """503 when Postgres or Redis is unreachable, so compose and load balancers can gate on it."""
    checks = {"db": _probe(lambda: _ping_db(ctx)),
              "redis": _probe(lambda: redis.Redis.from_url(ctx.settings.redis_url, socket_timeout=2).ping())}
    if any(v != "ok" for v in checks.values()):
        response.status_code = 503
    return {**checks, "ai_mode": ctx.ai.mode, "embed_model": ctx.ai.embed_model}


app = FastAPI(title="Doc Vault", version="1.0", lifespan=lifespan)
app.add_exception_handler(VaultError, vault_error_handler)  # type: ignore[arg-type]
app.include_router(router)
