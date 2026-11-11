"use client";

import { useSyncExternalStore } from "react";
import { api } from "@/api/client";
import type { Health, Result } from "@/api/domain";

const HEALTH_MS = 30_000;

/** One /health poll for the whole page, running while anything reads it. */
let latest: Result<Health> | null = null;
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function check(): void {
  void api.health().then((result) => {
    latest = result;
    listeners.forEach((l) => l());
  });
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    check();
    timer = setInterval(check, HEALTH_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

/** Null until the first answer. */
export function useHealth(): Result<Health> | null {
  return useSyncExternalStore(subscribe, () => latest, () => null);
}
