"use client";

import { useSyncExternalStore } from "react";
import { api } from "@/api/client";
import type { Comparison, Result } from "@/api/domain";
import { idle, step, type CompareRequest, type Run, type RunEvent } from "@/lib/compare";

/**
 * The run lives here, outside React: it is started from the new-chat screen and shown on the compare screen, and
 * no remount can send it twice.
 */
class CompareRuns {
  private run: Run = idle;
  private listeners = new Set<() => void>();

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  get = (): Run => this.run;

  /**
   * Null when a run is already in flight. `onCompared` hears a success before the run is forgotten, so the screen
   * that shows the run can move to the comparison first.
   */
  async start(request: CompareRequest, onCompared: (c: Comparison) => void): Promise<Result<Comparison> | null> {
    if (!this.dispatch({ type: "started", request, at: Date.now() })) return null;
    const result = await api.compare(request.documents.map((d) => d.id), request.focus);
    if (result.ok) onCompared(result.value);
    this.dispatch(result.ok ? { type: "compared" } : { type: "failed", error: result.error });
    return result;
  }

  dismiss(): void {
    this.dispatch({ type: "dismissed" });
  }

  /** False when the event changed nothing. */
  private dispatch(event: RunEvent): boolean {
    const next = step(this.run, event);
    if (next === this.run) return false;
    this.run = next;
    this.listeners.forEach((l) => l());
    return true;
  }
}

export const compareRuns = new CompareRuns();

export function useCompareRun(): Run {
  return useSyncExternalStore(compareRuns.subscribe, compareRuns.get, () => idle);
}
