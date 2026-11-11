// A comparison run as a pure state machine, and the rules the comparison screens read. hooks/useCompareRun.ts runs
// the request and feeds its result in as events.

import type { ApiError, CellCite, ComparedDoc, CompareCell, CompareRow, Comparison, Doc, DocumentId } from "../api/domain.ts";

/** POST /comparisons takes 2 to 5 documents and a focus of at most 200 characters. */
export const COMPARE_MIN = 2;
export const COMPARE_MAX = 5;
export const FOCUS_MAX = 200;

/** The columns in order, named so the run can say what it compares, and the focus (null for none). */
export type CompareRequest = { documents: readonly { id: DocumentId; filename: string }[]; focus: string | null };

/**
 * One run at a time. A success leaves nothing to keep: the comparison is stored and the screen opens it. A failure
 * keeps its request, so Try again sends it again and "Change the documents" puts it back in the picker.
 */
export type Run =
  | { phase: "idle" }
  | { phase: "running"; request: CompareRequest; startedAt: number }
  | { phase: "failed"; request: CompareRequest; error: ApiError };

export type RunEvent =
  | { type: "started"; request: CompareRequest; at: number }
  | { type: "compared" }
  | { type: "failed"; error: ApiError }
  | { type: "dismissed" };

export const idle: Run = { phase: "idle" };

export function step(run: Run, e: RunEvent): Run {
  switch (e.type) {
    case "started":
      return run.phase === "running" ? run : { phase: "running", request: e.request, startedAt: e.at };
    case "compared":
      return run.phase === "running" ? idle : run;
    case "failed":
      return run.phase === "running" ? { phase: "failed", request: run.request, error: e.error } : run;
    case "dismissed":
      return run.phase === "failed" ? idle : run;
    default: {
      const unknown: never = e;
      return unknown;
    }
  }
}

/** The live list, then a document it does not hold: one older than its newest 100, read on its own. */
export function withDoc(docs: readonly Doc[], doc: Doc | null): readonly Doc[] {
  return doc === null || docs.some((d) => d.id === doc.id) ? docs : [...docs, doc];
}

/** The ticked documents that are ready, in the order they were ticked: the columns of the comparison. */
export function columns(chosen: ReadonlySet<DocumentId>, docs: readonly Doc[]): Doc[] {
  const ready = new Map(docs.filter((d) => d.lifecycle.status === "ready").map((d) => [d.id, d]));
  return [...chosen].flatMap((id) => ready.get(id) ?? []);
}

/**
 * Whether sending the same request again can succeed. A refusal the backend marks final would only fail again, except
 * documents_not_ready: a document becomes ready on its own.
 */
export function worthRetrying(error: ApiError): boolean {
  return !(error.kind === "refused" && error.retryable === false && error.code !== "documents_not_ready");
}

/** The rows "Only differences" leaves, each with its index in `rows`, which is what a cell's URL names. */
export function visibleRows(rows: readonly CompareRow[], only: boolean): { row: CompareRow; index: number }[] {
  return rows.flatMap((row, index) => (only && !row.differs ? [] : [{ row, index }]));
}

/** The cell a URL names, with its row's field and its column's document; null when it names an empty cell or none. */
export function cellAt(c: Comparison, cite: CellCite): { field: string; doc: ComparedDoc; cell: CompareCell } | null {
  const row = c.rows[cite.row];
  const doc = c.documents[cite.col];
  const cell = row?.cells[cite.col];
  return row && doc && cell ? { field: row.field, doc, cell } : null;
}

/**
 * The version a column's header marks: any after the first, and version 1 too when another column has the same
 * filename, so two versions of one file can be told apart. Null when the comparison predates versions.
 */
export function columnVersion(documents: readonly ComparedDoc[], doc: ComparedDoc): number | null {
  if (doc.version === null) return null;
  const twin = documents.some((d) => d.id !== doc.id && d.filename === doc.filename);
  return doc.version > 1 || twin ? doc.version : null;
}
