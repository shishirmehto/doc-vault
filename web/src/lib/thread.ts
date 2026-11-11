// One chat's turns and its one question in flight, as a pure state machine. hooks/useThread.ts
// runs the requests and feeds their results in as events.

import type { ApiError, AskProgress, AskStage, Result, Turn } from "../api/domain.ts";

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
  /**
   * The answer streams: the step the server last named (null before it names one) and the provisional text so
   * far, which the final turn replaces. `quiet` is set when text stopped arriving for a while, so the answer is
   * being checked, not written.
   */
  | { phase: "streaming"; question: string; stage: AskStage | null; text: string; quiet: boolean }
  | { phase: "checking"; question: string; error: ApiError }
  /** History shows no turn for the question: asking again is safe. */
  | { phase: "failed"; question: string; error: ApiError }
  /** History could not be read either, so it is unknown whether the question was recorded. */
  | { phase: "unconfirmed"; question: string; error: ApiError };

export type Thread = { history: History; ask: Ask };

export type ThreadEvent =
  | { type: "history"; result: Result<readonly Turn[]> }
  /**
   * History read while a stream that broke mid-answer may still be landing its turn on the server. It
   * adopts the turn when it is there, and otherwise keeps checking: only a "history" event offers a retry.
   */
  | { type: "history-early"; result: Result<readonly Turn[]> }
  | { type: "sent"; question: string }
  | { type: "progress"; progress: AskProgress }
  /** No text has arrived for a while. hooks/useThread.ts times it. */
  | { type: "quiet" }
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

/** The latest turn's follow-up questions, offered only while a click can ask at once. */
export function offered(t: Thread): readonly string[] {
  return t.history.kind === "loaded" && t.ask.phase === "idle" ? (t.history.turns.at(-1)?.suggestions ?? []) : [];
}

export function step(t: Thread, e: ThreadEvent): Thread {
  switch (e.type) {
    case "history":
      return afterHistory(t, e.result, true);
    case "history-early":
      return afterHistory(t, e.result, false);
    case "sent":
      return canAsk(t) ? { ...t, ask: { phase: "asking", question: e.question } } : t;
    case "progress":
      return { ...t, ask: afterProgress(t.ask, e.progress) };
    case "quiet":
      return t.ask.phase === "streaming" && t.ask.text !== "" ? { ...t, ask: { ...t.ask, quiet: true } } : t;
    case "answered":
      return { history: { kind: "loaded", turns: [...turnsOf(t), e.turn] }, ask: IDLE };
    case "ask-failed":
      return t.ask.phase === "asking" || t.ask.phase === "streaming"
        ? { ...t, ask: { phase: "checking", question: t.ask.question, error: e.error } }
        : t;
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

/** Progress only moves a question in flight. Text implies the answering step, whatever came before it. */
function afterProgress(ask: Ask, p: AskProgress): Ask {
  if (ask.phase !== "asking" && ask.phase !== "streaming") return ask;
  const now: Ask & { phase: "streaming" } = ask.phase === "streaming" ? ask : { phase: "streaming", question: ask.question, stage: null, text: "", quiet: false };
  switch (p.kind) {
    case "stage":
      return { ...now, stage: p.stage, quiet: false };
    case "text":
      return { ...now, stage: "answering", text: now.text + p.text, quiet: false };
    case "reset":
      return { ...now, text: "", quiet: false };
    default: {
      const unknown: never = p;
      return unknown;
    }
  }
}

function afterHistory(t: Thread, result: Result<readonly Turn[]>, final: boolean): Thread {
  const ask = t.ask;
  if (ask.phase !== "checking") {
    if (result.ok) return { ...t, history: { kind: "loaded", turns: result.value } };
    return t.history.kind === "loaded" ? t : { ...t, history: { kind: "failed", error: result.error } };
  }
  if (!result.ok) return final ? { ...t, ask: { ...ask, phase: "unconfirmed" } } : t;
  const known = new Set(turnsOf(t).map((turn) => turn.id));
  const recorded = result.value.some((turn) => !known.has(turn.id) && turn.question === ask.question);
  if (recorded) return { history: { kind: "loaded", turns: result.value }, ask: IDLE };
  return final ? { history: { kind: "loaded", turns: result.value }, ask: { ...ask, phase: "failed" } } : t;
}

function turnsOf(t: Thread): readonly Turn[] {
  return t.history.kind === "loaded" ? t.history.turns : [];
}
