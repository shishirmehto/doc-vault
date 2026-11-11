// Every sentence the interface writes about state. The backend's own messages are shown verbatim.

import type { ApiError, Doc, NotFoundReason, PageUnit, Turn } from "./api/domain.ts";

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

/** What happened and what to try, under the backend's own sentence. A deliberate result, never an error. */
export const NOT_FOUND: Record<NotFoundReason, string> = {
  no_evidence:
    "Nothing in these documents matched the question. Try the words the document itself would use, or start a chat with more documents.",
  model_declined:
    "Passages close to the question were found, but none of them answers it. Try asking about one specific item, such as a limit, a date or a party.",
  unverified:
    "An answer was drafted, but its quotes could not be found in the documents, so it was held back. Try a narrower question.",
};

/** "2 model calls, 1,960 tokens in, 11 out, $0, 13 ms". */
export function usageLine(turn: Turn): string {
  const { calls, inputTokens, outputTokens, costUsd } = turn.usage;
  const n = (x: number) => x.toLocaleString("en-US");
  const cost =
    costUsd === null ? "cost unknown" : costUsd === 0 ? "$0" : `$${costUsd < 0.01 ? costUsd.toFixed(4) : costUsd.toFixed(2)}`;
  const callWord = calls === 1 ? "call" : "calls";
  return `${calls} model ${callWord}, ${n(inputTokens)} tokens in, ${n(outputTokens)} out, ${cost}, ${formatMs(turn.latencyMs)}`;
}

/** "a.pdf", "a.pdf and b.pdf", "a.pdf, b.pdf and c.pdf". */
export function listNames(names: readonly string[]): string {
  return names.length <= 1 ? (names[0] ?? "") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}
