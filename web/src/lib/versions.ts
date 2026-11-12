import type { Doc, DocEvent, FamilyId } from "../api/domain.ts";

export function laterVersion(doc: Doc): number | null {
  return doc.version.number > 1 ? doc.version.number : null;
}

export function hasVersions(doc: Doc): boolean {
  return doc.version.number > 1 || doc.version.latest > 1;
}

export type Family = { others: readonly Doc[]; newest: Doc | null; compareWith: Doc | null };

export function familyOf(doc: Doc, versions: readonly Doc[]): Family {
  const others = versions
    .filter((d) => d.id !== doc.id && d.version.family === doc.version.family)
    .sort((a, b) => a.version.number - b.version.number);
  const top = others.at(-1);
  const newest = top !== undefined && top.version.number > doc.version.number ? top : null;
  const earlier = others.filter((d) => d.version.number < doc.version.number);
  return { others, newest, compareWith: newest ?? earlier.at(-1) ?? null };
}

export type ChatVersion = { number: number; newer: { newest: Doc | null } | null };

export function chatVersion(doc: Doc | null, listed: readonly Doc[] | null): ChatVersion | null {
  if (doc === null || !hasVersions(doc)) return null;
  const newer = doc.version.number < doc.version.latest ? { newest: familyOf(doc, listed ?? []).newest } : null;
  return { number: doc.version.number, newer };
}

export function applyVersionEvent(versions: readonly Doc[], family: FamilyId, event: Exclude<DocEvent, { kind: "resync" }>): readonly Doc[] {
  switch (event.kind) {
    case "document":
      if (event.doc.version.family !== family) return versions;
      return [...versions.filter((d) => d.id !== event.doc.id), event.doc].sort((a, b) => a.version.number - b.version.number);
    case "deleted":
      return versions.some((d) => d.id === event.id) ? versions.filter((d) => d.id !== event.id) : versions;
    default: {
      const unknown: never = event;
      return unknown;
    }
  }
}
