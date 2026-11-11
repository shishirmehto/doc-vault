"""HTTP request and response models, and the domain -> wire mapping.

Only api.py imports this module. Domain unions are flattened here into the
JSON shapes in the README; discriminated unions keyed on `status` keep the
OpenAPI schema honest about which fields exist in which state.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Annotated, Literal, assert_never
from uuid import UUID

from pydantic import BaseModel, Field

from . import pages
from .domain import (
    Answered,
    Chat,
    DocKind,
    DocStatus,
    Document,
    Embedding,
    Failed,
    Failure,
    InsightsFailed,
    InsightsPending,
    InsightsReady,
    InsightsStatus,
    InsightsUnavailable,
    Page,
    PageBasis,
    Parsing,
    Purpose,
    Queued,
    Ready,
    Step,
    Turn,
    TurnOutcome,
)
from .metrics import DocumentStats, ProcessingStats

# ------------------------------------------------------------------ shared


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool | None = None
    details: dict[str, object] = {}


class ErrorOut(BaseModel):
    error: ErrorBody


class HealthOut(BaseModel):
    db: Literal["ok", "unreachable"]
    redis: Literal["ok", "unreachable"]
    ai_mode: Literal["live", "fake", "mixed"]
    embed_model: str
    max_upload_bytes: int              # POST /documents refuses larger files with 413 file_too_large


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
    pages: int | None                  # None until parsed, then the stored count in every status
    page_basis: PageBasis | None
    empty_pages: list[int] = []
    insights_status: InsightsStatus
    error: ErrorBody | None            # only when status == "failed"
    failed_stage: Literal["parse", "embed"] | None   # only when status == "failed": the step that failed
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
    page_label: str                    # "p. 3" or "section 3"
    quote: str                         # the source text on that page, whitespace collapsed
    page_url: str


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
    coverage: Coverage
    dropped_key_facts: int             # cited by the model, but the quote was not on the cited page
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
    document_ids: list[UUID] = Field(min_length=1, description="The documents the chat answers from, fixed for its life.")
    title: str | None = None


class ChatDocumentOut(BaseModel):
    id: UUID
    filename: str                      # as it was when the chat started
    deleted: bool                      # true: the chat answers no new questions (409 documents_deleted)


class ChatOut(BaseModel):
    id: UUID
    title: str | None
    documents: list[ChatDocumentOut]
    created_at: datetime


class ChatPage(BaseModel):
    items: list[ChatOut]
    next_cursor: str | None


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
    cost_usd: float | None              # null when any call used a model with no price


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


# ------------------------------------------------------------------ metrics


class PercentilesOut(BaseModel):
    p50: int | None                    # null when the window holds no sample
    p95: int | None


class DocumentMetricsOut(BaseModel):
    total: int
    by_status: dict[DocStatus, int]
    by_kind: dict[DocKind, int]
    insights: dict[InsightsStatus, int]
    pages: int
    chunks: int
    bytes: int
    failures_by_code: dict[str, int]   # documents failed now, by error code


class StepMetricsOut(BaseModel):
    runs: int
    ok: int
    failed: int
    abandoned: int                     # crashed runs, closed by the next worker
    retries: int                       # runs with attempt > 1
    duration_ms: PercentilesOut        # ok runs only
    failures_by_code: dict[str, int]


class AIUsageOut(BaseModel):
    purpose: Purpose
    model: str
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float                    # priced calls only
    unpriced_calls: int
    latency_ms: PercentilesOut


class AIMetricsOut(BaseModel):
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    unpriced_calls: int
    by_purpose_and_model: list[AIUsageOut]


class ChatMetricsOut(BaseModel):
    turns: int
    outcomes: dict[TurnOutcome, int]
    latency_ms: PercentilesOut


class ProcessingMetricsOut(BaseModel):
    window: str
    since: datetime
    uploaded: int                      # documents created in the window that still exist
    indexed: int                       # documents with a successful embed in the window
    time_to_ready_ms: PercentilesOut   # upload to first successful embed
    queue_wait_ms: PercentilesOut      # upload to first parse start
    steps: dict[Step, StepMetricsOut]
    ai: AIMetricsOut
    chat: ChatMetricsOut


# ------------------------------------------------------------------ mapping


def error_body(failure: Failure, details: dict[str, object] | None = None) -> ErrorBody:
    return ErrorBody(code=failure.code, message=failure.message, retryable=failure.retryable, details=details or {})


def document_out(doc: Document) -> DocumentOut:
    parsed, progress, error, failed_stage = None, None, None, None
    match doc.lifecycle:
        case Queued(parsed):
            status = DocStatus.QUEUED
        case Parsing():
            status = DocStatus.PARSING
        case Embedding(parsed, embedded_chunks):
            status = DocStatus.EMBEDDING
            progress = Progress(embedded_chunks=embedded_chunks, total_chunks=parsed.chunk_count)
        case Ready(parsed):
            status = DocStatus.READY
        case Failed(failure, parsed) as failed:
            status = DocStatus.FAILED
            error = error_body(failure)
            failed_stage = failed.stage
        case _:
            assert_never(doc.lifecycle)
    match doc.insights:
        case InsightsPending():
            insights_status = InsightsStatus.PENDING
        case InsightsReady():
            insights_status = InsightsStatus.READY
        case InsightsFailed():
            insights_status = InsightsStatus.FAILED
        case InsightsUnavailable():
            insights_status = InsightsStatus.UNAVAILABLE
        case _:
            assert_never(doc.insights)
    return DocumentOut(
        id=doc.id, filename=doc.filename, kind=doc.kind, size_bytes=doc.size_bytes, status=status, progress=progress,
        pages=parsed and parsed.page_count, page_basis=parsed and parsed.basis,
        empty_pages=list(parsed.empty_pages) if parsed else [],
        insights_status=insights_status, error=error, failed_stage=failed_stage, processing_ms=doc.processing_ms,
        created_at=doc.created_at,
        links={"self": f"/documents/{doc.id}", "insights": f"/documents/{doc.id}/insights",
               "pages": f"/documents/{doc.id}/pages/{{n}}"})


def page_out(doc_id: UUID, basis: PageBasis, page: Page) -> PageOut:
    return PageOut(document_id=doc_id, number=page.number, label=pages.page_label(basis, page.number), text=page.text)


def insights_out(doc: Document) -> InsightsReadyOut | InsightsPendingOut | InsightsFailedOut | InsightsUnavailableOut:
    match doc.insights:
        case InsightsReady(body=body) as ready:
            return InsightsReadyOut(
                status="ready", title=body.title, document_type=body.document_type, summary=body.summary,
                key_facts=[KeyFactOut(label=fact.label, value=fact.value, page=fact.page,
                                      page_label=pages.page_label(ready.page_basis, fact.page), quote=fact.quote,
                                      page_url=_page_url(doc.id, fact.page)) for fact in body.key_facts],
                tags=list(body.tags), coverage=Coverage(pages_read=ready.pages_read, page_count=ready.page_count),
                dropped_key_facts=ready.dropped_key_facts, model=ready.model, prompt_version=ready.prompt_version)
        case InsightsPending():
            return InsightsPendingOut(status="pending")
        case InsightsFailed(failure):
            return InsightsFailedOut(status="failed", error=error_body(failure))
        case InsightsUnavailable():
            return InsightsUnavailableOut(status="unavailable",
                                          reason="This document failed to process, so it has no insights.")
        case _:
            assert_never(doc.insights)


def chat_out(chat: Chat) -> ChatOut:
    return ChatOut(id=chat.id, title=chat.title, created_at=chat.created_at,
                   documents=[ChatDocumentOut(id=doc.id, filename=doc.filename, deleted=doc.deleted)
                              for doc in chat.documents])


def turn_out(turn: Turn) -> TurnOut:
    """Citations are numbered 1..n in tuple order, matching the [n] markers in the answer."""
    answer = turn.answer
    answered = isinstance(answer, Answered)
    return TurnOut(
        id=turn.id, question=turn.question, interpreted_as=turn.standalone_query,
        status="answered" if answered else "not_found", reason=None if answered else answer.reason,
        answer=answer.text,
        citations=[CitationOut(ref=ref, document_id=c.document_id, filename=c.filename, page=c.page,
                               page_label=pages.page_label(c.page_basis, c.page), quote=c.quote, chunk_id=c.chunk_id,
                               page_url=_page_url(c.document_id, c.page))
                   for ref, c in enumerate(answer.citations, start=1)],
        usage=UsageOut.model_validate(turn.usage, from_attributes=True),
        latency_ms=round((turn.finished_at - turn.started_at).total_seconds() * 1000), created_at=turn.started_at)


def _page_url(doc_id: UUID, page: int) -> str:
    """The GET /documents/{id}/pages/{n} route a citation links to."""
    return f"/documents/{doc_id}/pages/{page}"


def document_metrics_out(stats: DocumentStats) -> DocumentMetricsOut:
    return DocumentMetricsOut.model_validate(dataclasses.asdict(stats))


def processing_metrics_out(window: str, stats: ProcessingStats) -> ProcessingMetricsOut:
    return ProcessingMetricsOut.model_validate({"window": window, **dataclasses.asdict(stats)})
