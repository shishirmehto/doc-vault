import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { settle } from "./client.ts";
import { parseDoc, parseDocumentPage, parseHealth, parseInsights, parsePage, parseSelection, selectionQuery } from "./parse.ts";
import type { DocumentId } from "./domain.ts";

/** A captured response: the API's real status, headers and JSON body. */
function replay<T>(name: string, parse: (w: never) => T, dataStatuses: readonly number[] = []) {
  const { status, headers, body } = JSON.parse(readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), "utf8"));
  return settle(status, JSON.stringify(body), headers["retry-after"] ?? null, parse as (w: unknown) => T, dataStatuses);
}

const MEMO = "5958a67a-0fe6-4c0a-92fb-231c5fa72b87" as DocumentId;
const POLICY = "6e67edc4-27c9-4d1b-9090-4b47a40b3c29" as DocumentId;

test("a failed document keeps the backend's reason and says parsing never finished", () => {
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
    },
  });
});

test("the document list is complete when there is no next cursor", () => {
  const r = replay("documents-page", parseDocumentPage);
  assert.ok(r.ok);
  assert.equal(r.value.complete, true);
  assert.deepEqual(r.value.docs.map((d) => `${d.filename}:${d.lifecycle.status}`), [
    "ui-textless.pdf:failed",
    "ui-renewal-memo-1.txt:ready",
    "u5-check2.txt:ready",
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
    },
  });
});

test("FastAPI's own 422 has no envelope, so it is broken; a 503 carries Retry-After", () => {
  assert.deepEqual(replay("error-422-validation", parseDoc), { ok: false, error: { kind: "broken", status: 422 } });
  const busy = replay("constructed/error-503-ai-unavailable", parseDoc);
  assert.deepEqual(busy.ok ? null : busy.error, {
    kind: "refused",
    status: 503,
    code: "ai_unavailable",
    message: "The AI provider could not complete the request (timeout).",
    retryable: true,
    retryAfterS: 5,
  });
});

test("a 2xx body in the wrong shape is unexpected, a proxy error page is broken", () => {
  assert.deepEqual(settle(200, '{"status":"archived"}', null, parseDoc), {
    ok: false,
    error: { kind: "unexpected", detail: "not a document id: undefined" },
  });
  assert.deepEqual(settle(500, "Internal Server Error", null, parseDoc), { ok: false, error: { kind: "broken", status: 500 } });
});

test("health names the fake provider and anything unreachable", () => {
  assert.deepEqual(replay("health", parseHealth, [503]), { ok: true, value: { aiMode: "fake", unreachable: [] } });
  assert.deepEqual(parseHealth({ db: "unreachable", redis: "ok", ai_mode: "openai" }), { aiMode: "openai", unreachable: ["db"] });
});

test("the URL selects a document, then a cited fact or a page; malformed parts are dropped", () => {
  const sel = (q: string) => parseSelection(new URLSearchParams(q));
  assert.deepEqual(sel(`doc=${POLICY}&cite=fact.3&p=2`), { doc: POLICY, pane: { kind: "cite", cite: { kind: "fact", index: 3 }, page: 2 } });
  assert.deepEqual(sel(`doc=${POLICY}&p=4`), { doc: POLICY, pane: { kind: "page", page: 4 } });
  assert.deepEqual(sel(`doc=${POLICY}&cite=turn.1&p=0`), { doc: POLICY, pane: null });
  assert.deepEqual(sel("doc=not-a-uuid&p=2"), { doc: null, pane: null });
  assert.equal(selectionQuery(sel(`p=2&cite=fact.0&doc=${POLICY}`)), `?doc=${POLICY}&cite=fact.0&p=2`);
  assert.equal(selectionQuery({ doc: null, pane: null }), "");
});
