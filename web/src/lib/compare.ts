import type { ApiError, CellCite, ComparedDoc, CompareCell, CompareRow, Comparison, Doc, DocumentId } from "../api/domain.ts";

/** POST /comparisons takes 2 to 5 documents and a focus of at most 200 characters. */
export const COMPARE_MIN = 2;
export const COMPARE_MAX = 5;
export const FOCUS_MAX = 200;

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

export function compareRequest(docs: readonly Doc[], focus: string): CompareRequest {
  return { documents: docs.map((d) => ({ id: d.id, filename: d.filename })), focus: focus || null };
}

/** Documents the live list lacks, such as a linked one older than its newest 100 or an earlier version, after it. */
export function withDocs(docs: readonly Doc[], extra: readonly Doc[]): readonly Doc[] {
  const added = extra.filter((doc, i) => !docs.some((d) => d.id === doc.id) && extra.findIndex((e) => e.id === doc.id) === i);
  return added.length === 0 ? docs : [...docs, ...added];
}

/** The ids the live list lacks, to read one by one. */
export function unlisted(ids: readonly DocumentId[], docs: readonly Doc[]): DocumentId[] {
  return ids.filter((id) => !docs.some((d) => d.id === id));
}

export function columns(chosen: ReadonlySet<DocumentId>, docs: readonly Doc[]): Doc[] {
  const ready = new Map(docs.filter((d) => d.lifecycle.status === "ready").map((d) => [d.id, d]));
  return [...chosen].flatMap((id) => ready.get(id) ?? []);
}

/**
 * A used-up spending cap fails again until it resets, and a rate limit until its Retry-After has passed.
 */
export function worthRetrying(error: ApiError, now: Date = new Date()): boolean {
  const until = failsUntil(error);
  if (until !== null) return now >= until;
  return !(error.kind === "refused" && error.retryable === false && error.code !== "documents_not_ready");
}

/** setTimeout fires at once for a longer wait. A cap resets within a day, so only a malformed reset gets near it. */
const LONGEST_WAIT_MS = 2 ** 31 - 1;

/**
 * Calls `onChange` when worthRetrying(error) may have turned true: at a used-up cap's reset or a rate limit's
 * Retry-After, the only answers that change with time. Returns the cancel. The caller reads worthRetrying again, and
 * calls this again if it is still no.
 */
export function whenRetryChanges(error: ApiError, onChange: () => void): () => void {
  const until = failsUntil(error);
  if (until === null || worthRetrying(error)) return () => {};
  const timer = setTimeout(onChange, Math.min(LONGEST_WAIT_MS, until.getTime() - Date.now()));
  return () => clearTimeout(timer);
}

/** The instant before which `error` would only come back: a used-up cap's reset, or a rate limit's Retry-After. */
function failsUntil(error: ApiError): Date | null {
  if (error.kind !== "refused") return null;
  if (error.code === "quota_exceeded") return error.quota?.resetsAt ?? null;
  return error.code === "rate_limited" ? error.retryAt : null;
}

export function visibleRows(rows: readonly CompareRow[], only: boolean): { row: CompareRow; index: number }[] {
  return rows.flatMap((row, index) => (only && !row.differs ? [] : [{ row, index }]));
}

export function cellAt(c: Comparison, cite: CellCite): { field: string; doc: ComparedDoc; cell: CompareCell } | null {
  const row = c.rows[cite.row];
  const doc = c.documents[cite.col];
  const cell = row?.cells[cite.col];
  return row && doc && cell ? { field: row.field, doc, cell } : null;
}

export function columnVersion(documents: readonly ComparedDoc[], doc: ComparedDoc): number | null {
  if (doc.version === null) return null;
  const twin = documents.some((d) => d.id !== doc.id && d.filename === doc.filename);
  return doc.version > 1 || twin ? doc.version : null;
}
