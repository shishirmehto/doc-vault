// One chat's turns and its one question in flight, as a pure state machine. hooks/useThread.ts
// runs the requests and feeds their results in as events.

import type { ApiError, Result, Turn } from "../api/domain.ts";

export type History =
  | { kind: "loading" }
  | { kind: "loaded"; turns: readonly Turn[] }
  | { kind: "failed"; error: ApiError };

/**
 * A failed ask may still have been answered and recorded (the response was lost, or the proxy gave up
 * first), so history is fetched again before anything is offered. Retry is offered only once history
 * shows the question was not recorded, so a retry never records it twice.
 */
export type Ask =
  | { phase: "idle" }
  | { phase: "asking"; question: string }
  | { phase: "checking"; question: string; error: ApiError }
  /** History shows no turn for the question: asking again is safe. */
  | { phase: "failed"; question: string; error: ApiError }
  /** History could not be read either, so it is unknown whether the question was recorded. */
  | { phase: "unconfirmed"; question: string; error: ApiError };

export type Thread = { history: History; ask: Ask };

export type ThreadEvent =
  | { type: "history"; result: Result<readonly Turn[]> }
  | { type: "sent"; question: string }
  | { type: "answered"; turn: Turn }
  | { type: "ask-failed"; error: ApiError }
  | { type: "recheck" }
  | { type: "dismissed" };

const IDLE: Ask = { phase: "idle" };

export const opening: Thread = { history: { kind: "loading" }, ask: IDLE };
export const started: Thread = { history: { kind: "loaded", turns: [] }, ask: IDLE };

/** Order matters to the backend, which reads a follow-up against the turns before it. */
export function canAsk(t: Thread): boolean {
  return t.history.kind === "loaded" && (t.ask.phase === "idle" || t.ask.phase === "failed");
}

export function step(t: Thread, e: ThreadEvent): Thread {
  switch (e.type) {
    case "history":
      return afterHistory(t, e.result);
    case "sent":
      return canAsk(t) ? { ...t, ask: { phase: "asking", question: e.question } } : t;
    case "answered":
      return { history: { kind: "loaded", turns: [...turnsOf(t), e.turn] }, ask: IDLE };
    case "ask-failed":
      return t.ask.phase === "asking" ? { ...t, ask: { phase: "checking", question: t.ask.question, error: e.error } } : t;
    case "recheck":
      return t.ask.phase === "unconfirmed" ? { ...t, ask: { ...t.ask, phase: "checking" } } : t;
    case "dismissed":
      return t.ask.phase === "failed" ? { ...t, ask: IDLE } : t;
    default: {
      const unknown: never = e;
      return unknown;
    }
  }
}

function afterHistory(t: Thread, result: Result<readonly Turn[]>): Thread {
  const ask = t.ask;
  if (ask.phase !== "checking") {
    if (result.ok) return { ...t, history: { kind: "loaded", turns: result.value } };
    return t.history.kind === "loaded" ? t : { ...t, history: { kind: "failed", error: result.error } };
  }
  if (!result.ok) return { ...t, ask: { ...ask, phase: "unconfirmed" } };
  const known = new Set(turnsOf(t).map((turn) => turn.id));
  const recorded = result.value.some((turn) => !known.has(turn.id) && turn.question === ask.question);
  return { history: { kind: "loaded", turns: result.value }, ask: recorded ? IDLE : { ...ask, phase: "failed" } };
}

function turnsOf(t: Thread): readonly Turn[] {
  return t.history.kind === "loaded" ? t.history.turns : [];
}
