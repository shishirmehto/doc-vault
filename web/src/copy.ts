// Every sentence the interface writes about state. The backend's own messages are shown verbatim.

import type { ApiError, ComparedDoc, CompareRow, Doc, NotFoundReason, PageUnit, Turn } from "./api/domain.ts";
import { COMPARE_MAX, COMPARE_MIN, FOCUS_MAX } from "./lib/compare.ts";
import type { Ask } from "./lib/thread.ts";

type InFlight = Extract<Ask, { phase: "asking" | "streaming" }>;

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

/**
 * "Policy. PDF, 7 pages, 11 KB": the type from the insights leads once it is known, unless it is "other", which
 * says nothing. Commas, never middle dots.
 */
export function docMeta(doc: Doc): string {
  const parts = [KIND[doc.kind]];
  if (doc.layout) parts.push(`${doc.layout.pageCount} ${unitName(doc.layout.unit, doc.layout.pageCount)}`);
  parts.push(formatBytes(doc.sizeBytes));
  const type = doc.documentType && doc.documentType !== "other" ? `${doc.documentType.charAt(0).toUpperCase()}${doc.documentType.slice(1)}. ` : "";
  return type + parts.join(", ");
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

const WHEN = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** "11 Nov, 16:23", in the reader's time zone. */
export function formatWhen(at: Date): string {
  return WHEN.format(at);
}

export function formatMs(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** "$0", "$0.0042", "$1.25". */
export function formatUsd(usd: number): string {
  return usd === 0 ? "$0" : `$${usd < 0.01 ? usd.toFixed(4) : usd.toFixed(2)}`;
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
  const cost = costUsd === null ? "cost unknown" : formatUsd(costUsd);
  const callWord = calls === 1 ? "call" : "calls";
  return `${calls} model ${callWord}, ${n(inputTokens)} tokens in, ${n(outputTokens)} out, ${cost}, ${formatMs(turn.latencyMs)}`;
}

/** "Searching 3 documents". The count is unknown until the chat's header loads. */
export function searching(documents: number | null): string {
  return documents === null ? "Searching" : `Searching ${documents === 1 ? "1 document" : `${documents} documents`}`;
}

/**
 * The status line under a question in flight. It follows the server's steps in the order it names them, and
 * until it names the first one the question is only being sent. `quiet`: text stopped arriving, so the
 * citations are being checked.
 */
export function askStatus(ask: InFlight, documents: number | null): string {
  if (ask.phase === "asking" || ask.stage === null) return "Sending the question";
  switch (ask.stage) {
    case "condensing":
      return "Reading the follow-up against the conversation";
    case "searching":
      return searching(documents);
    case "answering":
      return ask.quiet ? "Checking citations" : "Writing the answer";
    default: {
      const unknown: never = ask.stage;
      return unknown;
    }
  }
}

/** "a.pdf", "a.pdf and b.pdf", "a.pdf, b.pdf and c.pdf". */
export function listNames(names: readonly string[]): string {
  return names.length <= 1 ? (names[0] ?? "") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/**
 * A refusal's documents that are not ready, each with its status now: "a.pdf (indexing 3 of 9 passages)". A document
 * gone from the list keeps the filename the refusal gave.
 */
export function notReadyNames(error: ApiError, docs: readonly Doc[]): string[] {
  const notReady = error.kind === "refused" && error.code === "documents_not_ready" ? error.documents : [];
  return notReady.map(({ id, filename }) => {
    const doc = docs.find((d) => d.id === id);
    return doc ? `${doc.filename} (${statusLine(doc).text.toLowerCase()})` : (filename ?? "a document no longer in the vault");
  });
}

// ------------------------------------------------------------------ comparison

/** Why Compare is disabled, or null when it is not. `focus` is the text the comparison would focus on. */
export function compareBlocked(ticked: number, focus: string): string | null {
  if (ticked < COMPARE_MIN) {
    return ticked === 1 ? "Tick one more ready document to compare." : "Tick at least two ready documents to compare.";
  }
  if (ticked > COMPARE_MAX) {
    return `A comparison takes at most ${COMPARE_MAX} documents, and ${ticked} are ticked. Untick ${ticked - COMPARE_MAX} to compare.`;
  }
  const length = focus.trim().length;
  if (length > FOCUS_MAX) {
    return `Compare uses the question as its focus, which takes at most ${FOCUS_MAX} characters. This one has ${length}.`;
  }
  return null;
}

/** One comparison runs at a time. */
export const COMPARE_BUSY = "A comparison is already running. Compare opens up again when it finishes.";
/** Under the new-chat footer while Compare is offered, because Compare reads the question box as its focus. */
export const COMPARE_FOCUS_HINT = "Compare uses the question as its focus. Leave it empty to compare every field a broker checks.";

export const COMPARE_FULL = `A comparison takes at most ${COMPARE_MAX} documents. Untick one to choose another.`;

export const COMPARE_LONG = "A long document can take up to a minute and a half.";

export const NO_COMPARISONS = "No comparisons yet. Tick two documents above to make the first.";

export const NOTHING_TO_COMPARE = "No documents yet. Upload two or more, and compare them once they are ready.";

export const NONE_DIFFER = "None of the fields differ.";

export const NO_CELL = "This comparison has no value in that cell, so there is no page to open.";

/** A failed run: what did not happen, the backend's reason, and the documents that are not ready, if any. */
export function compareFailed(error: ApiError, notReady: readonly string[]): string[] {
  const lines = [`The comparison was not made. ${explain(error)}`];
  if (notReady.length > 0) {
    const one = notReady.length === 1;
    lines.push(`Not ready: ${listNames(notReady)}. Wait until ${one ? "it is" : "they are"} ready, or change the documents.`);
  }
  return lines;
}

export function cannotOpen(doc: { filename: string; deleted: boolean }): string {
  return doc.deleted ? `${doc.filename} was deleted, so its pages can no longer be opened.` : `${doc.filename} is no longer in the vault.`;
}

/** "a.pdf, b.pdf (deleted) and c.pdf": a chat's or a comparison's documents. */
export function documentNames(documents: readonly { filename: string; deleted?: boolean }[]): string {
  return listNames(documents.map((d) => (d.deleted ? `${d.filename} (deleted)` : d.filename)));
}

/** "Comparing a.pdf, b.pdf (deleted) and c.pdf". */
export function comparing(documents: readonly { filename: string; deleted?: boolean }[]): string {
  return `Comparing ${documentNames(documents)}`;
}

export function focusLine(focus: string | null): string {
  return focus === null ? "No focus: the fields a broker compares" : `Focus: ${focus}`;
}

/** "12 fields, 5 differ". */
export function fieldCount(rows: readonly CompareRow[]): string {
  if (rows.length === 0) return "No fields were found to compare";
  const differ = rows.filter((r) => r.differs).length;
  const fields = `${rows.length} ${rows.length === 1 ? "field" : "fields"}`;
  return `${fields}, ${differ === 0 ? "none differ" : `${differ} ${differ === 1 ? "differs" : "differ"}`}`;
}

/** "5 of 12 fields differ", for a list of comparisons. */
export function differCount(rows: readonly CompareRow[]): string {
  if (rows.length === 0) return "No fields found";
  const differ = rows.filter((r) => r.differs).length;
  return `${differ} of ${rows.length} ${rows.length === 1 ? "field" : "fields"} ${differ === 1 ? "differs" : "differ"}`;
}

export function droppedCells(n: number): string {
  return n === 1
    ? "1 value was left out because it could not be checked against the page it cites."
    : `${n} values were left out because they could not be checked against the pages they cite.`;
}

/** Shown only for a document the comparison read part of. */
export function partlyRead(doc: ComparedDoc, unit: PageUnit): string {
  const units = unitName(unit, doc.pageCount);
  return `Of the ${doc.pageCount} ${units} in ${doc.filename}, only the first ${doc.pagesRead} ${doc.pagesRead === 1 ? "was" : "were"} read, so fields on later ${unitName(unit, 2)} are not compared.`;
}

export function deletedColumns(filenames: readonly string[]): string {
  return filenames.length === 1
    ? `${filenames[0]} was deleted. Its values stay, but its pages can no longer be opened.`
    : `${listNames(filenames)} were deleted. Their values stay, but their pages can no longer be opened.`;
}
