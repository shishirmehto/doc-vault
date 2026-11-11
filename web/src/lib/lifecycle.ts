// Pure rules about a document's progress: when polling can stop, and how full each stage segment is.

import type { Doc, Lifecycle } from "../api/domain.ts";

/** A document is settled when polling again cannot change what the user sees. */
export function isSettled(docs: Iterable<Doc>): boolean {
  for (const d of docs) {
    const s = d.lifecycle.status;
    if (s !== "failed" && !(s === "ready" && d.insights !== "pending")) return false;
  }
  return true;
}

/** One segment of the stage bar. `fraction` is null when no honest number exists (parsing). */
export type Segment =
  | { kind: "todo" }
  | { kind: "active"; fraction: number | null }
  | { kind: "done" }
  | { kind: "failed" };

/** Three segments: parse, embed, ready. */
export type StageBar = readonly [parse: Segment, embed: Segment, ready: Segment];

const TODO: Segment = { kind: "todo" };
const DONE: Segment = { kind: "done" };
const FAILED: Segment = { kind: "failed" };

export function stageProgress(l: Lifecycle): StageBar {
  switch (l.status) {
    case "queued":
      return [TODO, TODO, TODO];
    case "parsing":
      return [{ kind: "active", fraction: null }, TODO, TODO];
    case "embedding":
      return [DONE, { kind: "active", fraction: l.total > 0 ? l.embedded / l.total : null }, TODO];
    case "ready":
      return [DONE, DONE, DONE];
    case "failed":
      return l.stage === "parse" ? [FAILED, TODO, TODO] : [DONE, FAILED, TODO];
    default: {
      const unknown: never = l;
      return unknown;
    }
  }
}
