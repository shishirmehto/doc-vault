"""Summaries of one ready document in the length, focus and tone a reader picks. The only writer of
document_summaries.

create() is the whole path in one function a reader can follow top to bottom:

  check the document is ready -> serve a stored summary for the same options, model and prompt version
  -> otherwise read its pages -> summary prompt -> one model call -> ground (check every source's quote) -> persist

ground is pure, so the grounding rule is unit-tested without a model or a database. SummaryOptions.of (domain.py)
normalises the focus, so the options are both the cache key and the prompt's input.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb

from . import chat, prompts
from .ai import SummaryOwner
from .context import Ctx
from .db import Db, decode_cursor, encode_cursor
from .domain import (
    DocumentId,
    ErrorCode,
    Page,
    PageBasis,
    Summary,
    SummaryId,
    SummaryLength,
    SummaryOptions,
    SummarySource,
    SummaryTone,
    VaultError,
)
from .pages import collapse_whitespace, find_on_page
from .prompts import SUMMARY_VERSION, SummaryDraft

MAX_SOURCES = 6


def create(ctx: Ctx, document_id: DocumentId, options: SummaryOptions) -> Summary:
    """The document summarised with these options. Persists at most one summary per options, model and prompt version.

    Unknown id -> DOCUMENT_NOT_FOUND (404) and a document not READY ->
    DOCUMENTS_NOT_READY (409), as chat.start refuses them. A stored summary for
    the same key comes back with cached=True and no model call. Otherwise the
    model call is billed to the new summary's id, which exists before it, and no
    connection or transaction is held across it. A provider failure raises
    VaultError and stores nothing; its llm_calls rows stay. When an identical
    request stored first, its row comes back with cached=False, because this
    request did call the model. A document deleted during the call -> DOCUMENT_NOT_FOUND.
    """
    key = {"document_id": document_id, "length": options.length, "focus_key": options.focus or "",
           "tone": options.tone, "model": ctx.ai.chat.models["main"], "prompt_version": SUMMARY_VERSION}
    with ctx.db.connection() as conn, conn.transaction():
        [doc] = chat.ready_documents(conn, [document_id])
        stored = conn.execute(_SUMMARY_SQL + _BY_KEY, key).fetchone()
        if stored is not None:
            return _row_to_summary(stored, cached=True)
        pages = [Page(row["number"], row["text"]) for row in conn.execute(
            "SELECT number, text FROM pages WHERE document_id = %s ORDER BY number", (document_id,))]

    summary_id = SummaryId(uuid4())
    prompt = prompts.summary(doc.filename, doc.page_basis, pages, options, budget_chars=ctx.settings.insights_max_chars)
    draft = ctx.ai.complete(prompt, SummaryDraft, owner=SummaryOwner(summary_id))
    sources, dropped = ground(draft, pages)

    with ctx.db.connection() as conn, conn.transaction():
        try:
            conn.execute(_INSERT, {**key, "id": summary_id, "text": collapse_whitespace(draft.summary),
                                   "sources": Jsonb([{"page": s.page, "quote": s.quote} for s in sources]),
                                   "dropped_sources": dropped})
        except ForeignKeyViolation:
            raise _deleted(document_id) from None
        row = conn.execute(_SUMMARY_SQL + _BY_KEY, key).fetchone()
    if row is None:   # an identical request stored first, and the document went before this read
        raise _deleted(document_id)
    return _row_to_summary(row, cached=False)


def list_summaries(db: Db, document_id: DocumentId, *, limit: int,
                   cursor: str | None) -> tuple[list[Summary], str | None]:
    """The document's stored summaries, newest first, each cached=True. Keyset pagination on (created_at DESC, id
    DESC), the same cursor as compare.list_comparisons. Raises DOCUMENT_NOT_FOUND; a document in any other state
    lists what it has, which is nothing until it has been ready."""
    after = decode_cursor(cursor) if cursor else None
    with db.connection() as conn:
        if conn.execute("SELECT 1 FROM documents WHERE id = %s", (document_id,)).fetchone() is None:
            raise VaultError(ErrorCode.DOCUMENT_NOT_FOUND, f"No document has the id {document_id}.")
        rows = conn.execute(
            _SUMMARY_SQL + "WHERE s.document_id = %(document_id)s AND (%(created_at)s::timestamptz IS NULL"
            " OR (s.created_at, s.id) < (%(created_at)s::timestamptz, %(id)s::uuid))"
            " ORDER BY s.created_at DESC, s.id DESC LIMIT %(limit)s",
            {"document_id": document_id, "created_at": after and after[0], "id": after and after[1],
             "limit": limit + 1}).fetchall()
    last = rows[limit - 1] if len(rows) > limit else None
    return ([_row_to_summary(row, cached=True) for row in rows[:limit]],
            last and encode_cursor(last["created_at"], last["id"]))


def ground(draft: SummaryDraft, pages: Sequence[Page]) -> tuple[tuple[SummarySource, ...], int]:
    """Keep each source only when pages.find_on_page finds its quote on the page it cites. Pure.

    A kept source quotes the source span, not the model's copy. A source whose
    page and span repeat a kept one is skipped and not counted. The drafts are
    read in order until MAX_SOURCES are kept, and the rest are not read. Keeping
    none is fine: a summary is not a fact claim, so it is returned without sources.

    Returns the sources and the dropped count: the drafts read whose quote is
    not on the page they cite.
    """
    kept: list[SummarySource] = []
    dropped = 0
    for source in draft.sources:
        if len(kept) == MAX_SOURCES:
            break
        found = find_on_page(source.quote, pages, source.page)
        if found is None:
            dropped += 1
        elif (cited := SummarySource(source.page, found)) not in kept:
            kept.append(cited)
    return tuple(kept), dropped


# ------------------------------------------------------------------ rows

# page_basis comes from the documents row at read time. A summary goes with its document, so the join keeps every row.
_SUMMARY_SQL = """
SELECT s.id, s.document_id, s.length, s.focus_key, s.tone, s.text, s.sources, s.dropped_sources, s.model,
       s.prompt_version, s.created_at, d.page_basis
