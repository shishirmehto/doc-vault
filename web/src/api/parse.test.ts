import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { settle } from "./client.ts";
import {
  parseChat, parseChatPage, parseDoc, parseDocumentMetrics, parseDocumentPage, parseHealth, parseHistory, parseInsights,
  parsePage, parseProcessingMetrics, parseSelection, parseTurn, selectionQuery,
} from "./parse.ts";
import type { ChatId, DocumentId, TurnId } from "./domain.ts";

/** A captured response: the API's real status, headers and JSON body. */
function replay<T>(name: string, parse: (w: never) => T, dataStatuses: readonly number[] = []) {
  const { status, headers, body } = JSON.parse(readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), "utf8"));
  return settle(status, JSON.stringify(body), headers["retry-after"] ?? null, parse as (w: unknown) => T, dataStatuses);
}

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
      layout: null,
      processingMs: null,
      createdAt: new Date("2026-11-11T03:06:52.288Z"),
    },
  });
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
      retryAfterS: null,
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
      retryAfterS: null,
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
      retryAfterS: null,
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
      retryAfterS: 2,
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
    retryAfterS: 5,
    documents: [],
  });
});

test("a 2xx body in the wrong shape is unexpected, a proxy error page is broken", () => {
  assert.deepEqual(settle(200, '{"status":"archived"}', null, parseDoc), {
    ok: false,
    error: { kind: "unexpected", detail: "not a document id: undefined" },
  });
  assert.deepEqual(settle(500, "Internal Server Error", null, parseDoc), { ok: false, error: { kind: "broken", status: 500 } });
});

test("health names the fake provider, the upload limit and anything unreachable", () => {
  assert.deepEqual(replay("health", parseHealth, [503]), {
    ok: true,
    value: { aiMode: "fake", unreachable: [], maxUploadBytes: 52428800 },
  });
  assert.deepEqual(
    parseHealth({ db: "unreachable", redis: "ok", ai_mode: "live", embed_model: "text-embedding-3-small", max_upload_bytes: 1024 }),
    { aiMode: "live", unreachable: ["db"], maxUploadBytes: 1024 },
  );
});

test("a delete answers 204 with no body; a 404 names the document the vault no longer has", () => {
  assert.deepEqual(settle(204, "", null, () => null), { ok: true, value: null });
  const gone = replay("delete-404-not-found", () => null);
  assert.deepEqual(gone.ok ? null : gone.error, {
    kind: "refused", status: 404, code: "document_not_found",
    message: "No document has the id 00000000-0000-4000-8000-000000000000.",
    retryable: false, retryAfterS: null, documents: [],
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
  assert.deepEqual(sel("doc=not-a-uuid&p=2"), { kind: "none" });
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
    retryAfterS: null,
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
      retryAfterS: null,
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
    },
  });
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
    "24h", 11, 10, { p50Ms: 438, p95Ms: 617 }, { p50Ms: 114, p95Ms: 592 },
  ]);
  assert.deepEqual(m.steps[0], {
    step: "parse",
    runs: 13,
    failed: 3,
    retries: 0,
    duration: { p50Ms: 20, p95Ms: 174 },
    failures: [{ code: "no_extractable_text", count: 3 }],
  });
  assert.deepEqual(m.steps.map((s) => s.step), ["parse", "embed", "analyze"]);
  assert.deepEqual([m.ai.calls, m.ai.costUsd, m.ai.unpricedCalls, m.ai.rows.length], [130, 0, 0, 6]);
  assert.deepEqual(m.ai.rows[0], {
    purpose: "answer",
    model: "fake-chat",
    calls: 34,
    errors: 0,
    inputTokens: 74451,
    outputTokens: 500,
    costUsd: 0,
    unpricedCalls: 0,
    latency: { p50Ms: 3, p95Ms: 8 },
  });
  assert.deepEqual(m.chat, {
    turns: 43,
    outcomes: { answered: 28, no_evidence: 9, model_declined: 6, unverified: 0 },
    latency: { p50Ms: 12, p95Ms: 155 },
  });
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
