"use client";

import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ChatDocument, Doc, DocumentId, Result } from "@/api/domain";
import { liveFeed } from "@/hooks/useLiveFeed";
import { useFetched } from "@/hooks/useFetched";

/**
 * Each of a chat's documents, and the document as the vault holds it now: from the live list, or read on its own when
 * the list lacks it, as it lacks an earlier version. `now` is null for a deleted document, and until its read lands. A read
 * follows the events that name it, and keeps showing what it last read while it reads again.
 */
export function useChatDocs(
  documents: readonly ChatDocument[] | null,
  docs: readonly Doc[] | null,
): readonly { asked: ChatDocument; now: Doc | null }[] {
  const unlisted = documents && docs ? documents.filter((d) => !d.deleted && !docs.some((x) => x.id === d.id)).map((d) => d.id) : [];
  const key = unlisted.join(",");
  const read = useFetched(`chat-docs:${key}`, async (): Promise<Result<readonly Doc[]>> => {
    const results = await Promise.all(unlisted.map((id) => api.document(id)));
    return { ok: true, value: results.flatMap((r) => (r.ok ? [r.value] : [])) };
  });
  const reload = read.reload;
  useEffect(() => {
    if (key === "") return;
    const ids = new Set(key.split(","));
    return liveFeed.subscribe((event) => {
      if (event.kind === "down") return;
      if (event.kind === "document" && !ids.has(event.doc.id)) return;
      if (event.kind === "deleted" && !ids.has(event.id)) return;
      reload();
    });
  }, [key, reload]);

  const [held, setHeld] = useState<ReadonlyMap<DocumentId, Doc>>(new Map());
  const [seen, setSeen] = useState(read.result);
  if (read.result !== null && read.result !== seen) {
    setSeen(read.result);
    if (read.result.ok) setHeld(new Map(read.result.value.map((d) => [d.id, d])));
  }
  return (documents ?? []).map((d) => ({ asked: d, now: d.deleted ? null : (docs?.find((x) => x.id === d.id) ?? held.get(d.id) ?? null) }));
}
