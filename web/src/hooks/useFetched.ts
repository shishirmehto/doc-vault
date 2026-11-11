"use client";

import { useCallback, useEffect, useState } from "react";
import type { Result } from "@/api/domain";

/** Fetch once per key. `result` is null while the current key loads; `reload` fetches it again. */
export function useFetched<T>(key: string, fetcher: () => Promise<Result<T>>): { result: Result<T> | null; reload(): void } {
  const [attempt, setAttempt] = useState(0);
  const [held, setHeld] = useState<{ key: string; attempt: number; result: Result<T> } | null>(null);

  useEffect(() => {
    let current = true;
    void fetcher().then((result) => {
      if (current) setHeld({ key, attempt, result });
    });
    return () => {
      current = false;
    };
    // The key names what is fetched; a new fetcher closure for the same key is the same request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  return { result: held && held.key === key && held.attempt === attempt ? held.result : null, reload };
}
