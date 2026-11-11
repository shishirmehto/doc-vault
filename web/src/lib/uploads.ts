// The upload queue as data: which files wait, which are sending, and how each one ended.
// A file the server accepted leaves the queue; its document row takes over.

import type { ApiError, Doc } from "../api/domain.ts";

export type Upload = { key: string; filename: string } & (
  | { phase: "waiting" }
  | { phase: "sending"; sent: number; total: number }
  | { phase: "rejected"; error: ApiError }
  | { phase: "duplicate"; doc: Doc }
);

export const MAX_SENDING = 3;

/** The waiting uploads to start now, oldest first, so that at most MAX_SENDING are in flight. */
export function nextToSend(queue: readonly Upload[]): Upload[] {
  const free = MAX_SENDING - queue.filter((u) => u.phase === "sending").length;
  return free > 0 ? queue.filter((u) => u.phase === "waiting").slice(0, free) : [];
}
