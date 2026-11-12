import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { settle } from "./client.ts";
import {
  parseChat, parseChatPage, parseComparison, parseComparisonPage, parseDoc, parseDocumentMetrics, parseDocumentPage,
  parseError, parseHealth, parseHistory, parseInsights, parsePage, parseProcessingMetrics, parseSelection, parseSummary,
  parseSummaryPage, parseTurn, parseVersionList, selectionQuery,
} from "./parse.ts";
import type { ApiError, ChatId, ComparisonId, DocumentId, Selection, SummaryId, TurnId } from "./domain.ts";

/** A captured response: the API's real status, headers and JSON body. */
/** When each replayed response arrives: 79118 s, the captured cap refusals' Retry-After, before midnight UTC. */
const ARRIVED = Date.parse("2026-11-12T02:01:22Z");

function replay<T>(name: string, parse: (w: never) => T, dataStatuses: readonly number[] = []) {
  const { status, headers, body } = JSON.parse(readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), "utf8"));
  const heard = { retryAfter: headers["retry-after"] ?? null, requestId: headers["x-request-id"] ?? null };
  return settle(status, JSON.stringify(body), heard, parse as (w: unknown) => T, dataStatuses, new Date(ARRIVED));
}

/** A response with neither Retry-After nor X-Request-ID. */
const NO_HEADERS = { retryAfter: null, requestId: null };

const MEMO = "5958a67a-0fe6-4c0a-92fb-231c5fa72b87" as DocumentId;
const POLICY = "6e67edc4-27c9-4d1b-9090-4b47a40b3c29" as DocumentId;
const RENEWAL = "23b7e49c-b644-4ccf-8768-5716b69cb9cc" as DocumentId;
const TEXTLESS = "723645a0-5244-4242-b8d0-84140e7a7dd0" as DocumentId;

test("a failed document keeps the backend's reason and the stage that failed", () => {
  assert.deepEqual(replay("document-pdf-failed-insights-unavailable", parseDoc), {
    ok: true,
    value: {
      id: "723645a0-5244-4242-b8d0-84140e7a7dd0",
      filename: "ui-textless.pdf",
      kind: "pdf",
      sizeBytes: 580,
      lifecycle: {
        status: "failed",
        stage: "parse",
        failure: {
          code: "no_extractable_text",
          message: "None of the 3 pages has a text layer. This looks like a scanned PDF, and OCR is not supported.",
          retryable: false,
        },
      },
      insights: "unavailable",
      documentType: null,
      layout: null,
      processingMs: null,
      createdAt: new Date("2026-11-11T03:06:52.288Z"),
      version: { family: "723645a0-5244-4242-b8d0-84140e7a7dd0", number: 1, latest: 1 },
    },
  });
});

const TERMS_V1 = "3a70e0b8-be92-4f6d-94d1-ff08557ab241";
const TERMS_V2 = "ec435dda-9e95-402f-a719-453bc0beded1";

test("new bytes under a stored name arrive as version 2 of the first upload's family", () => {
  const r = replay("upload-202-version-2", parseDoc);
  assert.deepEqual(r.ok && [r.value.id, r.value.filename, r.value.version], [
    TERMS_V2, "phase-e-fixture-terms.txt", { family: TERMS_V1, number: 2, latest: 2 },
  ]);
});

test("version 1 read after version 2 arrived says the family's latest is 2", () => {
  const r = replay("document-version-1-of-2", parseDoc);
  assert.deepEqual(r.ok && r.value.version, { family: TERMS_V1, number: 1, latest: 2 });
});

test("the versions list is the family oldest first; an unknown id or an unknown replaces is a 404", () => {
  const r = replay("versions-two", parseVersionList);
  assert.deepEqual(r.ok && r.value.map((d) => [d.id, d.version.number, d.version.latest, d.lifecycle.status]), [
    [TERMS_V1, 1, 2, "ready"],
    [TERMS_V2, 2, 2, "ready"],
  ]);
  for (const name of ["versions-404-not-found", "upload-404-replaces-unknown"]) {
    const missing = replay(name, parseVersionList);
    assert.deepEqual(missing.ok ? null : missing.error.kind === "refused" && [missing.error.status, missing.error.code], [
      404, "document_not_found",
    ]);
  }
});

test("a document whose version is past its family's latest, or whose family is no id, is not parsed", () => {
  const { body } = JSON.parse(readFileSync(new URL("./fixtures/upload-202-version-2.json", import.meta.url), "utf8"));
  const parsed = (patch: object) => settle(202, JSON.stringify({ ...body, ...patch }), NO_HEADERS, parseDoc);
  assert.deepEqual([parsed({ latest_version: 1 }), parsed({ family_id: "family" }), parsed({ version: 0 })].map((r) => r.ok), [
    false, false, false,
  ]);
});

test("an embedding document carries its chunk counts and its sections", () => {
  const r = replay("document-txt-embedding-insights-pending", parseDoc);
  assert.ok(r.ok);
  assert.deepEqual(r.value.lifecycle, { status: "embedding", embedded: 0, total: 23 });
  assert.deepEqual(r.value.layout, { pageCount: 12, unit: "section", emptyPages: [] });
  assert.equal(r.value.insights, "pending");
});

test("each captured status parses to its own lifecycle variant", () => {
  const statuses = [
    "document-txt-queued-insights-pending",
    "document-txt-parsing-insights-pending",
    "document-txt-ready-insights-pending",
    "document-txt-ready-insights-ready",
  ].map((name) => {
    const r = replay(name, parseDoc);
    return r.ok ? `${r.value.lifecycle.status}/${r.value.insights}/${r.value.processingMs}` : r.error.kind;
  });
  assert.deepEqual(statuses, ["queued/pending/null", "parsing/pending/null", "ready/pending/242", "ready/ready/242"]);
});

test("upload answers: 202 is new, 200 is the same bytes seen before, 415 is the backend's refusal", () => {
  const receipt = (w: never) => ({ doc: parseDoc(w), duplicate: false });
  const created = replay("upload-202-txt", receipt);
  assert.ok(created.ok);
  assert.deepEqual([created.value.doc.id, created.value.doc.lifecycle], [MEMO, { status: "queued" }]);
  assert.deepEqual(replay("upload-415-unsupported", receipt), {
    ok: false,
    error: {
      kind: "refused",
      status: 415,
      code: "unsupported_type",
      message: "This looks like a binary file of an unknown type. Upload a PDF, a Word .docx file or a plain text file.",
      retryable: false,
      retryAt: null, quota: null, requestId: null,
      documents: [],
    },
  });
});

