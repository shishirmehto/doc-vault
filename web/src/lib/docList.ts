import type { ApiError, Doc, DocEvent, Result } from "../api/domain.ts";

export type DocList = { docs: Doc[]; complete: boolean };

/** A document the list lacks, read by id. `doc` and `failed` both null, once loaded, means it is not in the vault. */
export type LookedUp = { doc: Doc | null; loading: boolean; failed: ApiError | null };

/** A read that failed for any reason but a 404: the document may well be there, so the reader gets Try again. */
export function failedRead(read: Result<unknown> | null): ApiError | null {
  if (read === null || read.ok) return null;
  return read.error.kind === "refused" && read.error.status === 404 ? null : read.error;
}

export const LIST_LIMIT = 100;

function compareNewestFirst(a: Doc, b: Doc): number {
  const byTime = b.createdAt.getTime() - a.createdAt.getTime();
  if (byTime !== 0) return byTime;
  return a.id === b.id ? 0 : a.id > b.id ? -1 : 1;
}

/**
 * Puts a document the server just returned into the list, where a fresh read of the list would put it. A version that
 * is not its family's latest leaves the list. One that is takes its family's row, even from a higher version: the
 * server read it after that version was deleted, or before that version was uploaded, and then that version's own event
 * follows, because one consumer reads and sends events in order. So the list never holds two rows for one family.
 */
export function upsertDoc(list: DocList, doc: Doc): DocList {
  const family = doc.version.family;
  if (doc.version.number < doc.version.latest) {
    return list.docs.some((d) => d.id === doc.id) ? { ...list, docs: list.docs.filter((d) => d.id !== doc.id) } : list;
  }
  if (list.docs.some((d) => d.version.family === family && d.id !== doc.id)) {
    list = { ...list, docs: list.docs.filter((d) => d.version.family !== family || d.id === doc.id) };
  }
  const known = list.docs.findIndex((d) => d.id === doc.id);
  if (known !== -1) return { ...list, docs: list.docs.map((d, i) => (i === known ? doc : d)) };
  const last = list.docs.at(-1);
  if (!list.complete && last !== undefined && compareNewestFirst(doc, last) > 0) return list;
  const at = list.docs.findIndex((d) => compareNewestFirst(doc, d) < 0);
  const docs = at === -1 ? [...list.docs, doc] : [...list.docs.slice(0, at), doc, ...list.docs.slice(at)];
  return docs.length > LIST_LIMIT ? { docs: docs.slice(0, LIST_LIMIT), complete: false } : { ...list, docs };
}

export function applyDocEvent(list: DocList, event: Exclude<DocEvent, { kind: "resync" }>): DocList {
  switch (event.kind) {
    case "document":
      return upsertDoc(list, event.doc);
    case "deleted":
      return list.docs.some((d) => d.id === event.id) ? { ...list, docs: list.docs.filter((d) => d.id !== event.id) } : list;
    default: {
      const unknown: never = event;
      return unknown;
    }
  }
}
