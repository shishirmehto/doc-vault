"use client";

import { useState, type CSSProperties } from "react";
import { api } from "@/api/client";
import type {
  ApiError, CellCite, ComparedDoc, Comparison, ComparisonId, ComparisonPane, Doc, Result, Selection,
} from "@/api/domain";
import { useFetched } from "@/hooks/useFetched";
import { cellAt, visibleRows } from "@/lib/compare";
import {
  NO_CELL, NONE_DIFFER, cannotOpen, comparing, deletedColumns, droppedCells, explain, fieldCount, focusLine, partlyRead,
} from "@/copy";
import { InAppLink } from "./InAppLink";
import { MissingComparison } from "./Missing";
import { SourcePane, type SourceView } from "./SourcePane";

type Props = {
  id: ComparisonId;
  docs: readonly Doc[] | null;
  pane: ComparisonPane | null;
  only: boolean;
  push(s: Selection): void;
  replace(s: Selection): void;
};

/** One stored comparison: its header, the table of fields across its documents, and the cited page beside them. */
export function ComparisonScreen({ id, docs, pane, only, push, replace }: Props) {
  const { result, reload } = useFetched(`comparison:${id}`, () => api.comparison(id));
  const comparison = result?.ok ? result.value : null;
  const at = (next: ComparisonPane | null, nextOnly = only): Selection => ({ kind: "comparison", comparison: id, pane: next, only: nextOnly });

  // The live list holds the newest 100 documents. A cell from an older one reads that one document instead.
  const target = comparison && pane ? cellAt(comparison, pane.cite) : null;
  const listed = target && docs ? (docs.find((d) => d.id === target.doc.id) ?? null) : null;
  const lookUp = target && !target.doc.deleted && docs !== null && listed === null ? target.doc.id : null;
  const older = useFetched(`compared:${lookUp}`, (): Promise<Result<Doc | null>> => (lookUp ? api.document(lookUp) : Promise.resolve({ ok: true, value: null })));

  if (result && !result.ok && result.error.kind === "refused" && result.error.code === "comparison_not_found") {
    return <main className="centre"><MissingComparison /></main>;
  }

  return (
    <>
      <main className="centre">
        {result === null ? (
          <div className="comparison" aria-busy="true">
            <span className="skeleton heading" style={{ width: "60%" }} />
            {[40, 90, 84, 88].map((w) => <span key={w} className="skeleton line" style={{ width: `${w}%` }} />)}
          </div>
        ) : !result.ok ? (
          <LoadFailed error={result.error} onRetry={reload} />
        ) : (
          <ComparisonView c={result.value} docs={docs} active={pane?.cite ?? null} only={only}
            onOpen={(cite) => push(at({ cite, page: null }))}
            onOnly={(next) => replace(at(pane, next))}
            onNew={() => push({ kind: "compare", with: null })} />
        )}
      </main>
      {pane && (
        <SourcePane
          view={sourceView(comparison, pane, listed ?? (older.result?.ok ? older.result.value : null), docs === null || (lookUp !== null && older.result === null))}
          onPage={(page) => replace(at({ ...pane, page }))}
          onClose={() => push(at(null))}
        />
      )}
    </>
  );
}

/** The URL names a cell by row and column; the comparison and the document resolve it to a page and a passage. */
function sourceView(c: Comparison | null, pane: ComparisonPane, doc: Doc | null, loading: boolean): SourceView {
  if (c === null) return { kind: "resolving" };
  const target = cellAt(c, pane.cite);
  if (!target) return { kind: "unknown", message: NO_CELL };
  if (target.doc.deleted) return { kind: "unknown", message: cannotOpen(target.doc) };
  if (doc === null) return loading ? { kind: "resolving" } : { kind: "unknown", message: cannotOpen(target.doc) };
  const cited = target.cell.source.page;
  const page = pane.page ?? cited;
  return { kind: "page", doc, page, mark: page === cited ? { quote: target.cell.source.quote, label: target.field } : null, citedPage: cited };
}