test("the document list is complete when there is no next cursor", () => {
  const r = replay("documents-page", parseDocumentPage);
  assert.ok(r.ok);
  assert.equal(r.value.complete, true);
  assert.deepEqual(r.value.docs.map((d) => `${d.filename}:${d.lifecycle.status === "failed" ? `failed at ${d.lifecycle.stage}` : d.lifecycle.status}`), [
    "live-scan.pdf:failed at parse",
    "ui-drop-check.txt:ready",
    "ui-renewal-memo-4.txt:ready",
    "ui-renewal-memo-2.txt:ready",
    "ui-renewal-memo-3.txt:ready",
    "ui-textless.pdf:failed at parse",
    "ui-renewal-memo-1.txt:ready",
    "u5-check.txt:ready",
    "sample-endorsement-additional-insured.docx:ready",
    "sample-renewal-quote.pdf:ready",
    "sample-cgl-policy.pdf:ready",
  ]);
});

test("ready insights turn each key fact into a pointer at its page and quote", () => {
  const r = replay("insights-ready-pdf", (w: never) => parseInsights(w, POLICY));
  assert.ok(r.ok && r.value.status === "ready");
  assert.equal(r.value.title, "COMMERCIAL GENERAL LIABILITY POLICY");
  assert.deepEqual(r.value.coverage, { pagesRead: 7, pageCount: 7 });
  assert.deepEqual(r.value.writtenBy, { model: "fake-chat", promptVersion: "insights-v1" });
  assert.deepEqual(r.value.keyFacts[0], {
    label: "Insurer",
    value: "Sample Mutual Insurance Company",
    pageLabel: "p. 1",
    source: { documentId: POLICY, page: 1, quote: "Insurer: Sample Mutual Insurance Company" },
  });
});

test("insights that read only part of a long document say how much", () => {
  const r = replay("insights-ready-partial-coverage", (w: never) => parseInsights(w, MEMO));
  assert.ok(r.ok && r.value.status === "ready");
  assert.deepEqual([r.value.coverage, r.value.keyFacts.length, r.value.droppedKeyFacts], [{ pagesRead: 82, pageCount: 254 }, 0, 0]);
});

test("insights pending, unavailable and failed each parse to their variant", () => {
  const parse = (w: never) => parseInsights(w, MEMO);
  assert.deepEqual(replay("insights-pending", parse), { ok: true, value: { status: "pending" } });
  assert.deepEqual(replay("insights-unavailable", parse), {
    ok: true,
    value: { status: "unavailable", reason: "This document failed to process, so it has no insights." },
  });
  assert.deepEqual(replay("constructed/insights-failed", parse), {
    ok: true,
    value: {
      status: "failed",
      failure: { code: "ai_bad_output", message: "The model returned invalid output twice.", retryable: false },
    },
  });
});

test("a page keeps its label and text; a missing page is the backend's 404", () => {
  const page = replay("page-docx-1", parsePage);
  assert.ok(page.ok);
  assert.deepEqual([page.value.number, page.value.label, page.value.text.slice(0, 29)], [1, "section 1", "SAMPLE - fictitious document."]);
  assert.deepEqual(replay("error-404-page", parsePage), {
    ok: false,
    error: {
      kind: "refused",
      status: 404,
      code: "page_not_found",
      message: "This document has no page 99. It has 7.",
      retryable: false,
      retryAt: null, quota: null, requestId: null,
      documents: [],
    },
  });
});

test("a 422 carries the envelope; a 503 and a 429 carry Retry-After", () => {
  assert.deepEqual(replay("error-422-validation", parseDoc), {
    ok: false,
    error: {
      kind: "refused",
      status: 422,
      code: "invalid_request",
      message: "The request is not valid. path.doc_id: Input should be a valid UUID, invalid character: found `n` at 0",
      retryable: false,
      retryAt: null, quota: null, requestId: null,
      documents: [],
    },
  });
  assert.deepEqual(replay("upload-429-rate-limited", parseDoc), {
    ok: false,
    error: {
      kind: "refused",
      status: 429,
      code: "rate_limited",
      message: "Too many requests. Try again in 2 seconds.",
      retryable: true,
      retryAt: new Date(ARRIVED + 2_000), quota: null, requestId: null,
      documents: [],
    },
  });
  const busy = replay("constructed/error-503-ai-unavailable", parseDoc);
  assert.deepEqual(busy.ok ? null : busy.error, {
    kind: "refused",
    status: 503,
    code: "ai_unavailable",
    message: "The AI provider could not complete the request (timeout).",
    retryable: true,
    retryAt: new Date(ARRIVED + 5_000), quota: null, requestId: null,
    documents: [],
  });
});

const CAP_MESSAGE = "Today's AI spending cap of $0.000001 is used up. It resets at midnight UTC.";
const QUOTA = { limitUsd: 0.000001, spentTodayUsd: 0.01, resetsAt: new Date("2026-11-13T00:00:00Z") };

test("a used-up spending cap is a 429 with the cap, the spend, the reset and the request id, from all four routes", () => {
  const refusal = (name: string) => {
    const r = replay(name, parseTurn);
    return r.ok ? null : r.error;
  };
  const quotaError = (requestId: string) => ({
    kind: "refused", status: 429, code: "quota_exceeded", message: CAP_MESSAGE, retryable: true, retryAt: new Date("2026-11-13T00:00:00Z"),
    documents: [], quota: QUOTA, requestId,
  });
  assert.deepEqual(
    ["ask-429-quota-exceeded", "ask-stream-429-quota-exceeded", "compare-429-quota-exceeded", "summary-429-quota-exceeded"].map(refusal),
    [
      quotaError("phase-f-ask-f00ef6"),
      quotaError("0ed48ac56b3a4b04a2f27815b1eb4b28"),
      quotaError("15bc27734df149428ab3525945fc8537"),
      quotaError("59f57582d6d94b28b0429392156ee616"),
    ],
  );
});

test("quota details that are missing, negative or not a date leave the refusal without a quota; other codes never have one", () => {
  const { body } = JSON.parse(readFileSync(new URL("./fixtures/ask-429-quota-exceeded.json", import.meta.url), "utf8"));
  const withDetails = (details: object, code = "quota_exceeded") =>
    parseError(429, { error: { ...body.error, code, details: { ...body.error.details, ...details } } }, "79118");
  const quotaOf = (e: ApiError) => (e.kind === "refused" ? e.quota : "not refused");
  assert.deepEqual(
    [
      withDetails({}),
      withDetails({ limit_usd: "5" }),
      withDetails({ spent_today_usd: -1 }),
      withDetails({ resets_at: "midnight" }),
      withDetails({ resets_at: null }),
      withDetails({}, "rate_limited"),
      parseError(429, { error: { ...body.error, details: undefined } }, null),
    ].map(quotaOf),
    [QUOTA, null, null, null, null, null, null],
  );
});

