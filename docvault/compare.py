"""Side-by-side comparison of a few ready documents. The only writer of comparisons.

create() is the whole path in one function a reader can follow top to bottom:

  check the documents are ready -> read their pages and verified key facts -> compare prompt
  (each document an even share of the budget) -> one model call -> ground (check every cell's quote) -> persist

ground is pure, and Row.differs (domain.py) decides a difference from the values in code, so the
grounding and difference rules are unit-tested without a model or a database.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from . import chat, prompts
from .ai import ComparisonOwner
from .context import Ctx
from .domain import (
    Cell,
    ComparedDocument,
    Comparison,
    ComparisonId,
    DocumentId,
    KeyFact,
    Page,
    PageBasis,
    Row,
    Usage,
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
        refs = chat.ready_documents(conn, document_ids)
        ids = [ref.id for ref in refs]
        pages: dict[DocumentId, list[Page]] = {}
        for row in conn.execute("SELECT document_id, number, text FROM pages WHERE document_id = ANY(%s)"
                                " ORDER BY number", (ids,)):
            pages.setdefault(row["document_id"], []).append(Page(row["number"], row["text"]))
        facts = {row["document_id"]: tuple(KeyFact(**fact) for fact in row["body"]["key_facts"])
                 for row in conn.execute("SELECT document_id, body FROM document_insights"
                                         " WHERE document_id = ANY(%s) AND status = 'ready'", (ids,))}
    docs = [CompareDoc(f"D{n}", ref.filename, ref.page_basis, facts.get(ref.id, ()), tuple(pages[ref.id]))
            for n, ref in enumerate(refs, start=1)]

    prompt, pages_read = prompts.compare(docs, focus, budget_chars=ctx.settings.insights_max_chars)
    draft = ctx.ai.complete(prompt, ComparisonDraft, owner=ComparisonOwner(comparison_id))
    rows, dropped = ground(draft, docs)

    snapshot = [{"id": str(ref.id), "filename": ref.filename, "page_basis": ref.page_basis, "pages_read": read,
                 "page_count": doc.pages[-1].number} for ref, doc, read in zip(refs, docs, pages_read, strict=True)]
    with ctx.db.connection() as conn:
        conn.execute(
            "INSERT INTO comparisons (id, focus, documents, rows, dropped_cells, model, prompt_version)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (comparison_id, focus, Jsonb(snapshot), Jsonb([_row_json(row) for row in rows]), dropped,
             ctx.ai.chat.models[prompt.tier], prompt.version))
        return _row_to_comparison(conn.execute(_COMPARISON_SQL + "WHERE c.id = %s GROUP BY c.id",
                                               (comparison_id,)).fetchone())


def ground(draft: ComparisonDraft, documents: Sequence[CompareDoc]) -> tuple[tuple[Row, ...], int]:
    """Keep each cell only when pages.find_on_page finds its quote on the page it cites. Pure.

    Cells are placed by document label, one column per document. The first cell
    draft naming a document claims its column; a later one naming it again, and
    one naming no document, is ignored. A blank or null value is an empty cell.
    A value with no page or quote, or whose quote is not on that page of that
    document, becomes an empty cell. A kept cell quotes the source span, not
    the model's copy. Rows left with every cell empty are removed, then at most
    MAX_ROWS are kept. Returns the rows and the number of cells that carried a
    value and were not kept.
    """
    column = {doc.label: at for at, doc in enumerate(documents)}
    rows: list[Row] = []
    dropped = 0
    for row in draft.rows:
        cells: list[Cell | None] = [None] * len(documents)
        claimed: set[int] = set()
        for cell in row.cells:
            at = column.get(cell.document)
            if at is not None and at not in claimed:
                claimed.add(at)
                cells[at] = _grounded(cell, documents[at].pages)
                if cells[at] is not None:
                    continue
            if cell.value and cell.value.strip():
                dropped += 1
        if any(cell is not None for cell in cells):
            rows.append(Row(collapse_whitespace(row.field), tuple(cells)))
    return tuple(rows[:MAX_ROWS]), dropped


def _grounded(cell: CellDraft, pages: Sequence[Page]) -> Cell | None:
    if not (cell.value and cell.value.strip()) or cell.page is None or cell.quote is None:
        return None
    found = find_on_page(cell.quote, pages, cell.page)
    return None if found is None else Cell(collapse_whitespace(cell.value), cell.page, found)


# ------------------------------------------------------------------ rows

# Usage is read back from the ledger, as chat._TURN_SQL does, so it counts every attempt the gateway made.
# deleted comes from the documents table at read time; everything else about a column is the snapshot.
_COMPARISON_SQL = """
SELECT c.id, c.focus, c.rows, c.dropped_cells, c.model, c.prompt_version, c.created_at,
       (SELECT jsonb_agg(doc || jsonb_build_object('deleted', d.id IS NULL) ORDER BY at)
        FROM jsonb_array_elements(c.documents) WITH ORDINALITY AS snapshot (doc, at)
        LEFT JOIN documents d ON d.id = (doc ->> 'id')::uuid) AS documents,
       count(l.id) AS calls, coalesce(sum(l.input_tokens), 0) AS input_tokens,
       coalesce(sum(l.output_tokens), 0) AS output_tokens,
       CASE WHEN count(l.cost_usd) = count(l.id) THEN coalesce(sum(l.cost_usd), 0) END AS cost_usd
FROM comparisons c LEFT JOIN llm_calls l ON l.comparison_id = c.id
"""


def _row_json(row: Row) -> dict[str, object]:
    return {"field": row.field, "cells": [None if cell is None else dataclasses.asdict(cell) for cell in row.cells]}


def _row_to_comparison(row: Mapping[str, Any]) -> Comparison:
    """The database-to-domain boundary for comparisons. Rows are positional: cells[i] belongs to documents[i]."""
    return Comparison(
        id=ComparisonId(row["id"]),
        documents=tuple(ComparedDocument(DocumentId(UUID(doc["id"])), doc["filename"], PageBasis(doc["page_basis"]),
                                         doc["pages_read"], doc["page_count"], doc["deleted"])
                        for doc in row["documents"]),
        focus=row["focus"],
        rows=tuple(Row(stored["field"], tuple(None if cell is None else Cell(**cell) for cell in stored["cells"]))
                   for stored in row["rows"]),
        dropped_cells=row["dropped_cells"], model=row["model"], prompt_version=row["prompt_version"],
        usage=Usage(row["calls"], row["input_tokens"], row["output_tokens"], row["cost_usd"]),
        created_at=row["created_at"])
