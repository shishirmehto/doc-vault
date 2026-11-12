import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  parseComparison, parseComparisonPage, parseDocumentPage, parseError, parseHealth, parseSummary, parseTurn, parseVersionList,
} from "./api/parse.ts";
import { readAskStream } from "./api/client.ts";
import type { ApiError, DocumentId } from "./api/domain.ts";
import {
  cannotOpen, chatAsks, clockTime, compareBlocked, compareFailed, comparedNames, comparing, deletedColumns, differCount, docMeta, dollars,
  droppedCells, explain, fieldCount, focusLine, hitRate, newerVersion, noCap, noFieldsNext, notReadyNames, otherVersions, partlyRead,
  retrying, rewriteOptions, rewriting, rewrittenBy, sourceLabel, sourcesNote, usageLine, versionCompareBlocked,
  versionLine, versionMeta, withVersion, wordTarget,
} from "./copy.ts";

const page = JSON.parse(readFileSync(new URL("./api/fixtures/documents-page.json", import.meta.url), "utf8"));

test("a row's meta leads with the document type once insights know it, and skips \"other\"", () => {
  const meta = Object.fromEntries(parseDocumentPage(page.body).docs.map((d) => [d.filename, docMeta(d)]));
  assert.deepEqual(
    {
      policy: meta["sample-cgl-policy.pdf"],
      quote: meta["sample-renewal-quote.pdf"],
      endorsement: meta["sample-endorsement-additional-insured.docx"],
      other: meta["ui-drop-check.txt"],
      failed: meta["live-scan.pdf"],
    },
    {
      policy: "Policy. PDF, 7 pages, 11 KB",
      quote: "Quote. PDF, 3 pages, 5 KB",
      endorsement: "Endorsement. Word, 2 sections, 37 KB",
      other: "Text, 1 section, 147 B",
      failed: "PDF, 1 KB",
    },
  );
});

const fixture = (name: string) => JSON.parse(readFileSync(new URL(`./api/fixtures/${name}.json`, import.meta.url), "utf8")).body;

test("Compare says why it is disabled: too few or too many documents, or a focus too long", () => {
  const focus = "x".repeat(201);
  assert.deepEqual(
    [compareBlocked(0, ""), compareBlocked(1, ""), compareBlocked(2, ""), compareBlocked(5, " deductible "), compareBlocked(7, ""),
      compareBlocked(3, focus), compareBlocked(3, ` ${"x".repeat(200)} `)],
    [
      "Tick at least two ready documents to compare.",
      "Tick one more ready document to compare.",
      null,
      null,
      "A comparison takes at most 5 documents, and 7 are ticked. Untick 2 to compare.",
      "Compare uses the question as its focus, which takes at most 200 characters. This one has 201.",
      null,
    ],
  );
});

test("a comparison's header names its documents, its focus and how many fields differ", () => {
  const plain = parseComparison(fixture("compare-201-policy-quote"));
  const gone = parseComparison(fixture("comparison-get-deleted-document"));
  const focused = parseComparison(fixture("compare-201-focus-deductible"));
  assert.deepEqual(
    [comparing(comparedNames(plain.documents)), comparing(comparedNames(gone.documents)), focusLine(plain.focus), focusLine(focused.focus),
      fieldCount(plain.rows), fieldCount(focused.rows), fieldCount([]), fieldCount(gone.rows.slice(1))],
    [
      "Comparing sample-cgl-policy.pdf and sample-renewal-quote.pdf",
      "Comparing sample-cgl-policy.pdf and phase-c-shared-86f623.txt (deleted)",
      "No focus: the fields a broker compares",
      "Focus: deductible",
      "12 fields, 5 differ",
      "1 field, 1 differs",
      "No fields were found to compare",
      "1 field, none differ",
    ],
  );
});

test("two versions of one file are named apart wherever their comparison is named", () => {
  const doc = { id: "70eebfea-0db0-468f-b3e3-58c67c2f6d5f" as DocumentId, filename: "terms.txt", version: 1, deleted: false, pagesRead: 1, pageCount: 1 };
  const next = { ...doc, id: "70eebfea-0db0-468f-b3e3-58c67c2f6d60" as DocumentId, version: 2, deleted: true };
  assert.deepEqual(
    [comparedNames([doc, next]), comparedNames([doc, { ...next, filename: "quote.pdf", version: 1 }]), comparedNames([{ ...doc, version: null }, next])],
    ["terms.txt (version 1) and terms.txt (version 2) (deleted)", "terms.txt and quote.pdf (deleted)", "terms.txt and terms.txt (version 2) (deleted)"],
  );
});