function LoadFailed({ error, onRetry }: { error: ApiError; onRetry(): void }) {
  return (
    <div className="load-failed">
      <p className="notice" data-tone="failed" role="alert">{explain(error)}</p>
      <button type="button" className="button" onClick={onRetry}>Try again</button>
    </div>
  );
}

function ComparisonView({ c, docs, active, only: inUrl, onOpen, onOnly, onNew }: {
  c: Comparison;
  docs: readonly Doc[] | null;
  active: CellCite | null;
  only: boolean;
  onOpen(cite: CellCite): void;
  onOnly(only: boolean): void;
  onNew(): void;
}) {
  // The URL takes the filter in a transition, a moment after the click; the box and the rows follow the click at once.
  const [only, setOnly] = useState(inUrl);
  const [seen, setSeen] = useState(inUrl);
  if (seen !== inUrl) {
    setSeen(inUrl);
    setOnly(inUrl);
  }
  const differing = c.rows.filter((r) => r.differs).length;
  const rows = visibleRows(c.rows, only);
  const deleted = c.documents.filter((d) => d.deleted).map((d) => d.filename);
  const unit = (doc: ComparedDoc) => docs?.find((d) => d.id === doc.id)?.layout?.unit ?? "page";

  return (
    <section className="comparison" aria-labelledby="comparison-title">
      <header className="comparison-header">
        <h1 id="comparison-title">{comparing(c.documents)}</h1>
        <p className="quiet">
          {focusLine(c.focus)}. {fieldCount(c.rows)}.{" "}
          <InAppLink className="text-button" to={{ kind: "compare", with: null }} onFollow={onNew}>New comparison</InAppLink>
        </p>
        {c.droppedCells > 0 && <p className="quiet">{droppedCells(c.droppedCells)}</p>}
        {c.documents.filter((d) => d.pagesRead < d.pageCount).map((d) => <p key={d.id} className="quiet">{partlyRead(d, unit(d))}</p>)}
        {deleted.length > 0 && c.rows.length > 0 && <p className="quiet">{deletedColumns(deleted)}</p>}
        <p className="written-by">Compared by {c.model} with prompt {c.promptVersion}.</p>
      </header>
      {c.rows.length > 0 && (
        <>
          <label className="only-differences">
            <input type="checkbox" checked={only} onChange={(e) => {
              setOnly(e.currentTarget.checked);
              onOnly(e.currentTarget.checked);
            }} />
            Only differences ({differing})
          </label>
          {rows.length === 0 ? (
            <p className="quiet">{NONE_DIFFER}</p>
          ) : (
            <div className="table-box" role="region" aria-label="Comparison table" tabIndex={0} data-scroll-x>
              <table className="compare-table" style={{ "--columns": c.documents.length } as CSSProperties}>
                <thead>
                  <tr>
                    <th scope="col">Field</th>
                    {c.documents.map((d) => (
                      <th key={d.id} scope="col">{d.filename}{d.deleted && <span className="quiet"> (deleted)</span>}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map(({ row, index }) => (
                    <tr key={index}>
                      <th scope="row" data-differs={row.differs || undefined}>
                        {row.field}
                        {row.differs && <> <span className="differs">Differs</span></>}
                      </th>
                      {c.documents.map((doc, col) => {
                        const cell = row.cells[col] ?? null;
                        const open = active?.row === index && active.col === col;
                        return (
                          <td key={doc.id} data-active={open || undefined}>
                            {cell === null ? (
                              <span className="not-stated">Not stated</span>
                            ) : (
                              <div className="cell">
                                <span className="cell-value">{cell.value}</span>
                                {doc.deleted ? (
                                  <span className="page-gone">{cell.pageLabel}, deleted</span>
                                ) : (
                                  <button type="button" className="page-tab" aria-pressed={open} onClick={() => onOpen({ row: index, col })}
                                    aria-label={`${row.field} in ${doc.filename}: show the passage on ${cell.pageLabel}`}>
                                    {cell.pageLabel}
                                  </button>
                                )}
                              </div>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}
