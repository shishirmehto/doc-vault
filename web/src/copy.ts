// Every sentence the interface writes about state. The backend's own messages are shown verbatim.

import type { ApiError, Doc, PageUnit } from "./api/domain.ts";

export type Tone = "quiet" | "working" | "done" | "failed";

export function statusLine(doc: Doc): { text: string; tone: Tone } {
  const l = doc.lifecycle;
  switch (l.status) {
    case "queued":
      return { text: "Waiting to start", tone: "quiet" };
    case "parsing":
      return { text: "Reading pages", tone: "working" };
    case "embedding":
      return { text: l.total > 0 ? `Indexing ${l.embedded} of ${l.total} passages` : "Indexing passages", tone: "working" };
    case "ready":
      return doc.insights === "pending"
        ? { text: "Ready. Writing the summary", tone: "done" }
        : { text: "Ready", tone: "done" };
    case "failed":
      return { text: "Failed", tone: "failed" };
    default: {
      const unknown: never = l;
      return unknown;
    }
  }
}

const KIND: Record<Doc["kind"], string> = { pdf: "PDF", docx: "Word", txt: "Text" };

export function unitName(unit: PageUnit, count: number): string {
  return `${unit}${count === 1 ? "" : "s"}`;
}

/** "PDF, 7 pages, 1.8 MB". Commas, never middle dots. */
export function docMeta(doc: Doc): string {
  const parts = [KIND[doc.kind]];
  if (doc.layout) parts.push(`${doc.layout.pageCount} ${unitName(doc.layout.unit, doc.layout.pageCount)}`);
  parts.push(formatBytes(doc.sizeBytes));
  return parts.join(", ");
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatMs(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** One sentence for any failed request. A refusal is the backend's own message. */
export function explain(error: ApiError): string {
  switch (error.kind) {
    case "refused":
      return error.message;
    case "broken":
      return error.status >= 500
        ? `The vault service couldn't handle this request (HTTP ${error.status}).`
        : `The vault service rejected this request (HTTP ${error.status}).`;
    case "unexpected":
      return "The vault answered in a shape this page doesn't know. Regenerate the API types with npm run gen:api.";
    case "offline":
      return "Can't reach the vault. Check that the web server and the API are running.";
    default: {
      const unknown: never = error;
      return unknown;
    }
  }
}