test("an unhandled error is a 500 with the envelope and the id the API logged it under", () => {
  assert.deepEqual(replay("constructed/error-500-internal", parseDoc), {
    ok: false,
    error: {
      kind: "refused", status: 500, code: "internal_error", message: "The request failed unexpectedly.", retryable: true,
      retryAt: null, documents: [], quota: null, requestId: "phase-f-500-probe",
    },
  });
});

test("the id a response echoes is the error's; a page in front of the API keeps the id the request sent", () => {
  assert.deepEqual(
    [
      settle(503, "<html>Bad gateway</html>", { retryAfter: null, requestId: "sent-1" }, parseDoc),
      settle(404, JSON.stringify({ error: { code: "document_not_found", message: "Gone." } }), { retryAfter: null, requestId: "echoed-2" }, parseDoc),
    ],
    [
      { ok: false, error: { kind: "broken", status: 503, requestId: "sent-1" } },
      {
        ok: false,
        error: {
          kind: "refused", status: 404, code: "document_not_found", message: "Gone.", retryable: null, retryAt: null,
          documents: [], quota: null, requestId: "echoed-2",
        },
      },
    ],
  );
});

test("insights that hit the cap fail for good with the backend's sentence, on a document that is otherwise ready", () => {
  const doc = replay("document-txt-ready-insights-quota", parseDoc);
  assert.deepEqual(doc.ok && [doc.value.lifecycle, doc.value.insights], [{ status: "ready" }, "failed"]);
  assert.deepEqual(replay("insights-failed-quota-exceeded", (w: never) => parseInsights(w, MEMO)), {
    ok: true,
    value: {
      status: "failed",
      failure: {
        code: "quota_exceeded",
        message: "Insights were not written because today's AI spending cap of $0.000001 is used up. Retry after midnight UTC.",
        retryable: false,
      },
    },
  });
});

test("a 2xx body in the wrong shape is unexpected, a proxy error page is broken", () => {
  assert.deepEqual(settle(200, '{"status":"archived"}', NO_HEADERS, parseDoc), {
    ok: false,
    error: { kind: "unexpected", detail: "not a document id: undefined" },
  });
  assert.deepEqual(settle(500, "Internal Server Error", NO_HEADERS, parseDoc), { ok: false, error: { kind: "broken", status: 500, requestId: null } });
});

test("health names the fake provider, the upload limit, anything unreachable, no cap and the cache", () => {
  assert.deepEqual(replay("health", parseHealth, [503]), {
    ok: true,
    value: { aiMode: "fake", unreachable: [], maxUploadBytes: 52428800, aiBudget: null, cache: "ok" },
  });
  assert.deepEqual(
    parseHealth({
      db: "unreachable", redis: "ok", ai_mode: "live", embed_model: "text-embedding-3-small", max_upload_bytes: 1024,
      ai_budget: null, cache: "off",
    }),
    { aiMode: "live", unreachable: ["db"], maxUploadBytes: 1024, aiBudget: null, cache: "off" },
  );
});

test("health with a cap gives the cap and today's spend, under it and over it; the cache may be off or unreachable", () => {
  const read = (name: string) => {
    const r = replay(name, parseHealth, [503]);
    return r.ok ? [r.value.aiBudget, r.value.cache] : r.error;
  };
  assert.deepEqual(
    ["health-ai-budget-under", "health-ai-budget-over", "constructed/health-cache-off", "constructed/health-cache-unreachable"].map(read),
    [
      [{ limitUsd: 0.000001, spentTodayUsd: 0 }, "ok"],
      [{ limitUsd: 0.000001, spentTodayUsd: 0.01 }, "ok"],
      [null, "off"],
      [null, "unreachable"],
    ],
  );
});

test("a delete answers 204 with no body; a 404 names the document the vault no longer has", () => {
  assert.deepEqual(settle(204, "", NO_HEADERS, () => null), { ok: true, value: null });
  const gone = replay("delete-404-not-found", () => null);
  assert.deepEqual(gone.ok ? null : gone.error, {
    kind: "refused", status: 404, code: "document_not_found",
    message: "No document has the id 00000000-0000-4000-8000-000000000000.",
    retryable: false, retryAt: null, quota: null, requestId: null, documents: [],
  });
});

test("the URL selects a document, then a cited fact or a page; malformed parts are dropped", () => {
  const sel = (q: string) => parseSelection(new URLSearchParams(q));
  assert.deepEqual(sel(`doc=${POLICY}&cite=fact.3&p=2`), {
    kind: "doc",
    doc: POLICY,
    pane: { kind: "cite", cite: { kind: "fact", index: 3 }, page: 2 },
  });
  assert.deepEqual(sel(`doc=${POLICY}&p=4`), { kind: "doc", doc: POLICY, pane: { kind: "page", page: 4 } });
  assert.deepEqual(sel(`doc=${POLICY}&cite=turn.1&p=0`), { kind: "doc", doc: POLICY, pane: null });
  assert.deepEqual(sel("doc=not-a-uuid&p=2"), { kind: "missing", what: "doc" });
  assert.deepEqual(sel("chat=7532866d"), { kind: "missing", what: "chat" });
  assert.deepEqual(sel(""), { kind: "none" });
  assert.equal(selectionQuery(sel(`p=2&cite=fact.0&doc=${POLICY}`)), `?doc=${POLICY}&cite=fact.0&p=2`);
  assert.equal(selectionQuery({ kind: "none" }), "");
});

const CHAT = "7532866d-00fd-4c18-8d0f-590c32093708" as ChatId;
const DEDUCTIBLE_TURN = "3a46fb45-04ed-4542-8a5c-c98a3158d8de" as TurnId;

