// Every sentence the interface writes about state. The backend's own messages are shown verbatim, except the spending
// cap's, which is written again with its reset in the reader's time.

import type {
  AiBudget, ApiError, ComparedDoc, CompareRow, Counts, Doc, Health, NotFoundReason, PageUnit, Quota, Summary, SummaryLength,
  SummaryOptions, SummaryTone, Turn,
} from "./api/domain.ts";
import { COMPARE_MAX, COMPARE_MIN, FOCUS_MAX, columnVersion } from "./lib/compare.ts";
import { LENGTHS, LISTED } from "./lib/summary.ts";
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

/**
 * One sentence for any failed request. A refusal is the backend's own message, except a used-up spending cap. A server
 * failure ends with its request id, so a reader can quote it and an operator can find its log lines.
 */
export function explain(error: ApiError): string {
  const sentence = explained(error);
  const id = reference(error);
  return id === null ? sentence : `${sentence} Reference ${id}.`;
}

/**
 * The request id of a server failure: a response that is not the vault's envelope, or a refusal of 500 or more. An
 * error event inside a streamed ask carries the stream's 200, and each one is a failure while answering, except a
 * used-up cap, which is the 429's refusal arriving late. Null for anything else, and when the id is not known.
 */
function reference(error: ApiError): string | null {
  switch (error.kind) {
    case "broken":
      return error.requestId;
    case "refused":
      return error.status >= 500 || (error.status === 200 && error.code !== "quota_exceeded") ? error.requestId : null;
    default:
      return null;
  }
}

/**
 * Under a list that reads itself again every few seconds. No request id: each read sends a new one, so the note would
 * change, and be announced again, on every retry.
 */
export function retrying(error: ApiError): string {
  return `${explained(error)} Retrying.`;
}

