// Finding documents in a long list, and choosing many at once for a chat.

/** The find field shows only when the list is longer than this. */
export const FIND_AFTER = 6;

/** A chat answers from at most this many documents (the API's limit). */
export const CHAT_MAX_DOCUMENTS = 50;

/** Case-insensitive: every word of the query appears in the filename or the document type. */
export function findDocs<T extends { filename: string; documentType: string | null }>(docs: readonly T[], query: string): readonly T[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return docs;
  return docs.filter((d) => {
    const text = `${d.filename} ${d.documentType ?? ""}`.toLowerCase();
    return words.every((w) => text.includes(w));
  });
}

/**
 * "Select all ready": when every one of `ids` is chosen, unchoose them; otherwise choose them too, up to `max`
 * in all. `capped` says some were left out.
 */
export function chooseAll<Id>(chosen: ReadonlySet<Id>, ids: readonly Id[], max: number): { chosen: ReadonlySet<Id>; capped: boolean } {
  if (ids.length > 0 && ids.every((id) => chosen.has(id))) {
    return { chosen: new Set([...chosen].filter((id) => !ids.includes(id))), capped: false };
  }
  const next = new Set(chosen);
  for (const id of ids) {
    if (next.size >= max) return { chosen: next, capped: true };
    next.add(id);
  }
  return { chosen: next, capped: false };
}
