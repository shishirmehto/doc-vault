"use client";

import { useCallback, useMemo } from "react";
import { useSearchParams } from "next/navigation";
import type { Selection } from "@/api/domain";
import { parseSelection, selectionQuery } from "@/api/parse";

/**
 * The selection lives in the URL. Writes use the History API, which the App Router syncs into
 * useSearchParams without a server round trip. push adds a history entry, so Back undoes it.
 */
export function useSelection(): { selection: Selection; push(s: Selection): void; replace(s: Selection): void } {
  const params = useSearchParams();
  const selection = useMemo(() => parseSelection(new URLSearchParams(params.toString())), [params]);
  const push = useCallback((s: Selection) => window.history.pushState(null, "", selectionQuery(s) || window.location.pathname), []);
  const replace = useCallback((s: Selection) => window.history.replaceState(null, "", selectionQuery(s) || window.location.pathname), []);
  return { selection, push, replace };
}