test("a comparison with no fields says what to try, by its focus and whether its documents are still in the vault", () => {
  const live = [{ deleted: false }, { deleted: false }];
  assert.deepEqual(
    [noFieldsNext(null, live), noFieldsNext("earthquake sublimits", live), noFieldsNext("premium", [{ deleted: false }, { deleted: true }]),
      noFieldsNext(null, [{ deleted: true }, { deleted: true }])],
    [
      "None of the fields a broker compares was found in more than one of these documents. Try a focus, such as deductibles, or documents that state the same fields, such as a policy and its renewal quote.",
      "No field about “earthquake sublimits” was found in more than one of these documents. Try a broader focus, or none, or documents that state the same fields.",
      "No field about “premium” was found in more than one of these documents. Start a new comparison of documents that state the same fields, such as a policy and its renewal quote.",
      "None of the fields a broker compares was found in more than one of these documents. Start a new comparison of documents that state the same fields, such as a policy and its renewal quote.",
    ],
  );
});

test("recent comparisons say how many of their fields differ", () => {
  assert.deepEqual(parseComparisonPage(fixture("comparisons-page")).slice(0, 5).map((c) => differCount(c.rows)), [
    "1 of 2 fields differs",
    "12 of 13 fields differ",
    "1 of 1 field differs",
    "5 of 12 fields differ",
    "No fields found",
  ]);
});

test("the notes under a comparison: dropped values, documents read in part, deleted columns", () => {
  const memo = { id: "70eebfea-0db0-468f-b3e3-58c67c2f6d5f" as DocumentId, filename: "ui-renewal-memo-2.txt", version: 1, deleted: false, pagesRead: 40, pageCount: 254 };
  assert.deepEqual(
    [droppedCells(1), droppedCells(3), partlyRead(memo, "section"), partlyRead({ ...memo, pagesRead: 1, pageCount: 2 }, "page"),
      deletedColumns(["a.txt"]), deletedColumns(["a.txt", "b.pdf"])],
    [
      "1 value was left out because it could not be checked against the page it cites.",
      "3 values were left out because they could not be checked against the pages they cite.",
      "Of the 254 sections in ui-renewal-memo-2.txt, only the first 40 were read, so fields on later sections are not compared.",
      "Of the 2 pages in ui-renewal-memo-2.txt, only the first 1 was read, so fields on later pages are not compared.",
      "a.txt was deleted. Its values stay, but its pages can no longer be opened.",
      "a.txt and b.pdf were deleted. Their values stay, but their pages can no longer be opened.",
    ],
  );
});

test("a 409 names each document that is not ready with its status now, or the filename it gave", () => {
  const docs = parseDocumentPage(page.body).docs;
  const error: ApiError = {
    kind: "refused", status: 409, code: "documents_not_ready", message: "2 of 3 documents are not ready.", retryable: false,
    retryAt: null, quota: null, requestId: null,
    documents: [
      { id: "3e7c685f-ac8e-4c9f-8a86-f65e5f0a21a7" as DocumentId, filename: "live-scan.pdf" },
      { id: "41474e86-1b59-46ad-980b-c0355948cd08" as DocumentId, filename: "phase-c-large-86f623.txt" },
    ],
  };
  assert.deepEqual(notReadyNames(error, docs), ["live-scan.pdf (failed)", "phase-c-large-86f623.txt"]);
  assert.deepEqual(notReadyNames({ ...error, code: "rate_limited" }, docs), []);
});

test("a failed comparison says it was not made, why, and which documents are not ready", () => {
  const busy: ApiError = {
    kind: "refused", status: 409, code: "documents_not_ready", message: "1 of 2 documents are not ready.", retryable: false,
    retryAt: null, documents: [], quota: null, requestId: null,
  };
  assert.deepEqual(compareFailed(busy, ["big.txt (reading pages)"]), [
    "The comparison was not made. 1 of 2 documents are not ready.",
    "Not ready: big.txt (reading pages). Wait until it is ready, or change the documents.",
  ]);
  assert.deepEqual(compareFailed({ kind: "offline" }, []), [
    "The comparison was not made. Can't reach the vault. Check that the web server and the API are running.",
  ]);
  assert.deepEqual(
    [cannotOpen({ filename: "a.txt", deleted: true }), cannotOpen({ filename: "a.txt", deleted: false })],
    ["a.txt was deleted, so its pages can no longer be opened.", "a.txt is no longer in the vault."],
  );
});