test("the URL selects a chat, then one turn's citation; a fact cite has no meaning in a chat", () => {
  const sel = (q: string) => parseSelection(new URLSearchParams(q));
  assert.deepEqual(sel(`chat=${CHAT}&cite=turn.${DEDUCTIBLE_TURN}.1&p=3`), {
    kind: "chat",
    chat: CHAT,
    pane: { cite: { turn: DEDUCTIBLE_TURN, ref: 1 }, page: 3 },
  });
  assert.deepEqual(sel(`chat=${CHAT}&cite=fact.1`), { kind: "chat", chat: CHAT, pane: null });
  assert.deepEqual(sel(`chat=${CHAT}&cite=turn.${DEDUCTIBLE_TURN}.0`), { kind: "chat", chat: CHAT, pane: null });
  assert.equal(
    selectionQuery(sel(`cite=turn.${DEDUCTIBLE_TURN}.2&chat=${CHAT}`)),
    `?chat=${CHAT}&cite=turn.${DEDUCTIBLE_TURN}.2`,
  );
});

// ------------------------------------------------------------------ chat

test("a started chat lists its documents in the backend's order", () => {
  assert.deepEqual(replay("chat-start-201-two-documents", parseChat), {
    ok: true,
    value: {
      id: "54da5af8-df4c-46b2-87c3-a688062d0087",
      title: null,
      documents: [
        { id: POLICY, filename: "sample-cgl-policy.pdf", deleted: false },
        { id: RENEWAL, filename: "sample-renewal-quote.pdf", deleted: false },
      ],
      createdAt: new Date("2026-11-11T06:28:09.744Z"),
    },
  });
});

test("a chat read back marks a document deleted since it started, and asking it is refused with that filename", () => {
  const chat = replay("chat-get-deleted-document", parseChat);
  assert.ok(chat.ok);
  assert.deepEqual(chat.value.documents, [
    { id: POLICY, filename: "sample-cgl-policy.pdf", deleted: false },
    { id: "8b3527b2-b23c-4561-b6f1-f8d8351b1cad", filename: "u5-check2.txt", deleted: true },
  ]);
  const ask = replay("ask-409-documents-deleted", parseTurn);
  assert.deepEqual(ask.ok ? null : ask.error, {
    kind: "refused",
    status: 409,
    code: "documents_deleted",
    message: "1 of 2 documents in this chat were deleted. Start a new chat with the documents you want to ask about.",
    retryable: false,
    retryAt: null, quota: null, requestId: null,
    documents: [{ id: "8b3527b2-b23c-4561-b6f1-f8d8351b1cad", filename: "u5-check2.txt" }],
  });
});

test("recent chats come newest first with their documents", () => {
  const r = replay("chats-page", parseChatPage);
  assert.ok(r.ok);
  assert.deepEqual(
    r.value.map((c) => `${c.createdAt.toISOString()} ${c.documents.map((d) => d.filename).join(" + ")}`),
    [
      "2026-11-11T06:28:09.744Z sample-cgl-policy.pdf + sample-renewal-quote.pdf",
      "2026-11-11T04:02:37.201Z sample-cgl-policy.pdf + sample-renewal-quote.pdf",
      "2026-11-11T03:59:42.116Z sample-cgl-policy.pdf",
      "2026-11-11T03:52:45.455Z sample-endorsement-additional-insured.docx",
      "2026-11-11T03:52:32.215Z sample-cgl-policy.pdf",
    ],
  );
});

test("a 409 names the documents that are not ready", () => {
  assert.deepEqual(replay("chat-start-409-not-ready", parseChat), {
    ok: false,
    error: {
      kind: "refused",
      status: 409,
      code: "documents_not_ready",
      message: "1 of 2 documents are not ready.",
      retryable: false,
      retryAt: null, quota: null, requestId: null,
      documents: [{ id: TEXTLESS, filename: "ui-textless.pdf" }],
    },
  });
});

test("a cited answer becomes text and a citation pointing at its page and quote", () => {
  assert.deepEqual(replay("turn-answered-one-citation", parseTurn), {
    ok: true,
    value: {
      id: DEDUCTIBLE_TURN,
      question: "What is the property damage deductible?",
      readAs: null,
      outcome: {
        kind: "answered",
        parts: [
          { kind: "text", text: "Property Damage Deductible $2,500 per occurrence" },
          {
            kind: "cite",
            citation: {
              ref: 1,
              filename: "sample-cgl-policy.pdf",
              pageLabel: "p. 2",
              source: { documentId: POLICY, page: 2, quote: "Property Damage Deductible $2,500 per occurrence" },
            },
          },
        ],
        citations: [
          {
            ref: 1,
            filename: "sample-cgl-policy.pdf",
            pageLabel: "p. 2",
            source: { documentId: POLICY, page: 2, quote: "Property Damage Deductible $2,500 per occurrence" },
          },
        ],
      },
      usage: { calls: 2, inputTokens: 1994, outputTokens: 23, costUsd: 0 },
      latencyMs: 13,
      suggestions: ["What is the Policy Number?", "What is the Named Insured?", "What is the Mailing Address?"],
      cached: false,
    },
  });
});

const CACHE_DOC = "056ba6c3-8e83-46dc-991c-6ef42eb52e56" as DocumentId;

test("a first question answered before comes back cached, with no model call, and history keeps the mark", () => {
  const cached = (r: ReturnType<typeof parseTurn>) => [r.cached, r.usage, r.latencyMs, r.outcome.kind === "answered" && r.outcome.citations[0]?.source];
  const turn = replay("turn-answered-cached", parseTurn);
  const history = replay("history-cached-turn", parseHistory);
  const expected = [
    true,
    { calls: 0, inputTokens: 0, outputTokens: 0, costUsd: 0 },
    1,
    { documentId: CACHE_DOC, page: 1, quote: "Property Damage Deductible: $2,500 per occurrence" },
  ];
  assert.deepEqual(turn.ok && cached(turn.value), expected);
  assert.deepEqual(history.ok && history.value.map(cached), [expected]);
});

test("a turn whose cached flag is missing or not a boolean is a body in the wrong shape", () => {
  const { body } = JSON.parse(readFileSync(new URL("./fixtures/turn-answered-cached.json", import.meta.url), "utf8"));
  const older = { ...body };
  delete older.cached;
  assert.deepEqual(
    [settle(200, JSON.stringify(older), NO_HEADERS, parseTurn), settle(200, JSON.stringify({ ...body, cached: "yes" }), NO_HEADERS, parseTurn)],
    [
      { ok: false, error: { kind: "unexpected", detail: "not a cached flag: undefined" } },
      { ok: false, error: { kind: "unexpected", detail: 'not a cached flag: "yes"' } },
    ],
  );
});

test("a follow-up keeps the standalone question it was read as", () => {
  const r = replay("turn-answered-read-as", parseTurn);
  assert.ok(r.ok);
  assert.deepEqual([r.value.question, r.value.readAs], [
    "Does it apply per occurrence?",
    "What is the property damage deductible? Does it apply per occurrence?",
  ]);
});

