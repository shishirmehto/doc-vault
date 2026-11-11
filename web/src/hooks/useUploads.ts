"use client";

import { useState, useSyncExternalStore } from "react";
import { api } from "@/api/client";
import type { Doc } from "@/api/domain";
import { nextToSend, type Upload } from "@/lib/uploads";

/** The queue outlives renders, so it is a small store rather than component state. */
class UploadQueue {
  private items: readonly Upload[] = [];
  private files = new Map<string, File>();
  private listeners = new Set<() => void>();
  private seq = 0;
  private readonly onDocument: (doc: Doc) => void;

  constructor(onDocument: (doc: Doc) => void) {
    this.onDocument = onDocument;
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  snapshot = () => this.items;

  add(files: Iterable<File>): void {
    for (const file of files) {
      const key = `u${++this.seq}`;
      this.files.set(key, file);
      this.items = [...this.items, { key, filename: file.name, phase: "waiting" }];
    }
    this.pump();
  }

  dismiss(key: string): void {
    this.set(key, null);
  }

  private set(key: string, next: Upload | null): void {
    this.items = next === null ? this.items.filter((u) => u.key !== key) : this.items.map((u) => (u.key === key ? next : u));
    if (next === null || next.phase === "rejected" || next.phase === "duplicate") this.files.delete(key);
    this.listeners.forEach((l) => l());
  }

  private pump(): void {
    for (const upload of nextToSend(this.items)) {
      const file = this.files.get(upload.key);
      if (!file) continue;
      const { key, filename } = upload;
      this.set(key, { key, filename, phase: "sending", sent: 0, total: file.size });
      void api
        .upload(file, (sent, total) => this.set(key, { key, filename, phase: "sending", sent, total }))
        .then((result) => {
          if (!result.ok) this.set(key, { key, filename, phase: "rejected", error: result.error });
          else if (result.value.duplicate) this.set(key, { key, filename, phase: "duplicate", doc: result.value.doc });
          else this.set(key, null);
          if (result.ok) this.onDocument(result.value.doc);
          this.pump();
        });
    }
    this.listeners.forEach((l) => l());
  }
}

export function useUploads(onDocument: (doc: Doc) => void): {
  uploads: readonly Upload[];
  add(files: Iterable<File>): void;
  dismiss(key: string): void;
} {
  const [queue] = useState(() => new UploadQueue(onDocument));
  const uploads = useSyncExternalStore(queue.subscribe, queue.snapshot, queue.snapshot);
  return { uploads, add: (files) => queue.add(files), dismiss: (key) => queue.dismiss(key) };
}