test("a rewritten summary's options read as its heading, and as the sentence while it is written", () => {
  const options = [
    { length: "brief", focus: "deductibles", tone: "client" },
    { length: "standard", focus: null, tone: "plain" },
    { length: "detailed", focus: null, tone: "formal" },
    { length: "standard", focus: "named insured", tone: "formal" },
  ] as const;
  assert.deepEqual(options.map(rewriteOptions), [
    "Brief, for your client, about “deductibles”",
    "Standard, plain",
    "Detailed, formal",
    "Standard, formal, about “named insured”",
  ]);
  assert.deepEqual(options.map(rewriting), [
    "Writing a brief summary for your client about “deductibles”.",
    "Writing a standard summary.",
    "Writing a detailed summary in a formal tone.",
    "Writing a standard summary in a formal tone about “named insured”.",
  ]);
  assert.deepEqual((["brief", "standard", "detailed"] as const).map(wordTarget), ["About 60 words.", "About 150 words.", "About 300 words."]);
});

test("a rewritten summary's notes: no sources, quotes left out, each source's tab, and when it was written", () => {
  const body = JSON.parse(readFileSync(new URL("./api/fixtures/summary-201-brief-focus-client.json", import.meta.url), "utf8")).body;
  const summary = parseSummary(body);
  assert.deepEqual(
    [
      summary,
      { ...summary, droppedSources: 1 },
      { ...summary, droppedSources: 3 },
      { ...summary, sources: [], droppedSources: 2 },
      { ...summary, sources: [], droppedSources: 0 },
    ].map(sourcesNote),
    [
      null,
      "One more quote was left out because it was not on the page it cited.",
      "3 more quotes were left out because they were not on the pages they cited.",
      "None of its quotes could be found on the page it cited, so this summary has no sources. Treat it as an overview.",
      "The model gave no sources for this summary. Treat it as an overview.",
    ],
  );
  assert.equal(sourceLabel(1, 4, "p. 2"), "Source 2 of 4: show the passage on p. 2");

  const now = new Date(2026, 10, 12, 18, 30);
  assert.deepEqual([clockTime(new Date(2026, 10, 12, 14, 3), now), clockTime(new Date(2026, 10, 11, 9, 5), now)], ["14:03", "09:05 on 11 Nov"]);
  assert.equal(rewrittenBy({ ...summary, createdAt: new Date(2026, 10, 12, 14, 3) }, now), "Written by fake-chat with prompt summary-v1 at 14:03.");
});

test("the version line, its links' label, an earlier version's note, the Versions list and why Compare waits", () => {
  const body = JSON.parse(readFileSync(new URL("./api/fixtures/versions-two.json", import.meta.url), "utf8")).body;
  const [v1, v2] = parseVersionList(body).map((d, i) => ({ ...d, createdAt: new Date(2026, 10, 12, 10, 41 + i) }));
  assert.ok(v1 && v2);
  const parsing = { ...v2, lifecycle: { status: "parsing" } } as const;
  const failed = { ...v1, lifecycle: { status: "failed", stage: "parse", failure: { code: "x", message: "x", retryable: true } } } as const;
  const alone = { ...v2, version: { ...v2.version, latest: 2 } };

  assert.deepEqual([versionLine(v1), versionLine(v2)], ["Version 1 of 2", "Version 2 of 2"]);
  assert.deepEqual([otherVersions(v2, 1), otherVersions(v2, 3), otherVersions(v1, 1), otherVersions(alone, 0)], [
    "Earlier version:", "Earlier versions:", "Other version:", "The earlier versions were deleted.",
  ]);
  assert.deepEqual([newerVersion(v1, v2), newerVersion(v1, null)], [
    "A newer version of this document, version 2, was uploaded 12 Nov, 10:42.",
    "A newer version of this document, version 2, is in the vault.",
  ]);
  assert.equal(versionMeta(v1), "phase-e-fixture-terms.txt. Uploaded 12 Nov, 10:41. Ready.");
  assert.deepEqual([withVersion("terms.txt", 2), withVersion("terms.txt", null)], ["terms.txt (version 2)", "terms.txt"]);
  assert.deepEqual(
    [versionCompareBlocked(v2, v1, false), versionCompareBlocked(v1, parsing, false), versionCompareBlocked(v2, failed, false),
      versionCompareBlocked(parsing, v1, false), versionCompareBlocked(v2, v1, true)],
    [
      null,
      "Version 2 is still being indexed. Compare opens when it is ready.",
      "Version 1 failed, so it cannot be compared. Retry it from its page.",
      "This version is still being indexed. Compare opens when it is ready.",
      "A comparison is already running. Compare opens up again when it finishes.",
    ],
  );
  assert.equal(chatAsks(v1), "This chat asks version 1 of phase-e-fixture-terms.txt.");
});

// ------------------------------------------------------------------ phase F

test("a cap of a millionth of a dollar is written with all six of its decimals", () => {
  assert.equal(dollars(1e-6), "$0.000001");
});

test("a whole-dollar cap is written with two decimals", () => {
  assert.equal(dollars(5), "$5.00");
});

test("a cap in cents is written with two decimals, no more", () => {
  assert.equal(dollars(0.25), "$0.25");
});

