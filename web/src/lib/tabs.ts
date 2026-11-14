// What the tabs of one browser say to each other about a chat, as pure functions. hooks/useThread.ts owns the
// channel and the locks, and posts and hears these.

import type { ChatId, Turn } from "../api/domain.ts";
import { asChatId, isRecord } from "../api/parse.ts";
import { turnsOf, type Ask, type AskId, type Thread } from "./thread.ts";

/** The channel's name carries the protocol version: a tab of an older build is on another channel. */
export const CHANNEL = "docvault.chat.v1";

/** The asking tab holds this lock for as long as its question is in flight, so the browser frees it when the tab goes. */
export const lockName = (from: AskId) => `docvault.ask.${from}`;

export type TabMessage =
  /** A tab has just opened the chat. The tab that holds a question in it answers with its snapshot. */
  | { chat: ChatId; hello: true }
  /** The asking tab's own Ask as it stands, and the turns its question added in this step. */
  | { chat: ChatId; from: AskId; ask: Ask; turns: readonly Turn[] };

/** The only constructor of an AskId. */
export function asAskId(raw: string): AskId {
  return raw as AskId;
}

/** What a step of this tab's own question tells the other tabs: null when its `ask` did not change. */
export function told(chat: ChatId, from: AskId, before: Thread, after: Thread): TabMessage | null {
  if (after.ask === before.ask) return null;
  if (after.history === before.history) return { chat, from, ask: after.ask, turns: [] };
  const shown = new Set(turnsOf(before).map((turn) => turn.id));
  return { chat, from, ask: after.ask, turns: turnsOf(after).filter((turn) => !shown.has(turn.id)) };
}

/**
 * Boundary parse of a channel message. Only the envelope is checked (a chat id; `hello`, or `from` + `ask.phase` +
 * a `turns` array): both ends are the same build on the same origin. Anything else: null.
 */
export function heard(data: unknown): TabMessage | null {
  if (!isRecord(data) || typeof data.chat !== "string") return null;
  const chat = asChatId(data.chat);
  if (chat === null) return null;
  if ("hello" in data) return data.hello === true ? { chat, hello: true } : null;
  const { from, ask, turns } = data;
  return typeof from === "string" && isAsk(ask) && Array.isArray(turns) ? { chat, from: asAskId(from), ask, turns } : null;
}

const PHASES: Record<Ask["phase"], true> = {
  idle: true, asking: true, streaming: true, checking: true, failed: true, unconfirmed: true, waiting: true,
};

function isAsk(ask: unknown): ask is Ask {
  return isRecord(ask) && typeof ask.phase === "string" && Object.hasOwn(PHASES, ask.phase);
}
