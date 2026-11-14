// One chat's turns and its one question in flight, as a pure state machine. hooks/useThread.ts
// runs the requests and feeds their results in as events.

import type { ApiError, AskProgress, AskStage, Brand, Result, Turn } from "../api/domain.ts";

export type History =
  | { kind: "loading" }
  | { kind: "loaded"; turns: readonly Turn[] }
  | { kind: "failed"; error: ApiError };

/**
 * A failed ask may still have been answered and recorded (the response was lost, or the proxy gave up
 * first), so history is fetched again before anything is offered. Retry is offered only once history
 * shows the question was not recorded and the turn cannot still land, so a retry never records it twice.
 *
 * `mayLand`: the server accepted the question (a 200 began its stream) and ended without a verdict (a turn or an
 * error event). It goes on answering and stores the turn whenever it finishes, so no read of history can show the
 * question will never be recorded, and no retry is offered.
 */
export type Ask =
  | { phase: "idle" }
  | { phase: "asking"; question: string }
  | { phase: "streaming"; question: string; stage: AskStage | null; text: string; quiet: boolean }
  | { phase: "checking"; question: string; error: ApiError; mayLand: boolean }
  /** History shows no turn for the question, and the server gave its verdict: asking again is safe. */
  | { phase: "failed"; question: string; error: ApiError }
  /** History could not be read either, so it is unknown whether the question was recorded. */
  | { phase: "unconfirmed"; question: string; error: ApiError; mayLand: boolean }
  /** History shows no turn yet, but the server may still store one: only Check again, never a second ask. */
  | { phase: "waiting"; question: string; error: ApiError };

/** One question as its asking tab names it. */
export type AskId = Brand<string, "AskId">;

/** The phases in which a question may still be recorded, so no tab may ask. */
export type Unsettled = Exclude<Ask, { phase: "idle" } | { phase: "failed" }>;

/**
 * Another tab's question in flight, mirrored read-only. Kept apart from `ask`: this tab's own history reads,
 * timers and failures never move it. Only that tab's snapshots set or clear it, or `orphaned` when that tab is gone.
 */
export type Elsewhere = { from: AskId; ask: Unsettled };

export type Thread = { history: History; ask: Ask; elsewhere: Elsewhere | null };

export type ThreadEvent =
  | { type: "history"; result: Result<readonly Turn[]> }
  /**
   * History read while a stream that broke mid-answer may still be landing its turn on the server. It
   * adopts the turn when it is there, and otherwise keeps checking: only a "history" event offers a retry.
   */
  | { type: "history-early"; result: Result<readonly Turn[]> }
  | { type: "sent"; question: string }
  | { type: "progress"; progress: AskProgress }
  | { type: "quiet" }
  | { type: "answered"; turn: Turn }
  /** `accepted`: a 200 began the answer's stream, so the server is answering the question. */
  | { type: "ask-failed"; error: ApiError; accepted: boolean }
  | { type: "recheck" }
  | { type: "dismissed" }
  /** Another tab's snapshot of its own question, with the turns that question has added so far. */
  | { type: "relayed"; from: AskId; ask: Ask; turns: readonly Turn[] }
  /** The tab that asked `from` is gone without a verdict. */
  | { type: "orphaned"; from: AskId };

const IDLE: Ask = { phase: "idle" };

export const opening: Thread = { history: { kind: "loading" }, ask: IDLE, elsewhere: null };
export const started: Thread = { history: { kind: "loaded", turns: [] }, ask: IDLE, elsewhere: null };

/**
 * Order matters to the backend, which reads a follow-up against the turns before it, so a question in flight in
 * another tab holds this tab's next one too.
 */
export function canAsk(t: Thread): boolean {
  return t.history.kind === "loaded" && settled(t.ask) && t.elsewhere === null;
}

export function offered(t: Thread): readonly string[] {
  return t.history.kind === "loaded" && t.ask.phase === "idle" && t.elsewhere === null ? (t.history.turns.at(-1)?.suggestions ?? []) : [];
}

/**
 * The chat's questions for Up and Down in the question box, oldest first: the recorded turns', then the one in
 * flight or failed here, then the mirrored one. Each text once, at its latest place.
 */
