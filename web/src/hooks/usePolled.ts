"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiError, Result } from "@/api/domain";

const RETRY_MS = 5000;

export type Polled<T> = {
  data: T | null;
  /** The last failed poll. Data from before it stays visible. */
  error: ApiError | null;
  /** Fetch now and restart the schedule. */
  refresh(): void;
  /** Apply a response already in hand (an upload, a retry), then refresh. */
  patch(update: (data: T) => T): void;
};

/**
 * Fetch, then ask the data when to fetch again: a number schedules the next poll, null stops.
 * A poll that was in flight when patch or refresh ran is dropped, so it cannot undo the patch.
 */
export function usePolled<T>(fetcher: () => Promise<Result<T>>, nextDelayMs: (data: T) => number | null): Polled<T> {
  const [state, setState] = useState<{ data: T | null; error: ApiError | null }>({ data: null, error: null });
  const generation = useRef(0);
  const fetcherRef = useRef(fetcher);
  const delayRef = useRef(nextDelayMs);
  useEffect(() => {
    fetcherRef.current = fetcher;
    delayRef.current = nextDelayMs;
  });

  const kick = useRef<() => void>(() => {});
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    async function tick() {
      if (timer) clearTimeout(timer);
      const gen = ++generation.current;
      const result = await fetcherRef.current();
      if (!alive || gen !== generation.current) return;
      const delay = result.ok ? delayRef.current(result.value) : RETRY_MS;
      setState((s) => (result.ok ? { data: result.value, error: null } : { data: s.data, error: result.error }));
      timer = delay === null ? null : setTimeout(tick, delay);
    }
    kick.current = () => void tick();
    void tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, []);

  const refresh = useCallback(() => kick.current(), []);
  const patch = useCallback((update: (data: T) => T) => {
    generation.current++;
    setState((s) => (s.data === null ? s : { data: update(s.data), error: s.error }));
    kick.current();
  }, []);

  return { data: state.data, error: state.error, refresh, patch };
}