function explained(error: ApiError): string {
  switch (error.kind) {
    case "refused":
      return error.quota ? capReached(error.quota) : error.message;
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

/** "2 model calls, 1,960 tokens in, 11 out, $0, 13 ms", or the saved note for an answer served from the cache. */
export function usageLine(turn: Turn): string {
  if (turn.cached) return SAVED_EARLIER;
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

/** After a few seconds of a comparison or a rewritten summary. */
export const LONG_DOCUMENT = "A long document can take up to a minute and a half.";

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
export function comparing(names: string): string {
  return `Comparing ${names}`;
}

export function comparedNames(documents: readonly ComparedDoc[]): string {
  return documentNames(documents.map((d) => ({ ...d, filename: withVersion(d.filename, columnVersion(documents, d)) })));
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

export function noFieldsNext(focus: string | null, documents: readonly { deleted: boolean }[]): string {
  const why = focus === null
    ? "None of the fields a broker compares was found in more than one of these documents."
    : `No field about “${focus}” was found in more than one of these documents.`;
  const next = documents.some((d) => d.deleted)
    ? "Start a new comparison of documents that state the same fields, such as a policy and its renewal quote."
    : focus === null
      ? "Try a focus, such as deductibles, or documents that state the same fields, such as a policy and its renewal quote."
      : "Try a broader focus, or none, or documents that state the same fields.";
  return `${why} ${next}`;
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

// ------------------------------------------------------------------ rewritten summaries

export const REWRITE_LEAD =
  "Write it again at another length, about one topic, or for your client. Every quote is checked against its page.";

/** "About 150 words." */
export function wordTarget(length: SummaryLength): string {
  return `About ${LENGTHS[length].words} words.`;
}

const TONE_PHRASE: Record<SummaryTone, string> = { plain: "plain", formal: "formal", client: "for your client" };

/** "Brief, for your client, about “deductibles”" or "Standard, plain": a summary's heading and its line in a list. */
export function rewriteOptions({ length, focus, tone }: SummaryOptions): string {
  return `${LENGTHS[length].label}, ${TONE_PHRASE[tone]}${focus === null ? "" : `, about “${focus}”`}`;
}

const TONE_CLAUSE: Record<SummaryTone, string> = { plain: "", formal: " in a formal tone", client: " for your client" };

/** "Writing a brief summary for your client about “deductibles”." */
export function rewriting({ length, focus, tone }: SummaryOptions): string {
  return `Writing a ${LENGTHS[length].label.toLowerCase()} summary${TONE_CLAUSE[tone]}${focus === null ? "" : ` about “${focus}”`}.`;
}

/** Said once a new summary lands. */
export const REWRITTEN = "Rewritten.";
/** Said once a stored summary comes back, and the line on its sheet. */
export const SAVED_EARLIER = "Saved earlier, no new model call.";

/** Said under a summary's sources when it has none, or when the backend left some out; null otherwise. */
export function sourcesNote({ sources, droppedSources: dropped }: Summary): string | null {
  if (sources.length === 0) {
    return dropped === 0
      ? "The model gave no sources for this summary. Treat it as an overview."
      : "None of its quotes could be found on the page it cited, so this summary has no sources. Treat it as an overview.";
  }
  if (dropped === 0) return null;
  return dropped === 1
    ? "One more quote was left out because it was not on the page it cited."
    : `${dropped} more quotes were left out because they were not on the pages they cited.`;
}

const TIME = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
const DAY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });

/** "14:03" today, "14:03 on 11 Nov" before, in the reader's time zone. */
export function clockTime(at: Date, now: Date): string {
  return at.toDateString() === now.toDateString() ? TIME.format(at) : timeAndDay(at);
}

/** "05:30 on 13 Nov", in the reader's time zone. */
export function timeAndDay(at: Date): string {
  return `${TIME.format(at)} on ${DAY.format(at)}`;
}

/** "Written by fake-chat with prompt summary-v1 at 14:03." */
export function rewrittenBy(summary: Summary, now: Date): string {
  return `Written by ${summary.writtenBy.model} with prompt ${summary.writtenBy.promptVersion} at ${clockTime(summary.createdAt, now)}.`;
}

/** "Source 2 of 4: show the passage on p. 2". */
export function sourceLabel(index: number, count: number, pageLabel: string): string {
  return `Source ${index + 1} of ${count}: show the passage on ${pageLabel}`;
}

export const LOADING_REWRITES = "Loading earlier rewrites.";
export const NO_REWRITES =
  "No rewrites yet. The first one you make is kept here, and asking again with the same options reuses it.";
export const MORE_REWRITES = `Showing the newest ${LISTED}.`;

/** A URL cites a summary the list does not hold: past the newest the list reads, or none of this document's. */
export function rewriteNotListed(complete: boolean): string {
  return complete
    ? "This document has no rewritten summary with that link, so there is no passage to open."
    : `This rewritten summary is not among the document's newest ${LISTED}, so its passage cannot be opened here.`;
}

export const NO_SUCH_SOURCE = "This rewritten summary has no source with that number, so there is no passage to open.";

// ------------------------------------------------------------------ versions

/** "Version 2 of 3": its number, and the family's highest number now. */
export function versionLine(doc: Doc): string {
  return `Version ${doc.version.number} of ${doc.version.latest}`;
}

/** What the version line says before its links to the `count` other versions, or instead of them when none is left. */
export function otherVersions(doc: Doc, count: number): string {
  if (count === 0) return doc.version.number > 1 ? "The earlier versions were deleted." : "";
  const latest = doc.version.number === doc.version.latest;
  return `${latest ? "Earlier" : "Other"} version${count === 1 ? "" : "s"}:`;
}

/** The note an earlier version opens with. `newest` is null until the family is read. */
export function newerVersion(doc: Doc, newest: Doc | null): string {
  return newest === null
    ? `A newer version of this document, version ${doc.version.latest}, is in the vault.`
    : `A newer version of this document, version ${newest.version.number}, was uploaded ${formatWhen(newest.createdAt)}.`;
}

export function chatAsks(doc: Doc): string {
  return `This chat asks version ${doc.version.number} of ${doc.filename}.`;
}

/** When the family could not be read, in place of the links to the other versions. */
export const VERSIONS_FAILED = "The other versions could not be listed.";

/** One line of the Versions list under its number: the file it was uploaded as, when, and where it stands. */
export function versionMeta(doc: Doc): string {
  return `${doc.filename}. Uploaded ${formatWhen(doc.createdAt)}. ${statusLine(doc).text}.`;
}

/** Why "Compare with version N" cannot run now, or null when it can. Both versions must be ready. */
export function versionCompareBlocked(doc: Doc, other: Doc, running: boolean): string | null {
  if (running) return COMPARE_BUSY;
  for (const [d, name] of [[doc, "This version"], [other, `Version ${other.version.number}`]] as const) {
    if (d.lifecycle.status === "failed") return `${name} failed, so it cannot be compared. Retry it from its page.`;
    if (d.lifecycle.status !== "ready") return `${name} is still being indexed. Compare opens when it is ready.`;
  }
  return null;
}

/** "terms.txt (version 2)", or the bare filename when no version is named. */
export function withVersion(filename: string, version: number | null): string {
  return version === null ? filename : `${filename} (version ${version})`;
}

/** The delete dialog's word on the family, for a document that has other versions. */
export const OTHER_VERSIONS_STAY = "Its other versions stay in the vault, with their numbers.";

// ------------------------------------------------------------------ the spending cap and the caches

/** "$0.000001", "$5.00", "$0.25": two decimals, or as many as the amount needs, as the API writes the cap. */
export function dollars(usd: number): string {
  const digits = decimals(usd);
  let format = USD.get(digits);
  if (format === undefined) {
    format = new Intl.NumberFormat("en-US", {
      style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
    USD.set(digits, format);
  }
  return format.format(usd);
}

const USD = new Map<number, Intl.NumberFormat>();

/** The fewest decimals, at least 2, that write `usd` exactly. */
function decimals(usd: number): number {
  for (let digits = 2; digits < 12; digits++) if (Number(usd.toFixed(digits)) === usd) return digits;
  return 12;
}

/** Said wherever the cap stops a request: on Ask, Compare and Rewrite the summary. */
export function capReached(quota: Quota): string {
  return `Today's AI spending cap of ${dollars(quota.limitUsd)} is used up. Asking, comparing and rewriting start again at ${timeAndDay(quota.resetsAt)}, when it resets.`;
}

/** What Spending cap says when /health reports no cap, which it also does while Postgres is unreachable. */
export function noCap(health: Health): string {
  return health.unreachable.includes("db")
    ? "The spending cap can't be read while the database is unreachable. Refresh once it is back."
    : "No daily spending cap is set. Set DAILY_AI_BUDGET_USD for the api and worker to add one.";
}

/**
 * The Spending cap table's rows, and the line above it once the cap is used up. The cap's day is UTC's, so it resets
 * at the next 00:00 UTC after `now`, shown in the reader's time. What is left is never below $0.
 */
export function capFigures({ limitUsd, spentTodayUsd }: AiBudget, now: Date): { rows: [string, string][]; stopped: string | null } {
  const resets = timeAndDay(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1)));
  // Rounded to the decimals the two amounts have, so the subtraction's float noise never shows.
  const left = Math.max(0, Number((limitUsd - spentTodayUsd).toFixed(Math.max(decimals(limitUsd), decimals(spentTodayUsd)))));
  return {
    rows: [
      ["Cap per day (UTC)", dollars(limitUsd)],
      ["Spent today", dollars(spentTodayUsd)],
      ["Left", dollars(left)],
      ["Resets", resets],
    ],
    stopped: spentTodayUsd < limitUsd
      ? null
      : `The cap is used up. Asking, comparing, rewriting and new insights stop until ${resets}. Uploads are still indexed. Raise DAILY_AI_BUDGET_USD for the api and worker to go on sooner.`,
  };
}

export const CACHE_OFF =
  "The cache is off, so every embedding and every first answer goes to the model. Set CACHE_URL for the api and worker to turn it on.";

export const CACHE_UNREACHABLE =
  "The cache can't be reached, so every request goes to the model, and still works. Check that the cache service is running. If it restarted, its counts start again from zero.";

/**
 * The cache keeps nothing on disk and may drop any key to make room, its counts included, so they also start from
 * nothing after a restart or an eviction.
 */
export const NOTHING_CACHED =
  "Nothing has been counted yet. Upload a document or ask a chat's first question, and its lookups show here. The counts also start again from zero when the cache restarts, or drops them to make room.";

/** The lead line over the cache's table. */
export function cacheSince(countedSince: Date): string {
  return `These counts run from ${timeAndDay(countedSince)} and do not follow the window chosen above. They start again from zero when the cache restarts, or drops them to make room.`;
}

const PERCENT = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

/** "66.7%"; null with no lookups, which the table shows as a dash. */
export function hitRate({ hits, misses }: Counts): string | null {
  return hits + misses === 0 ? null : PERCENT.format(hits / (hits + misses));
}