export function asked(t: Thread): readonly string[] {
  const newestFirst = [
    ...turnsOf(t).map((turn) => turn.question),
    ...(t.ask.phase === "idle" ? [] : [t.ask.question]),
    ...(t.elsewhere ? [t.elsewhere.ask.question] : []),
  ].reverse();
  return [...new Set(newestFirst)].reverse();
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
      return { ...t, history: withTurns({ kind: "loaded", turns: turnsOf(t) }, [e.turn]), ask: IDLE };
    case "ask-failed":
      return t.ask.phase === "asking" || t.ask.phase === "streaming"
        ? { ...t, ask: { phase: "checking", question: t.ask.question, error: e.error, mayLand: e.accepted && e.error.kind !== "refused" } }
        : t;
    case "recheck":
      if (t.ask.phase === "unconfirmed") return { ...t, ask: { ...t.ask, phase: "checking" } };
      return t.ask.phase === "waiting" ? { ...t, ask: { ...t.ask, phase: "checking", mayLand: true } } : t;
    case "dismissed":
      return t.ask.phase === "failed" ? { ...t, ask: IDLE } : t;
    case "relayed": {
      const history = t.history.kind === "loaded" ? withTurns(t.history, e.turns) : t.history;
      // This tab's failed question was never recorded, and the chat has moved on in another tab.
      if (!settled(e.ask)) return { history, ask: t.ask.phase === "failed" ? IDLE : t.ask, elsewhere: { from: e.from, ask: e.ask } };
      if (t.elsewhere?.from === e.from) return { ...t, history, elsewhere: null };
      return history === t.history ? t : { ...t, history };
    }
    case "orphaned":
      if (t.elsewhere?.from !== e.from) return t;
      return { ...t, ask: settled(t.ask) ? takeOver(t.elsewhere.ask) : t.ask, elsewhere: null };
    default: {
      const unknown: never = e;
      return unknown;
    }
  }
}

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
  if (ask.phase !== "checking" && ask.phase !== "waiting") {
    if (result.ok) return { ...t, history: { kind: "loaded", turns: mergeTurns(result.value, turnsOf(t)) } };
    return t.history.kind === "loaded" ? t : { ...t, history: { kind: "failed", error: result.error } };
  }
  const mayLand = ask.phase === "waiting" || ask.mayLand;
  if (!result.ok) return final ? { ...t, ask: { phase: "unconfirmed", question: ask.question, error: ask.error, mayLand } } : t;
  const known = new Set(turnsOf(t).map((turn) => turn.id));
  const recorded = result.value.some((turn) => !known.has(turn.id) && turn.question === ask.question);
  const history: History = { kind: "loaded", turns: mergeTurns(result.value, turnsOf(t)) };
  if (recorded) return { ...t, history, ask: IDLE };
  if (!final) return t;
  const { question, error } = ask;
  return { ...t, history, ask: mayLand ? { phase: "waiting", question, error } : { phase: "failed", question, error } };
}

/**
 * A history read joined with what the tab shows. A shown turn that comes after the last turn the read also has is
 * newer than the read: it stays, after the read's turns. Shown turns before that point that the read lacks fell
 * out of the API's 200-turn window: they go. Returns `read` itself when nothing is kept.
 */
function mergeTurns(read: readonly Turn[], shown: readonly Turn[]): readonly Turn[] {
  const inRead = new Set(read.map((turn) => turn.id));
  const lastShared = shown.reduce((last, turn, i) => (inRead.has(turn.id) ? i : last), -1);
  const newer = shown.slice(lastShared + 1);
  return newer.length === 0 ? read : [...read, ...newer];
}

/** `history` with the turns of `more` it lacks, by id. `history` itself when it lacks none. */
function withTurns(history: History & { kind: "loaded" }, more: readonly Turn[]): History {
  if (more.length === 0) return history;
  const known = new Set(history.turns.map((turn) => turn.id));
  const added = more.filter((turn) => !known.has(turn.id));
  return added.length === 0 ? history : { kind: "loaded", turns: [...history.turns, ...added] };
}

function settled(ask: Ask): ask is Exclude<Ask, Unsettled> {
  return ask.phase === "idle" || ask.phase === "failed";
}

/**
 * The asking tab is gone without a verdict, and the server finishes an answer whatever happens to its client. So
 * this tab checks history for the question as if it had sent it. A question that tab had only sent may have
 * reached the server, so it may land too.
 */
function takeOver(ask: Unsettled): Ask {
  switch (ask.phase) {
    case "asking":
    case "streaming":
      return { phase: "checking", question: ask.question, error: { kind: "offline" }, mayLand: true };
    case "checking":
    case "unconfirmed":
      return { phase: "checking", question: ask.question, error: ask.error, mayLand: ask.mayLand };
    case "waiting":
      return { phase: "checking", question: ask.question, error: ask.error, mayLand: true };
    default: {
      const unknown: never = ask;
      return unknown;
    }
  }
}

/** The question may still be recorded, so history is read again until it shows the turn. */
export function landing(t: Thread): boolean {
  return t.ask.phase === "waiting" || (t.ask.phase === "checking" && t.ask.mayLand);
}

export function turnsOf(t: Thread): readonly Turn[] {
  return t.history.kind === "loaded" ? t.history.turns : [];
}
