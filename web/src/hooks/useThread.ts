"use client";

import { useEffect, useSyncExternalStore } from "react";
import { api } from "@/api/client";
import type { Chat, ChatId, DocumentId, NonEmpty, Result } from "@/api/domain";
import { CHANNEL, asAskId, heard, lockName, told, type TabMessage } from "@/lib/tabs";
import { canAsk, landing, opening, started, step, type AskId, type Thread, type ThreadEvent } from "@/lib/thread";

const MAX_QUESTION = 2000;
const QUIET_MS = 300;
/**
 * When a stream breaks after it started, the server still finishes the answer and stores the turn. History is read
 * after each of these waits (30 s in all); then the chat says it is still checking, offers Check again, and reads
 * history every 30 s for as long as the server may still be answering.
 */
const LANDING_WAITS_MS = [0, 1000, 2000, 4000, 8000, 15000];
/**
 * 570 s with the 30 s above, past the server's 550 s at its defaults (config.py, ai.py): three model calls (condense,
 * the query embedding, the answer), each up to 3 attempts of chat_timeout_s 30 s with two retry waits of at most
 * MAX_RETRY_WAIT_S 10 s, and a repair turn as long again for condense and the answer. This only stops the polling: the
 * live provider's timeout bounds each read of a streamed answer rather than the whole answer, so no retry is
 * offered after it either.
 */
const LATE_CHECKS_MS: readonly number[] = Array(18).fill(30_000);
/**
 * The asking tab keeps its lock this long after it stops working on its question, so its last snapshot reaches
 * the other tabs before the lock's release does, and none of them takes over a question that has finished.
 */
const GRACE_MS = 250;

/**
 * This module is also evaluated on the server and in Node, where an open BroadcastChannel would hold the process
 * open. The channel, the locks and randomUUID are the browser's, each reached only when first needed.
 */
const IN_BROWSER = typeof window !== "undefined";

/**
 * Web Locks need a secure context. Over plain HTTP from another host there are none: the other tabs still mirror a
 * question, and only the check for an asking tab that is gone is off.
 */
function locks(): LockManager | null {
  return IN_BROWSER && navigator.locks ? navigator.locks : null;
}

/** The browser frees the lock when `held` ends or the tab goes. */
async function beacon(name: string, held: () => Promise<void>): Promise<void> {
  await (locks()?.request(name, held) ?? held());
}

/** Unique among the open tabs, which is all a mirror and a lock name need. randomUUID needs a secure context too. */
function newAskId(): AskId {
  return asAskId(IN_BROWSER && typeof crypto.randomUUID === "function" ? crypto.randomUUID() : String(Math.random()));
}

/**
 * Asks live here, outside React, keyed by chat id: the first question is sent from the screen that
 * starts the chat and answered on the chat's own screen, and no remount can send a question twice.
 */
class Threads {
  private threads = new Map<ChatId, Thread>();
  private listeners = new Set<() => void>();
  private quietTimers = new Map<ChatId, ReturnType<typeof setTimeout>>();
  /**
   * The questions this tab sent itself and that are not over. A tab posts only about these, so a tab that took a
   * question over never posts, and nothing echoes.
   */
  private asking = new Map<ChatId, AskId>();
  private channel: BroadcastChannel | null = null;

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  get = (id: ChatId): Thread | undefined => this.threads.get(id);

  /** Idempotent: history is loaded the first time a chat is shown, never over a thread already here. */
  open(id: ChatId): void {
    if (this.threads.has(id)) return;
    this.set(id, opening);
    void this.loadHistory(id);
    this.post({ chat: id, hello: true });
  }

  /** Resolves when the chat exists, so the caller can show it; the question goes on in the background. */
  async start(ids: NonEmpty<DocumentId>, question: string | null): Promise<Result<Chat>> {
    const result = await api.startChat(ids);
    if (!result.ok) return result;
    this.set(result.value.id, started);
    // A chat started with no question posts nothing else, and the channel opens on the first post: the hello makes
    // this tab hear the others.
    this.post({ chat: result.value.id, hello: true });
    if (question !== null) void this.ask(result.value.id, question);
    return result;
  }

  async ask(id: ChatId, raw: string): Promise<void> {
    const question = raw.trim().slice(0, MAX_QUESTION);
    const thread = this.threads.get(id);
    if (question === "" || !thread || !canAsk(thread)) return;
    this.dispatch(id, { type: "sent", question });
    const from = newAskId();
    await beacon(lockName(from), async () => {
      this.asking.set(id, from);
      this.snapshot(id, from);
      try {
        await this.answer(id, question);
      } finally {
        if (this.asking.get(id) === from) this.asking.delete(id);
        await new Promise((resolve) => setTimeout(resolve, GRACE_MS));
      }
    });
  }