test("an answer from two documents cites each one with its own page", () => {
  const r = replay("turn-answered-two-documents", parseTurn);
  assert.ok(r.ok && r.value.outcome.kind === "answered");
  assert.deepEqual(
    r.value.outcome.parts.map((p) => (p.kind === "text" ? p.text : `[${p.citation.ref}: ${p.citation.filename} ${p.citation.pageLabel}]`)),
    [
      "General Aggregate Limit $3,000,000",
      "[1: sample-renewal-quote.pdf p. 2]",
      " General Aggregate Limit $2,000,000",
      "[2: sample-cgl-policy.pdf p. 2]",
    ],
  );
});

test("each not-found reason keeps the backend's sentence and cites nothing", () => {
  const outcome = (name: string) => {
    const r = replay(name, parseTurn);
    return r.ok ? r.value.outcome : r.error;
  };
  assert.deepEqual(outcome("turn-not-found-no-evidence"), {
    kind: "not_found",
    reason: "no_evidence",
    message: "I could not find anything about this in sample-cgl-policy.pdf, so I can't answer it from the selected documents.",
  });
  assert.deepEqual(outcome("turn-not-found-model-declined"), {
    kind: "not_found",
    reason: "model_declined",
    message: "The selected documents do not say.",
  });
  assert.deepEqual(outcome("constructed/turn-not-found-unverified"), {
    kind: "not_found",
    reason: "unverified",
    message:
      "I found passages that look related, but I could not verify an answer against their text, so I am not giving one.",
  });
});

test("an answer with no citations is all text, its marker included", () => {
  const r = replay("constructed/turn-answered-zero-citations", parseTurn);
  assert.ok(r.ok);
  assert.deepEqual(r.value.outcome, {
    kind: "answered",
    parts: [{ kind: "text", text: "Property Damage Deductible $2,500 per occurrence [1]" }],
    citations: [],
  });
});

test("history is oldest first, and a missing chat or an empty question is refused", () => {
  const history = replay("history-policy-four-turns", parseHistory);
  assert.ok(history.ok);
  assert.deepEqual(
    history.value.map((t) => `${t.question} -> ${t.outcome.kind === "answered" ? "answered" : t.outcome.reason}`),
    [
      "What is the property damage deductible? -> answered",
      "Does it apply per occurrence? -> answered",
      "Who won the 2022 football World Cup? -> no_evidence",
      "Exclusions? -> model_declined",
    ],
  );
  const missing = replay("chat-404-not-found", parseHistory);
  assert.deepEqual(missing.ok ? null : [missing.error.kind, missing.error.kind === "refused" && missing.error.code], [
    "refused",
    "chat_not_found",
  ]);
  const empty = replay("ask-422-empty-question", parseTurn);
  assert.deepEqual(empty.ok ? null : empty.error.kind === "refused" && [empty.error.status, empty.error.code, empty.error.message], [
    422,
    "invalid_request",
    "The request is not valid. body.question: String should have at least 1 character",
  ]);
});

// ------------------------------------------------------------------ metrics

test("document metrics count every status and list failures most first", () => {
  assert.deepEqual(replay("metrics-documents", parseDocumentMetrics), {
    ok: true,
    value: {
      total: 11,
      byStatus: { queued: 0, parsing: 0, embedding: 0, ready: 9, failed: 2 },
      failures: [{ code: "no_extractable_text", count: 2 }],
      pages: 697,
      chunks: 1303,
      bytes: 2025683,
    },
  });
});

test("processing metrics keep the step order, the AI rows and the chat outcomes", () => {
  const r = replay("metrics-processing-24h", parseProcessingMetrics);
  assert.ok(r.ok);
  const m = r.value;
  assert.deepEqual([m.window, m.uploaded, m.indexed, m.timeToReady, m.queueWait], [
    "24h", 9, 76, { p50Ms: 360, p95Ms: 568 }, { p50Ms: 92, p95Ms: 576 },
  ]);
  assert.deepEqual(m.steps[0], {
    step: "parse",
    runs: 89,
    failed: 13,
    retries: 0,
    duration: { p50Ms: 8, p95Ms: 1077 },
    failures: [{ code: "too_many_pages", count: 8 }, { code: "no_extractable_text", count: 5 }],
  });
  assert.deepEqual(m.steps.map((s) => s.step), ["parse", "embed", "analyze"]);
  assert.deepEqual([m.ai.calls, m.ai.costUsd, m.ai.unpricedCalls, m.ai.rows.length], [771, 0, 0, 7]);
  assert.deepEqual(m.ai.rows[0], {
    purpose: "answer",
    model: "fake-chat",
    calls: 90,
    errors: 0,
    inputTokens: 186023,
    outputTokens: 2158,
    costUsd: 0,
    unpricedCalls: 0,
    latency: { p50Ms: 500, p95Ms: 774 },
  });
  assert.deepEqual(m.chat, {
    turns: 103,
    outcomes: { answered: 85, no_evidence: 11, model_declined: 7, unverified: 0 },
    latency: { p50Ms: 133, p95Ms: 839 },
  });
});

test("processing metrics carry the cache's hits and misses since they were first counted, or null with no cache", () => {
  const cache = (name: string) => {
    const r = replay(name, parseProcessingMetrics);
    return r.ok ? r.value.cache : r.error;
  };
  assert.deepEqual(
    ["metrics-processing-24h", "metrics-processing-24h-capped", "constructed/metrics-processing-24h-cache-off", "metrics-processing-1h"].map(cache),
    [
      { embedding: { hits: 0, misses: 2 }, answer: { hits: 2, misses: 1 }, countedSince: new Date("2026-11-12T02:00:55.418Z") },
      { embedding: { hits: 0, misses: 4 }, answer: { hits: 0, misses: 1 }, countedSince: new Date("2026-11-12T02:01:09.109Z") },
      null,
      null,
    ],
  );
});

test("an empty window has no samples, so every spread is null", () => {
  const r = replay("metrics-processing-1h", parseProcessingMetrics);
  assert.ok(r.ok);
  assert.deepEqual(
    [r.value.timeToReady, r.value.queueWait, r.value.chat.latency, ...r.value.steps.map((s) => s.duration)],
    [null, null, null, null, null, null],
  );
  assert.equal(r.value.chat.turns, 0);
});

