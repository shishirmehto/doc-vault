"""Side-by-side comparison of a few ready documents. The only writer of comparisons.

create() is the whole path in one function a reader can follow top to bottom:

  check the documents are ready -> read their pages and verified key facts -> compare prompt
  (each document an even share of the budget) -> one model call -> ground (check every cell's quote) -> persist
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from . import documents, insights, prompts
from .ai import USAGE_SQL, ComparisonOwner, usage_of
from .context import Ctx
from .db import Db, decode_cursor, next_cursor
from .domain import (
    Cell,
    ComparedDocument,
    Comparison,
    ComparisonId,
    DocumentId,
    ErrorCode,
    Page,
    PageBasis,
    Row,
    VaultError,
)
from .pages import collapse_whitespace, find_on_page
from .prompts import CellDraft, CompareDoc, ComparisonDraft

MAX_ROWS = 30


def create(ctx: Ctx, document_ids: Sequence[DocumentId], focus: str | None) -> Comparison:
    """Compare the documents field by field. Persists exactly one comparison.

    Unknown ids -> DOCUMENT_NOT_FOUND (404) and any id not READY ->
    DOCUMENTS_NOT_READY (409), as chat.start refuses them. A blank focus is
    stored as null. The model call is billed to the comparison id, which exists
    before it. A provider failure raises VaultError and stores no comparison;
    the failed calls stay in llm_calls. No connection or transaction is held
    across the model call, so a document deleted meanwhile does not fail the
    insert and reads back as deleted.
    """
    comparison_id = ComparisonId(uuid4())
    focus = (focus or "").strip() or None
    with ctx.db.connection() as conn, conn.transaction():
        refs = documents.ready_documents(conn, document_ids)
        ids = [ref.id for ref in refs]
        pages: dict[DocumentId, list[Page]] = {}
        for row in conn.execute("SELECT document_id, number, text FROM pages WHERE document_id = ANY(%s)"
                                " ORDER BY number", (ids,)):
            pages.setdefault(row["document_id"], []).append(Page(row["number"], row["text"]))
        facts = insights.ready_key_facts(conn, ids)
        versions = {row["id"]: row["version"] for row in conn.execute(
            "SELECT id, version FROM documents WHERE id = ANY(%s)", (ids,))}
    docs = [CompareDoc(f"D{n}", ref.filename, ref.page_basis, facts.get(ref.id, ()), tuple(pages[ref.id]))
            for n, ref in enumerate(refs, start=1)]

    prompt, pages_read = prompts.compare(docs, focus, budget_chars=ctx.settings.insights_max_chars)
    draft = ctx.ai.complete(prompt, ComparisonDraft, owner=ComparisonOwner(comparison_id))
    rows, dropped = ground(draft, docs)

    snapshot = [{"id": str(ref.id), "filename": ref.filename, "version": versions[ref.id], "page_basis": ref.page_basis,
                 "pages_read": read, "page_count": doc.pages[-1].number}
                for ref, doc, read in zip(refs, docs, pages_read, strict=True)]
    with ctx.db.connection() as conn:
        conn.execute(
            "INSERT INTO comparisons (id, focus, documents, rows, dropped_cells, model, prompt_version)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (comparison_id, focus, Jsonb(snapshot), Jsonb([_row_json(row) for row in rows]), dropped,
             ctx.ai.chat.models[prompt.tier], prompt.version))
        return _row_to_comparison(conn.execute(_COMPARISON_SQL + "WHERE c.id = %s GROUP BY c.id",
                                               (comparison_id,)).fetchone())


def get(db: Db, comparison_id: ComparisonId) -> Comparison:
    """A comparison with its usage, deleted documents marked. Raises COMPARISON_NOT_FOUND."""
    with db.connection() as conn:
        row = conn.execute(_COMPARISON_SQL + "WHERE c.id = %s GROUP BY c.id", (comparison_id,)).fetchone()
    if row is None:
        raise VaultError(ErrorCode.COMPARISON_NOT_FOUND, f"No comparison has the id {comparison_id}.")
    return _row_to_comparison(row)


def list_comparisons(db: Db, *, limit: int, cursor: str | None) -> tuple[list[Comparison], str | None]:
    """Newest first. Keyset pagination on (created_at DESC, id DESC), the same cursor as chat.list_chats."""
    after = decode_cursor(cursor) if cursor else None
    with db.connection() as conn:
        rows = conn.execute(
            _COMPARISON_SQL + "WHERE (%(created_at)s::timestamptz IS NULL"
            " OR (c.created_at, c.id) < (%(created_at)s::timestamptz, %(id)s::uuid))"
            " GROUP BY c.id ORDER BY c.created_at DESC, c.id DESC LIMIT %(limit)s",
            {"created_at": after and after[0], "id": after and after[1], "limit": limit + 1}).fetchall()
    return [_row_to_comparison(row) for row in rows[:limit]], next_cursor(rows, limit)


def ground(draft: ComparisonDraft, documents: Sequence[CompareDoc]) -> tuple[tuple[Row, ...], int]:
    """Keep each cell only when pages.find_on_page finds its quote on the page it cites. Pure.

    Cells are placed by document label ("D2", matched ignoring case and
    surrounding spaces), one column per document. The first cell draft naming a
    document claims its column; a later one naming it again, and one naming no
    document, is ignored. A blank or null value is an empty cell. A value with
    no page or quote, or whose quote is not on that page of that document,
    becomes an empty cell. A kept cell quotes the source span, not the model's
    copy. A row left with every cell empty is removed. The row drafts are read
    in order until MAX_ROWS rows are kept, and the rest are not read.

    Returns the rows and the dropped count: the cell drafts with a non-blank
    value, in the row drafts read (removed rows included), that did not become
    a cell, because they name no document or one an earlier cell draft in the
    row named, lack a page or quote, or quote what is not on the page they cite.
    """
    column = {doc.label.casefold(): at for at, doc in enumerate(documents)}
    rows: list[Row] = []
    dropped = 0
    for row in draft.rows:
        if len(rows) == MAX_ROWS:
            break
        cells: list[Cell | None] = [None] * len(documents)
        claimed: set[int] = set()
        for cell in row.cells:
            at = column.get(cell.document.strip().casefold())
            if at is not None and at not in claimed:
                claimed.add(at)
                cells[at] = _grounded(cell, documents[at].pages)
                if cells[at] is not None:
                    continue
            if cell.value and cell.value.strip():
                dropped += 1
        if any(cell is not None for cell in cells):
            rows.append(Row(collapse_whitespace(row.field), tuple(cells)))
    return tuple(rows), dropped


def _grounded(cell: CellDraft, pages: Sequence[Page]) -> Cell | None:
    if not (cell.value and cell.value.strip()) or cell.page is None or cell.quote is None:
        return None
    found = find_on_page(cell.quote, pages, cell.page)
    return None if found is None else Cell(collapse_whitespace(cell.value), cell.page, found)


_COMPARISON_SQL = f"""
SELECT c.id, c.focus, c.rows, c.dropped_cells, c.model, c.prompt_version, c.created_at,
       (SELECT jsonb_agg(doc || jsonb_build_object('deleted', d.id IS NULL) ORDER BY at)
        FROM jsonb_array_elements(c.documents) WITH ORDINALITY AS snapshot (doc, at)
        LEFT JOIN documents d ON d.id = (doc ->> 'id')::uuid) AS documents,
       {USAGE_SQL}
FROM comparisons c LEFT JOIN llm_calls l ON l.comparison_id = c.id
"""


def _row_json(row: Row) -> dict[str, object]:
    return {"field": row.field, "cells": [None if cell is None else dataclasses.asdict(cell) for cell in row.cells]}


def _row_to_comparison(row: Mapping[str, Any]) -> Comparison:
    return Comparison(
        id=ComparisonId(row["id"]),
        documents=tuple(ComparedDocument(DocumentId(UUID(doc["id"])), doc["filename"], doc.get("version"),
                                         PageBasis(doc["page_basis"]), doc["pages_read"], doc["page_count"], doc["deleted"])
                        for doc in row["documents"]),
        focus=row["focus"],
        rows=tuple(Row(stored["field"], tuple(None if cell is None else Cell(**cell) for cell in stored["cells"]))
                   for stored in row["rows"]),
        dropped_cells=row["dropped_cells"], model=row["model"], prompt_version=row["prompt_version"],
        usage=usage_of(row),
        created_at=row["created_at"])
