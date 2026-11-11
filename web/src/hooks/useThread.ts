"use client";

import { useEffect, useSyncExternalStore } from "react";
import { api } from "@/api/client";
import type { Chat, ChatId, DocumentId, NonEmpty, Result } from "@/api/domain";
import { canAsk, opening, started, step, type Thread, type ThreadEvent } from "@/lib/thread";

const MAX_QUESTION = 2000;
/** Text that stops arriving for this long means the answer is written and its citations are being checked. */
const QUIET_MS = 300;

/**
 * Asks live here, outside React, keyed by chat id: the first question is sent from the screen that
 * starts the chat and answered on the chat's own screen, and no remount can send a question twice.
 */
class Threads {
  private threads = new Map<ChatId, Thread>();
  private listeners = new Set<() => void>();
  private quietTimers = new Map<ChatId, ReturnType<typeof setTimeout>>();

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
  }

  /** Resolves when the chat exists, so the caller can show it; the question goes on in the background. */
  async start(ids: NonEmpty<DocumentId>, question: string | null): Promise<Result<Chat>> {
    const result = await api.startChat(ids);
    if (!result.ok) return result;
    this.set(result.value.id, started);
    if (question !== null) void this.ask(result.value.id, question);
    return result;
  }

  async ask(id: ChatId, raw: string): Promise<void> {
    const question = raw.trim().slice(0, MAX_QUESTION);
    const thread = this.threads.get(id);
    if (question === "" || !thread || !canAsk(thread)) return;
    this.dispatch(id, { type: "sent", question });
    const result = await api.askStream(id, question, (progress) => {
      this.dispatch(id, { type: "progress", progress });
      if (progress.kind === "text") this.quietAfter(id, QUIET_MS);
    });
    clearTimeout(this.quietTimers.get(id));
    if (result.ok) {
      this.dispatch(id, { type: "answered", turn: result.value });
      return;
    }
    this.dispatch(id, { type: "ask-failed", error: result.error });
    await this.loadHistory(id);
  }

  async recheck(id: ChatId): Promise<void> {
    this.dispatch(id, { type: "recheck" });
    await this.loadHistory(id);
  }

  dismiss(id: ChatId): void {
    this.dispatch(id, { type: "dismissed" });
  }

  private quietAfter(id: ChatId, ms: number): void {
    clearTimeout(this.quietTimers.get(id));
    this.quietTimers.set(id, setTimeout(() => this.dispatch(id, { type: "quiet" }), ms));
  }

  private async loadHistory(id: ChatId): Promise<void> {
    this.dispatch(id, { type: "history", result: await api.history(id) });
  }

  private dispatch(id: ChatId, event: ThreadEvent): void {
    const thread = this.threads.get(id);
    if (thread) this.set(id, step(thread, event));
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
