"use client";

import { useEffect, useReducer } from "react";
import type { ApiError } from "@/api/domain";
import { whenRetryChanges, worthRetrying } from "@/lib/compare";

/**
 * worthRetrying(error), read again at the instant a used-up spending cap resets, so Try again comes back without a
 * reload. Each wake renders again; a wake that comes early schedules the next one.
 */
export function useWorthRetrying(error: ApiError | null): boolean {
  const [wakes, wake] = useReducer((n: number) => n + 1, 0);
  useEffect(() => (error === null ? undefined : whenRetryChanges(error, wake)), [error, wakes]);
  return error !== null && worthRetrying(error);
}
