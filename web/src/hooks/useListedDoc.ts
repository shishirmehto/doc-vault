"use client";

import { api } from "@/api/client";
import type { Doc, DocumentId, Result } from "@/api/domain";
import { useFetched } from "./useFetched";

export function useListedDoc(id: DocumentId | null, docs: readonly Doc[] | null): {
  doc: Doc | null;
  loading: boolean;
  lookUp: DocumentId | null;
  reload(): void;
} {
  const listed = id && docs ? (docs.find((d) => d.id === id) ?? null) : null;
  const lookUp = id !== null && docs !== null && listed === null ? id : null;
  const older = useFetched(`listed:${lookUp}`, (): Promise<Result<Doc | null>> => (lookUp ? api.document(lookUp) : Promise.resolve({ ok: true, value: null })));
  return {
    doc: listed ?? (older.result?.ok ? older.result.value : null),
    loading: docs === null || (lookUp !== null && older.result === null),
    lookUp,
    reload: older.reload,
  };
}