test("the URL selects the metrics view; 24h is the default window", () => {
  const sel = (q: string) => parseSelection(new URLSearchParams(q));
  assert.deepEqual(sel("view=metrics"), { kind: "metrics", window: "24h" });
  assert.deepEqual(sel(`view=metrics&window=7d&doc=${POLICY}`), { kind: "metrics", window: "7d" });
  assert.deepEqual(sel("view=metrics&window=1h"), { kind: "metrics", window: "24h" });
  assert.equal(selectionQuery({ kind: "metrics", window: "24h" }), "?view=metrics");
  assert.equal(selectionQuery({ kind: "metrics", window: "7d" }), "?view=metrics&window=7d");
});

const ENDORSEMENT = "054b85f0-6107-4030-8274-f72b9ae7a80b" as DocumentId;
const DEDUCTIBLE_ROW = {
  field: "Property Damage Deductible",
  differs: true,
  cells: [
    {
      value: "$2,500 per occurrence",
      pageLabel: "p. 2",
      source: { documentId: POLICY, page: 2, quote: "Property Damage Deductible $2,500 per occurrence" },
    },
    {
      value: "$5,000 per occurrence",
      pageLabel: "p. 2",
      source: { documentId: RENEWAL, page: 2, quote: "Property Damage Deductible $5,000 per occurrence" },
    },
  ],
};

test("a comparison lines up each field across its documents and says which differ", () => {
  const r = replay("compare-201-policy-quote", parseComparison);
  assert.ok(r.ok);
  const { rows, ...rest } = r.value;
  assert.deepEqual(rest, {
    id: "d7f2d3b9-38cf-4933-b031-6dd597edd899",
    documents: [
      { id: POLICY, filename: "sample-cgl-policy.pdf", version: null, deleted: false, pagesRead: 7, pageCount: 7 },
      { id: RENEWAL, filename: "sample-renewal-quote.pdf", version: null, deleted: false, pagesRead: 3, pageCount: 3 },
    ],
    focus: null,
    droppedCells: 0,
    model: "fake-chat",
    promptVersion: "compare-v1",
    usage: { calls: 1, inputTokens: 2292, outputTokens: 169, costUsd: 0 },
    createdAt: new Date("2026-11-11T16:23:06.321Z"),
  });
  assert.equal(rows.length, 12);
  assert.deepEqual(rows.filter((row) => row.differs).map((row) => row.field), [
    "Medical Expense Limit",
    "General Aggregate Limit",
    "Property Damage Deductible",
    "Estimated Annual Payroll",
    "Rate per $1,000 of Payroll",
  ]);
  assert.deepEqual(rows[9], DEDUCTIBLE_ROW);
});

test("a focus keeps only the fields it names", () => {
  const r = replay("compare-201-focus-deductible", parseComparison);
  assert.ok(r.ok);
  assert.equal(r.value.focus, "deductible");
  assert.deepEqual(r.value.rows, [DEDUCTIBLE_ROW]);
});

test("a document that does not state a field has an empty cell in its column", () => {
  const r = replay("compare-201-three-documents", parseComparison);
  assert.ok(r.ok);
  assert.equal(r.value.rows.length, 13);
  assert.deepEqual(r.value.rows[1], {
    field: "Policy Number",
    differs: true,
    cells: [
      {
        value: "SAMPLE-CGL-2026-0001",
        pageLabel: "p. 1",
        source: { documentId: POLICY, page: 1, quote: "Policy Number: SAMPLE-CGL-2026-0001" },
      },
      null,
      {
        value: "SAMPLE-CGL-2026-0001",
        pageLabel: "section 1",
        source: { documentId: ENDORSEMENT, page: 1, quote: "Policy Number | SAMPLE-CGL-2026-0001" },
      },
    ],
  });
});

test("a compared document carries its version, or null when the comparison predates versions", () => {
  const { body } = JSON.parse(readFileSync(new URL("./fixtures/compare-201-policy-quote.json", import.meta.url), "utf8"));
  const versioned = { ...body, documents: body.documents.map((d: object, i: number) => ({ ...d, version: i + 1 })) };
  const r = settle(201, JSON.stringify(versioned), NO_HEADERS, parseComparison);
  const stored = replay("compare-201-policy-quote", parseComparison);
  assert.deepEqual(r.ok && r.value.documents.map((d) => d.version), [1, 2]);
  assert.deepEqual(stored.ok && stored.value.documents.map((d) => d.version), [null, null]);
});

test("a comparison read back after a delete marks the document and keeps its cells and page labels", () => {
  const r = replay("comparison-get-deleted-document", parseComparison);
  assert.ok(r.ok);
  const gone = "31bcc18c-c6e7-48d2-b436-82c7e3dfe2ec" as DocumentId;
  assert.deepEqual(r.value.documents[1], { id: gone, filename: "phase-c-shared-86f623.txt", version: null, deleted: true, pagesRead: 1, pageCount: 1 });
  assert.deepEqual(r.value.rows.map((row) => [row.field, row.differs, row.cells[1]]), [
    ["Policy Number", true, { value: "PHASE-C-1", pageLabel: "section 1", source: { documentId: gone, page: 1, quote: "Policy Number: PHASE-C-1" } }],
    [
      "Named Insured",
      false,
      { value: "Example Contractors LLC", pageLabel: "section 1", source: { documentId: gone, page: 1, quote: "Named Insured: Example Contractors LLC" } },
    ],
  ]);
});

test("recent comparisons come newest first with their documents, focus and differing fields", () => {
  const r = replay("comparisons-page", parseComparisonPage);
  assert.ok(r.ok);
  assert.deepEqual(
    r.value.slice(0, 5).map((c) => [
      c.id,
      c.documents.map((d) => (d.deleted ? `${d.filename} (deleted)` : d.filename)).join(" + "),
      c.focus,
      `${c.rows.filter((row) => row.differs).length} of ${c.rows.length}`,
    ]),
    [
      ["f083878a-0197-4bcf-8533-d1d085d5beb8", "sample-cgl-policy.pdf + phase-c-shared-86f623.txt (deleted)", null, "1 of 2"],
      [
        "0ebe9ecd-db9f-4835-beef-6c78e579de9a",
        "sample-cgl-policy.pdf + sample-renewal-quote.pdf + sample-endorsement-additional-insured.docx",
        null,
        "12 of 13",
      ],
      ["35741edd-c098-41aa-aaaf-482c2c978290", "sample-cgl-policy.pdf + sample-renewal-quote.pdf", "deductible", "1 of 1"],
      ["d7f2d3b9-38cf-4933-b031-6dd597edd899", "sample-cgl-policy.pdf + sample-renewal-quote.pdf", null, "5 of 12"],
      ["30ab1b9f-12bf-4793-aafb-242221818319", "sample-cgl-policy.pdf + phase-c-shared-b7300b.txt (deleted)", null, "0 of 0"],
    ],
  );
  assert.equal(r.value.length, 9);
});