test("a turn served from the cache reads as saved earlier where a model-written one gives its calls", () => {
  assert.deepEqual([usageLine(parseTurn(fixture("turn-answered-cached"))), usageLine(parseTurn(fixture("turn-answered-one-citation")))], [
    "Saved earlier, no new model call.",
    "2 model calls, 1,994 tokens in, 23 out, $0, 13 ms",
  ]);
});

test("a server failure ends with its request id; a refusal below 500, or a failure with no id, does not", () => {
  const { status, headers, body } = JSON.parse(readFileSync(new URL("./api/fixtures/constructed/error-500-internal.json", import.meta.url), "utf8"));
  const gone = JSON.parse(readFileSync(new URL("./api/fixtures/delete-404-not-found.json", import.meta.url), "utf8"));
  assert.deepEqual(
    [
      explain(parseError(status, body, null, headers["x-request-id"])),
      explain(parseError(502, null, null, "6f1c0a52-3b7e-4d0e-9b61-8f2a4c1d7e90")),
      explain(parseError(gone.status, gone.body, null, "6f1c0a52-3b7e-4d0e-9b61-8f2a4c1d7e90")),
      explain(parseError(502, null, null, null)),
      explain({ kind: "offline" }),
    ],
    [
      "The request failed unexpectedly. Reference phase-f-500-probe.",
      "The vault service couldn't handle this request (HTTP 502). Reference 6f1c0a52-3b7e-4d0e-9b61-8f2a4c1d7e90.",
      "No document has the id 00000000-0000-4000-8000-000000000000.",
      "The vault service couldn't handle this request (HTTP 502).",
      "Can't reach the vault. Check that the web server and the API are running.",
    ],
  );
});

test("an error event inside a streamed ask is a failure while answering, so it carries the stream's reference; a used-up cap does not", async () => {
  const event = (code: string, message: string) =>
    `event: stage\ndata: {"stage": "answering"}\n\nevent: error\ndata: ${JSON.stringify({ error: { code, message, retryable: true, details: {} } })}\n\n`;
  const read = async (body: string) => {
    const r = await readAskStream(new Response(body).body!, () => {}, "0ed48ac5-6b3a-4b04-a2f2-7815b1eb4b28");
    return r.ok ? "answered" : explain(r.error);
  };
  assert.deepEqual(
    [
      await read(event("internal_error", "Answering failed unexpectedly. Try again.")),
      await read(event("ai_unavailable", "The AI provider could not complete the request (timeout).")),
      await read(readFileSync(new URL("./api/fixtures/constructed/ask-stream-error-quota-exceeded.txt", import.meta.url), "utf8")),
    ].map((line) => line.replace(/ at .* on .*, when/, " at <reset>, when")),
    [
      "Answering failed unexpectedly. Try again. Reference 0ed48ac5-6b3a-4b04-a2f2-7815b1eb4b28.",
      "The AI provider could not complete the request (timeout). Reference 0ed48ac5-6b3a-4b04-a2f2-7815b1eb4b28.",
      "Today's AI spending cap of $0.000001 is used up. Asking, comparing and rewriting start again at <reset>, when it resets.",
    ],
  );
});

test("a list that reads itself again says why it failed and that it is retrying, with no reference to change on every retry", () => {
  const { status, headers, body } = JSON.parse(readFileSync(new URL("./api/fixtures/constructed/error-500-internal.json", import.meta.url), "utf8"));
  assert.deepEqual(
    [retrying(parseError(502, null, null, "6f1c0a52-3b7e-4d0e-9b61-8f2a4c1d7e90")), retrying(parseError(status, body, null, headers["x-request-id"]))],
    ["The vault service couldn't handle this request (HTTP 502). Retrying.", "The request failed unexpectedly. Retrying."],
  );
});

test("with no cap reported, Spending cap says none is set, unless the database is down and the cap cannot be read", () => {
  const health = parseHealth(fixture("health"));
  assert.deepEqual([noCap(health), noCap({ ...health, unreachable: ["db"] }), noCap({ ...health, unreachable: ["redis"] })], [
    "No daily spending cap is set. Set DAILY_AI_BUDGET_USD for the api and worker to add one.",
    "The spending cap can't be read while the database is unreachable. Refresh once it is back.",
    "No daily spending cap is set. Set DAILY_AI_BUDGET_USD for the api and worker to add one.",
  ]);
});

test("a cache's hit rate is its hits over its lookups, and nothing with no lookups", () => {
  assert.deepEqual(
    [hitRate({ hits: 2, misses: 1 }), hitRate({ hits: 0, misses: 4 }), hitRate({ hits: 3, misses: 0 }), hitRate({ hits: 1, misses: 999 }), hitRate({ hits: 0, misses: 0 })],
    ["66.7%", "0%", "100%", "0.1%", null],
  );
});
