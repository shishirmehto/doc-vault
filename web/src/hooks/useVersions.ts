"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ApiError, Doc } from "@/api/domain";
import { ListSync } from "@/lib/listSync";
import { applyVersionEvent, hasVersions } from "@/lib/versions";
import { liveFeed } from "./useLiveFeed";

export type Versions =
  | { kind: "none" }
  | { kind: "loading" }
  | { kind: "failed"; error: ApiError; retry(): void }
  | { kind: "loaded"; docs: readonly Doc[] };

/**
 * The document's family, oldest first, for a document that has or had other versions. It is read when the page opens
 * and again when the family's latest number changes, on every feed open and on resync, and the events that name a
 * member update it in between. A sibling's change need not change this document's own body, so the page cannot key
 * the read on the document alone. The family read last stays on screen while a new read runs, and the events that
 * arrive during a read are replayed onto its snapshot (ListSync), so an older snapshot never undoes a newer event.
 */
export function useVersions(doc: Doc | null): Versions {
  const id = doc?.id ?? null;
  const family = doc?.version.family ?? null;
  const latest = doc?.version.latest ?? null;
  const wanted = doc !== null && hasVersions(doc);
  const [attempt, setAttempt] = useState(0);
  const [sync] = useState(() => new ListSync<readonly Doc[]>());
  const [state, setState] = useState<{ id: string; docs: readonly Doc[] | null; error: ApiError | null } | null>(null);
  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    if (!wanted || id === null || family === null) return;
    let alive = true;
    const read = () => {
      const fetch = sync.startFetch();
      void api.versions(id).then((result) => {
        if (!alive || !(result.ok ? sync.resolve(fetch, result.value) : sync.fail(fetch))) return;
        setState({ id, docs: sync.shown, error: result.ok ? null : result.error });
      });
    };
    read();
    const leave = liveFeed.subscribe((event) => {
      if (event.kind === "open" || event.kind === "resync") read();
      else if (event.kind === "document" || event.kind === "deleted") {
        sync.apply((docs) => applyVersionEvent(docs, family, event));
        setState((s) => (s && s.id === id ? { ...s, docs: sync.shown } : s));
      }
    });
    return () => {
      alive = false;
      leave();
    };
  }, [wanted, id, family, latest, attempt, sync]);

  if (!wanted) return { kind: "none" };
  const held = state?.id === id ? state : null;
  if (held?.docs) return { kind: "loaded", docs: held.docs };
  if (held?.error) return { kind: "failed", error: held.error, retry };
  return { kind: "loading" };
}
