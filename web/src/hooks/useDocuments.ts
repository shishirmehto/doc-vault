"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { ApiError } from "@/api/domain";
import { applyDocEvent, LIST_LIMIT, type DocList } from "@/lib/docList";
import { isSettled } from "@/lib/lifecycle";
import { ListSync, type Update } from "@/lib/listSync";
import { liveFeed } from "./useLiveFeed";

const POLL_MS = 1000;
const RETRY_MS = 5000;

export type Documents = {
  data: DocList | null;
  error: ApiError | null;
  refresh(): void;
  patch(update: Update): void;
};

export function useDocuments(): Documents {
  const [sync] = useState(() => new ListSync());
  const [state, setState] = useState<{ data: DocList | null; error: ApiError | null }>({ data: null, error: null });
  const actions = useRef<{ refresh(): void; patch(update: Update): void }>({ refresh() {}, patch() {} });

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let failed = false;

    function schedule(): void {
      if (timer !== null) clearTimeout(timer);
      const list = sync.shown;
      const delay = failed ? RETRY_MS : !liveFeed.open && list !== null && !isSettled(list.docs) ? POLL_MS : null;
      timer = delay === null ? null : setTimeout(read, delay);
    }

    async function read(): Promise<void> {
      if (timer !== null) clearTimeout(timer);
      timer = null;
      const fetch = sync.startFetch();
      const result = await api.documents();
      if (!alive || !(result.ok ? sync.resolve(fetch, result.value) : sync.fail(fetch))) return;
      failed = !result.ok;
      setState({ data: sync.shown, error: result.ok ? null : result.error });
      schedule();
    }

    function apply(update: Update): void {
      sync.apply(update);
      const data = sync.shown;
      setState((s) => ({ ...s, data }));
      // A delete from a list capped at LIST_LIMIT leaves room for an older document no event will ever name.
      if (data !== null && !data.complete && data.docs.length < LIST_LIMIT) void read();
    }

    const leave = liveFeed.subscribe((event) => {
      switch (event.kind) {
        case "open":
        case "resync":
          void read();
          break;
        case "down":
          if (timer === null) schedule();
          break;
        case "document":
        case "deleted":
          apply((list) => applyDocEvent(list, event));
          break;
        default: {
          const unknown: never = event;
          return unknown;
        }
      }
    });
    actions.current = {
      refresh: () => void read(),
      patch: (update) => {
        apply(update);
        if (!liveFeed.open) void read();
      },
    };
    void read();
    return () => {
      alive = false;
      leave();
      if (timer !== null) clearTimeout(timer);
    };
  }, [sync]);

  const refresh = useCallback(() => actions.current.refresh(), []);
  const patch = useCallback((update: Update) => actions.current.patch(update), []);
  return { data: state.data, error: state.error, refresh, patch };
}
