"""Domain types. No I/O, no framework imports.

Rows, HTTP payloads and LLM JSON are parsed into these types at the
boundary (documents._row_to_document, wire.py, ai.AI.complete) and
trusted everywhere else. Sum types replace status-plus-optional-fields, so a
"ready document with an error" or an "answer with zero citations" cannot be
constructed.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, NewType
from uuid import UUID

DocumentId = NewType("DocumentId", UUID)
ChunkId = NewType("ChunkId", UUID)
ChatId = NewType("ChatId", UUID)
TurnId = NewType("TurnId", UUID)
ComparisonId = NewType("ComparisonId", UUID)
FamilyId = NewType("FamilyId", UUID)
SummaryId = NewType("SummaryId", UUID)
Sha256 = NewType("Sha256", bytes)


class DocKind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"


class DocStatus(StrEnum):
    """Lifecycle of the searchable index. Transition table lives in documents.TRANSITIONS."""

    QUEUED = "queued"
    PARSING = "parsing"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"


class PageBasis(StrEnum):
    """What a page number means for this document.

    PHYSICAL: PDF page, 1-based, matches the viewer.
    SYNTHETIC: DOCX and TXT carry no reliable page information; ~3,000-character
    sections cut at paragraph boundaries. The API labels these "section n",
    never "page n".
    """

    PHYSICAL = "physical"
    SYNTHETIC = "synthetic"


class Step(StrEnum):
    PARSE = "parse"
    EMBED = "embed"
    ANALYZE = "analyze"


class StepOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    ABANDONED = "abandoned"   # left open by a crashed run, closed by the next lease holder


class Purpose(StrEnum):
    EMBED_CHUNKS = "embed_chunks"
    EMBED_QUERY = "embed_query"
    INSIGHTS = "insights"
    CONDENSE = "condense"
    ANSWER = "answer"
    COMPARE = "compare"
    SUMMARY = "summary"


class InsightsStatus(StrEnum):
    """What GET /documents/{id}/insights reads as. Each value names one InsightsState variant."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class TurnOutcome(StrEnum):
    ANSWERED = "answered"
    NO_EVIDENCE = "no_evidence"        # retrieval gate refused before any LLM call
    MODEL_DECLINED = "model_declined"  # the model said the sources do not support an answer
    UNVERIFIED = "unverified"          # the model answered but no quote matched its source


class ErrorCode(StrEnum):
    # Rejected at upload, never stored.
    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    UNSUPPORTED_TYPE = "unsupported_type"
    # Stored on a failed document or failed insights.
    ENCRYPTED_PDF = "encrypted_pdf"
    UNREADABLE_FILE = "unreadable_file"
    NO_EXTRACTABLE_TEXT = "no_extractable_text"   # scanned PDF; OCR is out of scope
    TOO_MANY_PAGES = "too_many_pages"
    AI_UNAVAILABLE = "ai_unavailable"             # timeout, 429, 5xx after retries
    AI_BAD_OUTPUT = "ai_bad_output"               # JSON failed schema, after one repair turn where the purpose gets one
    INTERNAL = "internal_error"                   # an unexpected exception in a task (a bug); retryable
    # Request errors.
    DOCUMENT_NOT_FOUND = "document_not_found"
    PAGE_NOT_FOUND = "page_not_found"
    CHAT_NOT_FOUND = "chat_not_found"
    COMPARISON_NOT_FOUND = "comparison_not_found"
    DOCUMENTS_NOT_READY = "documents_not_ready"
    DOCUMENTS_DELETED = "documents_deleted"
    NOTHING_TO_RETRY = "nothing_to_retry"
    VERSION_CONFLICT = "version_conflict"         # an upload lost more version-number races than it retries
    INVALID_CURSOR = "invalid_cursor"
    INVALID_REQUEST = "invalid_request"           # a parameter or body failed validation
    RATE_LIMITED = "rate_limited"                 # the client spent its per-minute budget for this route
    TOO_MANY_STREAMS = "too_many_streams"
    QUOTA_EXCEEDED = "quota_exceeded"             # today's AI spend reached DAILY_AI_BUDGET_USD; also stored on insights


@dataclass(frozen=True, slots=True)
class Failure:
    code: ErrorCode
    message: str        # written for the end user, e.g. "This PDF has no text layer (scanned?)"
    retryable: bool     # True means POST /documents/{id}/retry can succeed without a new file


class VaultError(Exception):
    """The only exception type that crosses module boundaries.

    api.py maps code -> HTTP status in one table. The pipeline maps
    retryable -> Celery retry, otherwise a terminal Failed state.
    """

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False,
                 details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.failure = Failure(code, message, retryable)
        self.details = details or {}


# ---------------------------------------------------------------- documents


@dataclass(frozen=True, slots=True)
class Parsed:
    """Facts recorded once by the parse checkpoint. Immutable afterwards."""

    basis: PageBasis
    page_count: int
    chunk_count: int
    empty_pages: tuple[int, ...]   # pages that produced no text; never cited


@dataclass(frozen=True, slots=True)
class Queued:
    parsed: Parsed | None   # set when a retry re-queued a document that failed after its parse checkpoint


