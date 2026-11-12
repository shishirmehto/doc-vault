"use client";

import { api } from "@/api/client";
import type { Doc, DocumentId, Result } from "@/api/domain";
import { unlisted } from "@/lib/compare";
import { failedRead, type LookedUp } from "@/lib/docList";
import { useFetched } from "./useFetched";

export function useListedDoc(id: DocumentId | null, docs: readonly Doc[] | null): LookedUp & {
  lookUp: DocumentId | null;
  reload(): void;
} {
  const listed = id && docs ? (docs.find((d) => d.id === id) ?? null) : null;
  const lookUp = id !== null && docs !== null && listed === null ? id : null;
  const older = useFetched(`listed:${lookUp}`, (): Promise<Result<Doc | null>> => (lookUp ? api.document(lookUp) : Promise.resolve({ ok: true, value: null })));
  return {
    doc: listed ?? (older.result?.ok ? older.result.value : null),
    loading: docs === null || (lookUp !== null && older.result === null),
    failed: listed ? null : failedRead(older.result),
    lookUp,
    reload: older.reload,
  };
}

/** The documents among `ids` that `docs` lacks, each read by id. A read that fails leaves its document out. */
export function useUnlistedDocs(ids: readonly DocumentId[], docs: readonly Doc[] | null): readonly Doc[] {
  const lookUp = docs === null ? [] : unlisted(ids, docs);
  const read = useFetched(`unlisted:${lookUp.join(",")}`, async (): Promise<Result<Doc[]>> => {
    const results = await Promise.all(lookUp.map((id) => api.document(id)));
    return { ok: true, value: results.flatMap((r) => (r.ok ? [r.value] : [])) };
  });
  return read.result?.ok ? read.result.value : NONE;
}

const NONE: readonly Doc[] = [];