test("comparing refuses one document, a document that is not ready, and an unknown comparison", () => {
  const one = replay("compare-422-one-document", parseComparison);
  assert.deepEqual(one.ok ? null : one.error, {
    kind: "refused", status: 422, code: "invalid_request",
    message: "The request is not valid. body.document_ids: List should have at least 2 items after validation, not 1",
    retryable: false, retryAt: null, quota: null, requestId: null, documents: [],
  });
  const busy = replay("compare-409-not-ready", parseComparison);
  assert.deepEqual(busy.ok ? null : busy.error, {
    kind: "refused", status: 409, code: "documents_not_ready", message: "1 of 2 documents are not ready.",
    retryable: false, retryAt: null, quota: null, requestId: null,
    documents: [{ id: "41474e86-1b59-46ad-980b-c0355948cd08", filename: "phase-c-large-86f623.txt" }],
  });
  const missing = replay("comparison-404-not-found", parseComparison);
  assert.deepEqual(missing.ok ? null : missing.error, {
    kind: "refused", status: 404, code: "comparison_not_found",
    message: "No comparison has the id 00000000-0000-4000-8000-000000000000.",
    retryable: false, retryAt: null, quota: null, requestId: null, documents: [],
  });
});

test("a row that does not line up with the columns is a body in the wrong shape", () => {
  const captured = JSON.parse(readFileSync(new URL("./fixtures/compare-201-policy-quote.json", import.meta.url), "utf8")).body;
  const broken = (edit: (row: { cells: Record<string, unknown>[] }) => void) => {
    const body = structuredClone(captured);
    edit(body.rows[9]);
    return settle(201, JSON.stringify(body), NO_HEADERS, parseComparison);
  };
  assert.equal(broken(() => {}).ok, true);
  assert.deepEqual(broken((row) => row.cells.pop()), {
    ok: false,
    error: { kind: "unexpected", detail: "the row Property Damage Deductible has 1 cells for 2 documents" },
  });
  assert.deepEqual(broken((row) => row.cells.reverse()), {
    ok: false,
    error: { kind: "unexpected", detail: `a cell for ${RENEWAL} in the column of ${POLICY}` },
  });
  assert.deepEqual(broken((row) => { row.cells[1]!.page = null; }), {
    ok: false,
    error: { kind: "unexpected", detail: "not a cell's page" },
  });
  assert.deepEqual(broken((row) => { row.cells[0]!.quote = null; }), {
    ok: false,
    error: { kind: "unexpected", detail: "not a cell's quote" },
  });
  assert.deepEqual(broken((row) => { row.cells[0]!.value = null; }), {
    ok: false,
    error: { kind: "unexpected", detail: "not a cell value" },
  });
});

const COMPARISON = "d7f2d3b9-38cf-4933-b031-6dd597edd899" as ComparisonId;

test("the URL selects the compare screen, with a document to tick; a malformed one is dropped", () => {
  const sel = (q: string) => parseSelection(new URLSearchParams(q));
  assert.deepEqual(sel("view=compare"), { kind: "compare", with: null });
  assert.deepEqual(sel(`view=compare&with=${POLICY}`), { kind: "compare", with: POLICY });
  assert.deepEqual(sel("view=compare&with=not-an-id"), { kind: "compare", with: null });
  assert.equal(selectionQuery({ kind: "compare", with: null }), "?view=compare");
  assert.equal(selectionQuery({ kind: "compare", with: POLICY }), `?view=compare&with=${POLICY}`);
});

test("the URL selects a comparison, then one cell's page, a page away from it, and only the differences", () => {
  const sel = (q: string) => parseSelection(new URLSearchParams(q));
  assert.deepEqual(sel(`compare=${COMPARISON}&cite=cmp.9.1&p=3&only=differences`), {
    kind: "comparison",
    comparison: COMPARISON,
    pane: { cite: { row: 9, col: 1 }, page: 3 },
    only: true,
  });
  assert.deepEqual(sel(`compare=${COMPARISON}&cite=cmp.0.0`), {
    kind: "comparison", comparison: COMPARISON, pane: { cite: { row: 0, col: 0 }, page: null }, only: false,
  });
  for (const bad of ["cmp.1", "cmp.1.5", "cmp.01.0", "cmp.-1.0", "fact.1", `turn.${DEDUCTIBLE_TURN}.1`]) {
    assert.deepEqual(sel(`compare=${COMPARISON}&cite=${bad}&p=2&only=yes`), {
      kind: "comparison", comparison: COMPARISON, pane: null, only: false,
    }, bad);
  }
  assert.deepEqual(sel("compare=d7f2d3b9"), { kind: "missing", what: "comparison" });
  assert.deepEqual(sel("compare="), { kind: "missing", what: "comparison" });
});

const REWRITTEN = "5cb8a257-f588-415e-a12c-df8a997cecdb" as DocumentId;
const BRIEF = "8b9267e2-7aa6-409a-9e54-84740ab28388" as SummaryId;

test("a rewritten summary keeps the focus as the backend stored it, and each source on its page", () => {
  assert.deepEqual(replay("summary-201-brief-focus-client", parseSummary), {
    ok: true,
    value: {
      id: BRIEF,
      documentId: REWRITTEN,
      options: { length: "brief", focus: "property damage deductibles", tone: "client" },
      text: "Here is what phase-d-summary-1cd9f3.txt means for you. A property damage deductible of $5,000 applies to each " +
        "occurrence. The deductible is paid by the insured before the insurer pays any claim.",
      sources: [
        { page: 2, pageLabel: "section 2", quote: "A property damage deductible of $5,000 applies to each occurrence." },
        { page: 3, pageLabel: "section 3", quote: "The deductible is paid by the insured before the insurer pays any claim." },
      ],
      droppedSources: 0,
      writtenBy: { model: "fake-chat", promptVersion: "summary-v1" },
      cached: false,
      createdAt: new Date("2026-11-11T18:57:52.235Z"),
    },
  });
});

test("the same options again answer 200 with the stored summary, marked cached", () => {
  const r = replay("summary-200-cached", parseSummary);
  assert.deepEqual(r.ok && [r.value.id, r.value.cached, r.value.options], [
    BRIEF, true, { length: "brief", focus: "property damage deductibles", tone: "client" },
  ]);
});

