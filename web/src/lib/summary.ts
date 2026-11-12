import type {
  ApiError, Result, Summary, SummaryCite, SummaryId, SummaryLength, SummaryList, SummaryOptions, SummaryTone,
} from "../api/domain.ts";

/** POST /documents/{id}/summaries takes a focus of at most 200 characters after trimming. */
export const FOCUS_MAX = 200;
/** The list reads this many, the API's maximum. A summary cited from past them cannot be shown. */
export const LISTED = 100;

/** In display order, with the backend's word targets. */
export const LENGTHS: Record<SummaryLength, { label: string; words: number }> = {
  brief: { label: "Brief", words: 60 },
  standard: { label: "Standard", words: 150 },
  detailed: { label: "Detailed", words: 300 },
};

export const TONES: Record<SummaryTone, string> = { plain: "Plain", formal: "Formal", client: "For the client" };

/** The backend's rule: trimmed, lowercase, single spaces, and blank is none. */
export function normaliseFocus(raw: string): string | null {
  return raw.trim().replace(/\s+/g, " ").toLowerCase() || null;
}

export type Run =
  | { kind: "idle" }
  | { kind: "running"; options: SummaryOptions; startedAt: number }
  | { kind: "failed"; options: SummaryOptions; error: ApiError };

export type RunEvent =
  | { type: "start"; options: SummaryOptions; at: number }
  | { type: "succeeded" }
  | { type: "failed"; error: ApiError }
  | { type: "reset" };

export const idle: Run = { kind: "idle" };

export function step(run: Run, e: RunEvent): Run {
  switch (e.type) {
    case "start":
      return run.kind === "running" ? run : { kind: "running", options: e.options, startedAt: e.at };
    case "succeeded":
      return run.kind === "running" ? idle : run;
    case "failed":
      return run.kind === "running" ? { kind: "failed", options: run.options, error: e.error } : run;
    case "reset":
      return run.kind === "failed" ? idle : run;
    default: {
      const unknown: never = e;
      return unknown;
    }
  }
}

export function mergeSummary(list: readonly Summary[], summary: Summary): Summary[] {
  return [summary, ...list.filter((s) => s.id !== summary.id)];
}

export function shownSummary(list: readonly Summary[], chosen: SummaryId | null, cite: SummaryCite | null): Summary | null {
  const id = cite?.summary ?? chosen;
  return list.find((s) => s.id === id) ?? null;
}

export function paneList(list: Result<SummaryList> | null, made: readonly Summary[], cite: SummaryCite | null): Result<SummaryList> | null {
  if (list?.ok !== true && cite !== null && made.some((s) => s.id === cite.summary)) {
    return { ok: true, value: { summaries: made, complete: false } };
  }
  return list;
}