  private async answer(id: ChatId, question: string): Promise<void> {
    let accepted = false;
    const result = await api.askStream(id, question, (progress) => {
      this.dispatch(id, { type: "progress", progress });
      if (progress.kind === "text") this.quietAfter(id, QUIET_MS);
    }, () => {
      accepted = true;
    });
    clearTimeout(this.quietTimers.get(id));
    if (result.ok) {
      this.dispatch(id, { type: "answered", turn: result.value });
      return;
    }
    this.dispatch(id, { type: "ask-failed", error: result.error, accepted });
    await this.confirm(id);
  }

  /** For a question that got no verdict here: history is read until it shows the turn, or once when it cannot land. */
  private async confirm(id: ChatId): Promise<void> {
    if (this.landing(id)) await this.awaitLanding(id);
    else await this.loadHistory(id);
  }

  reload(id: ChatId): void {
    const thread = this.threads.get(id);
    if (thread?.history.kind !== "failed") return;
    this.set(id, { ...opening, elsewhere: thread.elsewhere });
    void this.loadHistory(id);
  }

  async recheck(id: ChatId): Promise<void> {
    this.dispatch(id, { type: "recheck" });
    await this.loadHistory(id);
  }

  dismiss(id: ChatId): void {
    this.dispatch(id, { type: "dismissed" });
  }

  private async awaitLanding(id: ChatId): Promise<void> {
    for (const wait of LANDING_WAITS_MS) {
      await new Promise((resolve) => setTimeout(resolve, wait));
      this.dispatch(id, { type: "history-early", result: await api.history(id) });
      if (!this.landing(id)) return;
    }
    await this.loadHistory(id);
    for (const wait of LATE_CHECKS_MS) {
      if (!this.landing(id)) return;
      await new Promise((resolve) => setTimeout(resolve, wait));
      this.dispatch(id, { type: "history-early", result: await api.history(id) });
    }
  }

  private landing(id: ChatId): boolean {
    const thread = this.threads.get(id);
    return thread !== undefined && landing(thread);
  }

  private quietAfter(id: ChatId, ms: number): void {
    clearTimeout(this.quietTimers.get(id));
    this.quietTimers.set(id, setTimeout(() => this.dispatch(id, { type: "quiet" }), ms));
  }

  private async loadHistory(id: ChatId): Promise<void> {
    this.dispatch(id, { type: "history", result: await api.history(id) });
  }

  private dispatch(id: ChatId, event: ThreadEvent): void {
    const before = this.threads.get(id);
    if (!before) return;
    const after = step(before, event);
    this.set(id, after);
    const from = this.asking.get(id);
    const message = from ? told(id, from, before, after) : null;
    if (message) this.post(message);
  }

  /** This tab's question as it stands, for a tab that did not hear its steps. */
  private snapshot(id: ChatId, from: AskId): void {
    const thread = this.threads.get(id);
    if (thread) this.post({ chat: id, from, ask: thread.ask, turns: [] });
  }

  private post(message: TabMessage): void {
    this.tabs()?.postMessage(message);
  }

  private tabs(): BroadcastChannel | null {
    if (this.channel === null && IN_BROWSER && typeof BroadcastChannel !== "undefined") {
      this.channel = new BroadcastChannel(CHANNEL);
      this.channel.onmessage = (e) => this.receive(e.data);
    }
    return this.channel;
  }

  private receive(data: unknown): void {
    const message = heard(data);
    const before = message && this.threads.get(message.chat);
    if (!message || !before) return;
    if ("hello" in message) {
      const from = this.asking.get(message.chat);
      if (from) this.snapshot(message.chat, from);
      return;
    }
    const { chat, from, ask, turns } = message;
    const after = step(before, { type: "relayed", from, ask, turns });
    this.set(chat, after);
    if (turns.length > 0 && after.history.kind !== "loaded") void this.loadHistory(chat);
    if (after.elsewhere && after.elsewhere.from !== before.elsewhere?.from) void this.watch(chat, after.elsewhere.from);
  }

  /**
   * The asking tab holds the lock of its question until the question is over, so this shared request is granted
   * when that tab let go or is gone. "orphaned" changes this tab's ask only when the mirror still shows the
   * question, and then history is read for it as after a failed ask of this tab's own.
   */
  private async watch(chat: ChatId, from: AskId): Promise<void> {
    const manager = locks();
    if (!manager) return;
    await manager.request(lockName(from), { mode: "shared" }, () => {});
    const mine = this.threads.get(chat)?.ask;
    this.dispatch(chat, { type: "orphaned", from });
    if (this.threads.get(chat)?.ask !== mine) await this.confirm(chat);
  }

  private set(id: ChatId, thread: Thread): void {
    if (this.threads.get(id) === thread) return;
    this.threads.set(id, thread);
    this.listeners.forEach((l) => l());
  }
}

export const threads = new Threads();

export function useThread(id: ChatId): Thread | undefined {
  useEffect(() => threads.open(id), [id]);
  return useSyncExternalStore(threads.subscribe, () => threads.get(id), () => undefined);
}
