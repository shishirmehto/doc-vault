"""HTTP request and response models, and the domain -> wire mapping.

Only api.py imports this module. Domain unions are flattened here into the
JSON shapes in the README; discriminated unions keyed on `status` keep the
OpenAPI schema honest about which fields exist in which state.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, assert_never
from uuid import UUID

from pydantic import BaseModel, Field

from . import pages
from .domain import (
    DocKind,
    DocStatus,
    Document,
    Embedding,
    Failed,
    InsightsFailed,
    InsightsPending,
    InsightsReady,
    InsightsUnavailable,
    Page,
    PageBasis,
    Parsing,
    Queued,
    Ready,
    Turn,
)

# ------------------------------------------------------------------ shared


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool | None = None
    details: dict[str, object] = {}


class ErrorOut(BaseModel):
    error: ErrorBody


# ------------------------------------------------------------------ documents


class Progress(BaseModel):
    embedded_chunks: int
    total_chunks: int


class DocumentOut(BaseModel):
    id: UUID
    filename: str
    kind: DocKind
    size_bytes: int
    status: DocStatus
    progress: Progress | None          # only while status == "embedding"
    pages: int | None                  # None until parsed
    page_basis: PageBasis | None
    empty_pages: list[int] = []
    insights_status: Literal["pending", "ready", "failed", "unavailable"]
    error: ErrorBody | None            # only when status == "failed"
    processing_ms: int | None
    created_at: datetime
    links: dict[str, str]


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    next_cursor: str | None


class PageOut(BaseModel):
    document_id: UUID
    number: int
    label: str                         # "p. 3" or "section 3"
    text: str


class KeyFactOut(BaseModel):
    label: str
    value: str
    page: int
    quote: str


class Coverage(BaseModel):
    pages_read: int
    page_count: int


class InsightsReadyOut(BaseModel):
    status: Literal["ready"]
    title: str
    document_type: str
    summary: str
    key_facts: list[KeyFactOut]
    tags: list[str]
    suggested_questions: list[str]
    coverage: Coverage
    model: str
    prompt_version: str


class InsightsPendingOut(BaseModel):
    status: Literal["pending"]


class InsightsFailedOut(BaseModel):
    status: Literal["failed"]
    error: ErrorBody


class InsightsUnavailableOut(BaseModel):
    status: Literal["unavailable"]
    reason: str


InsightsOut = Annotated[
    InsightsReadyOut | InsightsPendingOut | InsightsFailedOut | InsightsUnavailableOut,
    Field(discriminator="status"),
]


# ------------------------------------------------------------------ chat


class StartChatIn(BaseModel):
    document_ids: list[UUID] = Field(default_factory=list, description="Empty means every ready document.")
    title: str | None = None


class ChatOut(BaseModel):
    id: UUID
    title: str | None
    documents: list[dict[str, str]]    # [{"id", "filename"}]
    created_at: datetime


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class CitationOut(BaseModel):
    ref: int                           # matches [n] in answer
    document_id: UUID
    filename: str
    page: int
    page_label: str
    quote: str
    chunk_id: UUID
    page_url: str


class UsageOut(BaseModel):
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None


class TurnOut(BaseModel):
    id: UUID
    question: str
    interpreted_as: str
    status: Literal["answered", "not_found"]
    reason: Literal["no_evidence", "model_declined", "unverified"] | None   # only when not_found
    answer: str
    citations: list[CitationOut]
    usage: UsageOut
    latency_ms: int
    created_at: datetime


class HistoryOut(BaseModel):
    chat_id: UUID
    turns: list[TurnOut]


# ------------------------------------------------------------------ mapping


def document_out(doc: Document) -> DocumentOut:
    parsed, progress, error = None, None, None
    match doc.lifecycle:
        case Queued():
            status = DocStatus.QUEUED
        case Parsing():
            status = DocStatus.PARSING
        case Embedding(parsed, embedded_chunks):
            status = DocStatus.EMBEDDING
            progress = Progress(embedded_chunks=embedded_chunks, total_chunks=parsed.chunk_count)
        case Ready(parsed):
            status = DocStatus.READY
        case Failed(failure, parsed):
            status = DocStatus.FAILED
            error = ErrorBody(code=failure.code, message=failure.message, retryable=failure.retryable)
        case _:
            assert_never(doc.lifecycle)
    match doc.insights:
        case InsightsPending():
            insights_status = "pending"
        case InsightsReady():
            insights_status = "ready"
        case InsightsFailed():
            insights_status = "failed"
        case InsightsUnavailable():
            insights_status = "unavailable"
        case _:
            assert_never(doc.insights)
    return DocumentOut(
        id=doc.id, filename=doc.filename, kind=doc.kind, size_bytes=doc.size_bytes, status=status, progress=progress,
        pages=parsed and parsed.page_count, page_basis=parsed and parsed.basis,
        empty_pages=list(parsed.empty_pages) if parsed else [],
        insights_status=insights_status, error=error, processing_ms=doc.processing_ms,
        created_at=doc.created_at,
        links={"self": f"/documents/{doc.id}", "insights": f"/documents/{doc.id}/insights",
               "pages": f"/documents/{doc.id}/pages/{{n}}"})


def page_out(doc_id: UUID, basis: PageBasis, page: Page) -> PageOut:
    return PageOut(document_id=doc_id, number=page.number, label=pages.page_label(basis, page.number), text=page.text)


def insights_out(doc: Document) -> InsightsReadyOut | InsightsPendingOut | InsightsFailedOut | InsightsUnavailableOut:
    raise NotImplementedError


def turn_out(turn: Turn) -> TurnOut:
    """Answered -> status answered, citations numbered 1..n in tuple order.
    NotFound -> status not_found, reason from NotFound.reason, citations []."""
    raise NotImplementedError