FROM document_summaries s JOIN documents d ON d.id = s.document_id
"""

_BY_KEY = ("WHERE s.document_id = %(document_id)s AND s.length = %(length)s AND s.focus_key = %(focus_key)s"
           " AND s.tone = %(tone)s AND s.model = %(model)s AND s.prompt_version = %(prompt_version)s")

_INSERT = """
INSERT INTO document_summaries (id, document_id, length, focus_key, tone, text, sources, dropped_sources, model,
                                prompt_version)
VALUES (%(id)s, %(document_id)s, %(length)s, %(focus_key)s, %(tone)s, %(text)s, %(sources)s, %(dropped_sources)s,
        %(model)s, %(prompt_version)s)
ON CONFLICT (document_id, length, focus_key, tone, model, prompt_version) DO NOTHING
"""


def _deleted(document_id: DocumentId) -> VaultError:
    """The 404 chat.ready_documents gives for a missing id."""
    return VaultError(ErrorCode.DOCUMENT_NOT_FOUND, "1 of 1 documents do not exist.",
                      details={"missing": [str(document_id)]})


def _row_to_summary(row: Mapping[str, Any], *, cached: bool) -> Summary:
    """The database-to-domain boundary for summaries. focus_key '' is no focus."""
    return Summary(
        id=SummaryId(row["id"]), document_id=DocumentId(row["document_id"]),
        options=SummaryOptions(SummaryLength(row["length"]), row["focus_key"] or None, SummaryTone(row["tone"])),
        text=row["text"], sources=tuple(SummarySource(**source) for source in row["sources"]),
        dropped_sources=row["dropped_sources"], page_basis=PageBasis(row["page_basis"]), model=row["model"],
        prompt_version=row["prompt_version"], cached=cached, created_at=row["created_at"])