test("a document's summaries come newest first, and the list is complete when there is no next cursor", () => {
  const r = replay("summaries-page", parseSummaryPage);
  assert.ok(r.ok);
  assert.deepEqual(
    r.value.summaries.map((s) => [s.id, s.options, s.sources.length, s.cached]),
    [
      ["03172f80-a399-4fe4-bb8d-e8b8b07d5a50", { length: "standard", focus: null, tone: "plain" }, 4, true],
      ["b083fceb-16ca-4457-9d7d-e1809d72c2b3", { length: "detailed", focus: null, tone: "formal" }, 6, true],
      [BRIEF, { length: "brief", focus: "property damage deductibles", tone: "client" }, 2, true],
    ],
  );
  assert.equal(r.value.complete, true);
  const captured = JSON.parse(readFileSync(new URL("./fixtures/summaries-page.json", import.meta.url), "utf8")).body;
  const more = settle(200, JSON.stringify({ ...captured, next_cursor: "abc" }), NO_HEADERS, parseSummaryPage);
  assert.deepEqual(more.ok ? more.value.complete : more.error, false);
});

test("summarising refuses a document that is not ready and an unknown length; listing refuses an unknown document", () => {
  const busy = replay("summary-409-not-ready", parseSummary);
  assert.deepEqual(busy.ok ? null : busy.error, {
    kind: "refused", status: 409, code: "documents_not_ready", message: "1 of 1 documents are not ready.",
    retryable: false, retryAt: null, quota: null, requestId: null,
    documents: [{ id: "2da79ec2-177a-4c59-9c06-fc2ae84f83d6", filename: "phase-d-large-1cd9f3.txt" }],
  });
  const short = replay("summary-422-length", parseSummary);
  assert.deepEqual(short.ok ? null : short.error, {
    kind: "refused", status: 422, code: "invalid_request",
    message: "The request is not valid. body.length: Input should be 'brief', 'standard' or 'detailed'",
    retryable: false, retryAt: null, quota: null, requestId: null, documents: [],
  });
  const missing = replay("summaries-404-not-found", parseSummaryPage);
  assert.deepEqual(missing.ok ? null : missing.error, {
    kind: "refused", status: 404, code: "document_not_found",
    message: "No document has the id 00000000-0000-4000-8000-000000000000.",
    retryable: false, retryAt: null, quota: null, requestId: null, documents: [],
  });
});

test("a summary with no sources parses; an unknown length or tone, a source on no page, or a non-boolean cached does not", () => {
  const captured = JSON.parse(readFileSync(new URL("./fixtures/summary-201-brief-focus-client.json", import.meta.url), "utf8")).body;
  const edited = (edit: (body: Record<string, unknown> & { sources: Record<string, unknown>[] }) => void) => {
    const body = structuredClone(captured);
    edit(body);
    const r = settle(201, JSON.stringify(body), NO_HEADERS, parseSummary);
    return r.ok ? { sources: r.value.sources, droppedSources: r.value.droppedSources } : r.error;
  };
  assert.deepEqual(edited((b) => { b.sources = []; b.dropped_sources = 2; }), { sources: [], droppedSources: 2 });
  assert.deepEqual(edited((b) => { b.length = "short"; }), { kind: "unexpected", detail: "not a summary length" });
  assert.deepEqual(edited((b) => { b.tone = "casual"; }), { kind: "unexpected", detail: "not a summary tone" });
  assert.deepEqual(edited((b) => { b.sources[1]!.page = 0; }), { kind: "unexpected", detail: "a source on page 0" });
  assert.deepEqual(edited((b) => { b.cached = "yes"; }), { kind: "unexpected", detail: 'not a cached flag: "yes"' });
});

test("the URL selects a rewritten summary's source on a document; a malformed one is dropped like a fact's", () => {
  const sel = (q: string) => parseSelection(new URLSearchParams(q));
  assert.deepEqual(sel(`doc=${REWRITTEN}&cite=sum.${BRIEF}.1&p=2`), {
    kind: "doc",
    doc: REWRITTEN,
    pane: { kind: "cite", cite: { kind: "summary", summary: BRIEF, index: 1 }, page: 2 },
  });
  for (const bad of [`sum.${BRIEF}.6`, `sum.${BRIEF}.01`, `sum.${BRIEF}`, "sum.8b9267e2.1", `sum.${BRIEF}.-1`, `summary.${BRIEF}.1`]) {
    assert.deepEqual(sel(`doc=${REWRITTEN}&cite=${bad}&p=3`), { kind: "doc", doc: REWRITTEN, pane: { kind: "page", page: 3 } }, bad);
    assert.deepEqual(sel(`doc=${REWRITTEN}&cite=${bad}`), { kind: "doc", doc: REWRITTEN, pane: null }, bad);
  }
  assert.deepEqual(sel(`chat=${CHAT}&cite=sum.${BRIEF}.1`), { kind: "chat", chat: CHAT, pane: null });
  assert.equal(
    selectionQuery(sel(`p=4&cite=sum.${BRIEF}.0&doc=${REWRITTEN}`)),
    `?doc=${REWRITTEN}&cite=sum.${BRIEF}.0&p=4`,
  );
});

test("every selection a link can carry reads back from the query it writes", () => {
  const forms: Selection[] = [
    { kind: "none" },
    { kind: "metrics", window: "7d" },
    { kind: "doc", doc: POLICY, pane: { kind: "cite", cite: { kind: "fact", index: 2 }, page: 4 } },
    { kind: "doc", doc: REWRITTEN, pane: { kind: "cite", cite: { kind: "summary", summary: BRIEF, index: 5 }, page: null } },
    { kind: "chat", chat: CHAT, pane: { cite: { turn: DEDUCTIBLE_TURN, ref: 1 }, page: null } },
    { kind: "compare", with: null },
    { kind: "compare", with: RENEWAL },
    { kind: "comparison", comparison: COMPARISON, pane: null, only: false },
    { kind: "comparison", comparison: COMPARISON, pane: null, only: true },
    { kind: "comparison", comparison: COMPARISON, pane: { cite: { row: 12, col: 4 }, page: null }, only: false },
    { kind: "comparison", comparison: COMPARISON, pane: { cite: { row: 0, col: 1 }, page: 7 }, only: true },
  ];
  for (const form of forms) {
    assert.deepEqual(parseSelection(new URLSearchParams(selectionQuery(form))), form, selectionQuery(form));
  }
  assert.equal(
    selectionQuery(forms[10]!),
    `?compare=${COMPARISON}&cite=cmp.0.1&p=7&only=differences`,
  );
});
