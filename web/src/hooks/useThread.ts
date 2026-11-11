"use client";

import { useEffect, useSyncExternalStore } from "react";
import { api } from "@/api/client";
import { asDocumentId } from "@/api/parse";
import type { Chat, ChatDocument, ChatId, DocumentId, NonEmpty, Result } from "@/api/domain";
import { canAsk, opening, started, step, type Thread, type ThreadEvent } from "@/lib/thread";

const MAX_QUESTION = 2000;

/**
 * Asks live here, outside React, keyed by chat id: the first question is sent from the screen that
 * starts the chat and answered on the chat's own screen, and no remount can send a question twice.
 */
class Threads {
  private threads = new Map<ChatId, Thread>();
  private scopes = new Map<ChatId, NonEmpty<ChatDocument>>();
  private listeners = new Set<() => void>();

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  get = (id: ChatId): Thread | undefined => this.threads.get(id);

  /** The chat's documents, when this browser started it. The API has no route to read them back. */
  documents(id: ChatId): NonEmpty<ChatDocument> | null {
    return this.scopes.get(id) ?? null;
  }

  /** Idempotent: history is loaded the first time a chat is shown, never over a thread already here. */
  open(id: ChatId): void {
    if (this.threads.has(id)) return;
    const remembered = readScope(id);
    if (remembered) this.scopes.set(id, remembered);
    this.set(id, opening);
    void this.loadHistory(id);
  }

  /** Resolves when the chat exists, so the caller can show it; the question goes on in the background. */
  async start(ids: NonEmpty<DocumentId>, question: string | null): Promise<Result<Chat>> {
    const result = await api.startChat(ids);
    if (!result.ok) return result;
    const chat = result.value;
    this.scopes.set(chat.id, chat.documents);
    writeScope(chat);
    this.set(chat.id, started);
    if (question !== null) void this.ask(chat.id, question);
    return result;
  }

  async ask(id: ChatId, raw: string): Promise<void> {
    const question = raw.trim().slice(0, MAX_QUESTION);
    const thread = this.threads.get(id);
    if (question === "" || !thread || !canAsk(thread)) return;
    this.dispatch(id, { type: "sent", question });
    const result = await api.ask(id, question);
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

export function useThread(id: ChatId): { thread: Thread | undefined; documents: NonEmpty<ChatDocument> | null } {
  useEffect(() => threads.open(id), [id]);
  const thread = useSyncExternalStore(threads.subscribe, () => threads.get(id), () => undefined);
  return { thread, documents: threads.documents(id) };
}

// ------------------------------------------------------------------ the chat's documents, in localStorage

const key = (id: ChatId) => `doc-vault.chat.${id}`;

function writeScope(chat: Chat): void {
  try {
    localStorage.setItem(key(chat.id), JSON.stringify(chat.documents));
  } catch {
    // Storage is a convenience: without it a reloaded chat only loses its document list.
  }
}

/** Read back as untrusted input: anything not a non-empty list of {id, filename} is ignored. */
function readScope(id: ChatId): NonEmpty<ChatDocument> | null {
  let stored: unknown;
  try {
    stored = JSON.parse(localStorage.getItem(key(id)) ?? "null");
  } catch {
    return null;
  }
  if (!Array.isArray(stored)) return null;
  const documents = stored.flatMap((d: unknown): ChatDocument[] => {
    if (typeof d !== "object" || d === null) return [];
    const { id: rawId, filename } = d as Record<string, unknown>;
    const docId = typeof rawId === "string" ? asDocumentId(rawId) : null;
    return docId && typeof filename === "string" ? [{ id: docId, filename }] : [];
  });
  const [first, ...rest] = documents;
  return first === undefined || documents.length !== stored.length ? null : [first, ...rest];
}