@dataclass(frozen=True, slots=True)
class Parsing:
    pass


@dataclass(frozen=True, slots=True)
class Embedding:
    parsed: Parsed
    embedded_chunks: int    # derived at read time: count(chunks with current-model embedding)


@dataclass(frozen=True, slots=True)
class Ready:
    parsed: Parsed


@dataclass(frozen=True, slots=True)
class Failed:
    failure: Failure
    parsed: Parsed | None   # None when parsing itself failed

    @property
    def stage(self) -> Literal[Step.PARSE, Step.EMBED]:
        """The step that failed. Parse facts are written once, by the parse checkpoint, so their presence says
        whether parsing finished."""
        return Step.PARSE if self.parsed is None else Step.EMBED


type Lifecycle = Queued | Parsing | Embedding | Ready | Failed


@dataclass(frozen=True, slots=True)
class KeyFact:
    label: str      # "Each occurrence limit"
    value: str      # "$1,000,000"
    page: int
    quote: str      # verified to occur on `page`


@dataclass(frozen=True, slots=True)
class InsightsBody:
    title: str
    document_type: str              # one of prompts.DOCUMENT_TYPES
    summary: str
    key_facts: tuple[KeyFact, ...]  # only facts whose quote was found on the cited page
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InsightsPending:
    pass


@dataclass(frozen=True, slots=True)
class InsightsReady:
    body: InsightsBody
    pages_read: int         # coverage: pages 1..pages_read fit the prompt budget
    page_count: int
    page_basis: PageBasis   # labels each key fact's page
    dropped_key_facts: int  # facts the model cited whose quote was not on the cited page
    model: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class InsightsFailed:
    failure: Failure


@dataclass(frozen=True, slots=True)
class InsightsUnavailable:
    """The document itself failed, so insights will never run."""


type InsightsState = InsightsPending | InsightsReady | InsightsFailed | InsightsUnavailable


@dataclass(frozen=True, slots=True)
class Version:
    """Where a document sits among the uploads of one file. Numbers are never renumbered, so a deleted version leaves
    a gap. A new version takes the highest number in the family plus one, so a deleted latest number is used again."""

    family_id: FamilyId
    number: int
    latest: int


@dataclass(frozen=True, slots=True)
class Document:
    id: DocumentId
    filename: str
    kind: DocKind
    size_bytes: int
    lifecycle: Lifecycle
    insights: InsightsState     # merged at read time from document_insights; documents.py never writes it
    created_at: datetime
    processing_ms: int | None   # derived from step_runs: last ok embed finish - created_at
    version: Version


@dataclass(frozen=True, slots=True)
class Created:
    document: Document


@dataclass(frozen=True, slots=True)
class Existing:
    """Same bytes were uploaded before. No new row, no new processing, no new cost."""

    document: Document


type UploadOutcome = Created | Existing


# ---------------------------------------------------------------- page provenance


@dataclass(frozen=True, slots=True)
class Page:
    number: int     # 1-based
    text: str       # may be "" (scanned or blank page)


@dataclass(frozen=True, slots=True)
class ExtractedText:
    basis: PageBasis
    pages: tuple[Page, ...]     # every page, numbered 1..n without gaps


@dataclass(frozen=True, slots=True)
class PageMap:
    """Where each page starts inside one chunk's text.

    Invariant: starts[0][0] == 0, offsets strictly increase, page numbers
    strictly increase (empty pages are skipped, so numbers may jump).
    """

    starts: tuple[tuple[int, int], ...]   # (offset_in_chunk, page_number)

    def page_at(self, offset: int) -> int:
        i = bisect_right([o for o, _ in self.starts], offset) - 1
        return self.starts[max(i, 0)][1]

    @property
    def first_page(self) -> int:
        return self.starts[0][1]

    @property
    def last_page(self) -> int:
        return self.starts[-1][1]


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """A slice [start, end) of the document text stream. text == stream[start:end] by construction."""

    ord: int
    start: int
    end: int
    text: str
    page_map: PageMap


# ---------------------------------------------------------------- chat


@dataclass(frozen=True, slots=True)
class DocRef:
    id: DocumentId
    filename: str
    page_basis: PageBasis


@dataclass(frozen=True, slots=True)
class ChatDocument:
    id: DocumentId
    filename: str       # snapshot taken when the chat started
    deleted: bool       # deleted since then; the chat refuses new questions


@dataclass(frozen=True, slots=True)
class Chat:
    id: ChatId
    title: str | None
    documents: tuple[ChatDocument, *tuple[ChatDocument, ...]]   # non-empty, fixed at start
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Source:
    """A retrieved chunk as the answer prompt sees it. label is "S1".."Sk", unique per turn."""

    label: str
    chunk_id: ChunkId
    doc: DocRef
    text: str
    page_map: PageMap
    similarity: float   # cosine similarity, 1 - distance
    rrf: float          # fused rank score used for ordering


@dataclass(frozen=True, slots=True)
class Citation:
    document_id: DocumentId
    filename: str
    page: int
    page_basis: PageBasis
    quote: str          # verbatim span of the source text (the matched span, not the model's copy)
    chunk_id: ChunkId


@dataclass(frozen=True, slots=True)
class Answered:
    text: str           # markers [1]..[n] index citations, 1-based
    citations: tuple[Citation, *tuple[Citation, ...]]   # at least one, by construction

    @property
    def outcome(self) -> TurnOutcome:
        return TurnOutcome.ANSWERED


@dataclass(frozen=True, slots=True)
class NotFound:
    reason: Literal[TurnOutcome.NO_EVIDENCE, TurnOutcome.MODEL_DECLINED, TurnOutcome.UNVERIFIED]
    text: str           # user-facing: what was searched and what is missing

    @property
    def outcome(self) -> TurnOutcome:
        return self.reason

    @property
    def citations(self) -> tuple[Citation, ...]:
        return ()


type Answer = Answered | NotFound


@dataclass(frozen=True, slots=True)
class Usage:
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None    # None if any call used an unpriced model


@dataclass(frozen=True, slots=True)
class Turn:
    id: TurnId
    chat_id: ChatId
    question: str
    standalone_query: str
    answer: Answer
    started_at: datetime
    finished_at: datetime
    usage: Usage                # aggregated from llm_calls WHERE turn_id = id
    suggestions: tuple[str, ...]
    cached: bool                # served from the answer cache: no model call, usage all zero


@dataclass(frozen=True, slots=True)
class ComparedDocument:
    """One column of a comparison. Everything but deleted is a snapshot taken when the comparison ran, so its
    labels and coverage survive a delete."""

    id: DocumentId
    filename: str
    version: int | None
    page_basis: PageBasis
    pages_read: int
    page_count: int
    deleted: bool


@dataclass(frozen=True, slots=True)
class Cell:
    value: str
    page: int
    quote: str


_CURRENCY = re.compile(r"[$€£]|(?<![a-z])(?:usd|eur|gbp)(?![a-z])")
_CURRENCY_CODE = {"$": "usd", "€": "eur", "£": "gbp"}
# 1,000 and 12,500,000 but not 1,5 (a decimal comma), 1,2 (a list) or 1,000,00.
_THOUSANDS = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?!\d|,\d)")
_ZERO_CENTS = re.compile(r"(?<=\d)\.00(?!\d)")
_SENTENCE_END = ".,;:!? "


def _comparable(value: str) -> tuple[tuple[str, ...], str]:
    """The currencies in the order they appear, and the text without them. A sign such as + or - is kept."""
    text = value.casefold()
    currencies = tuple(_CURRENCY_CODE.get(mark, mark) for mark in _CURRENCY.findall(text))
    text = _THOUSANDS.sub(lambda group: group[0].replace(",", ""), _CURRENCY.sub("", text))
    return currencies, " ".join(_ZERO_CENTS.sub("", text).split()).rstrip(_SENTENCE_END)


@dataclass(frozen=True, slots=True)
class Row:
    field: str
    cells: tuple[Cell | None, ...]

    @property
    def differs(self) -> bool:
        """Two or more distinct values, values whose currencies differ or come in another order (a value naming none
        fits any), or an empty cell beside a filled one. Decided here, never by the model."""
        values = [_comparable(cell.value) for cell in self.cells if cell is not None]
        texts = {text for _, text in values}
        currencies = {marks for marks, _ in values if marks}
        return len(texts) > 1 or len(currencies) > 1 or (bool(values) and None in self.cells)


@dataclass(frozen=True, slots=True)
class Comparison:
    id: ComparisonId
    documents: tuple[ComparedDocument, ...]
    focus: str | None
    rows: tuple[Row, ...]
    dropped_cells: int
    model: str
    prompt_version: str
    usage: Usage
    created_at: datetime


class SummaryLength(StrEnum):
    BRIEF = "brief"
    STANDARD = "standard"
    DETAILED = "detailed"


class SummaryTone(StrEnum):
    PLAIN = "plain"
    FORMAL = "formal"
    CLIENT = "client"


@dataclass(frozen=True, slots=True)
class SummaryOptions:
    """What a reader asked for. The options are the cache key, so focus is stored as normalised: the prompt gets
    the same text, and one key always means one model input."""

    length: SummaryLength
    focus: str | None       # trimmed, lowercase, single-spaced; None when blank
    tone: SummaryTone

    @classmethod
    def of(cls, length: SummaryLength, focus: str | None, tone: SummaryTone) -> SummaryOptions:
        """' Deductibles  ' -> 'deductibles'; a blank or absent focus -> None."""
        return cls(length, " ".join((focus or "").casefold().split()) or None, tone)


@dataclass(frozen=True, slots=True)
class SummarySource:
    page: int
    quote: str


@dataclass(frozen=True, slots=True)
class Summary:
    id: SummaryId
    document_id: DocumentId
    options: SummaryOptions
    text: str
    sources: tuple[SummarySource, ...]
    dropped_sources: int
    page_basis: PageBasis
    model: str
    prompt_version: str
    cached: bool
    created_at: datetime
