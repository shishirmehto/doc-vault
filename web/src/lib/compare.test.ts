import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseComparison, parseDocumentPage, parseError } from "../api/parse.ts";
import type { ApiError, Comparison, Doc, DocumentId } from "../api/domain.ts";
import {
  cellAt, columns, columnVersion, idle, step, unlisted, visibleRows, whenRetryChanges, withDocs, worthRetrying, type CompareRequest,
  type Run,
} from "./compare.ts";

function captured(name: string): Comparison {
  return parseComparison(JSON.parse(readFileSync(new URL(`../api/fixtures/${name}.json`, import.meta.url), "utf8")).body);
}

const POLICY = "6e67edc4-27c9-4d1b-9090-4b47a40b3c29" as DocumentId;
const RENEWAL = "23b7e49c-b644-4ccf-8768-5716b69cb9cc" as DocumentId;
const REQUEST: CompareRequest = {
  documents: [{ id: POLICY, filename: "sample-cgl-policy.pdf" }, { id: RENEWAL, filename: "sample-renewal-quote.pdf" }],
  focus: "deductible",
};
const LIMITED: ApiError = {
  kind: "refused", status: 429, code: "rate_limited", message: "Too many requests. Try again in 2 seconds.",
  retryable: true, retryAt: new Date("2026-11-12T12:00:02Z"), documents: [], quota: null, requestId: null,
};

test("a run starts once, and a success forgets it", () => {
  const running = step(idle, { type: "started", request: REQUEST, at: 1000 });
  assert.deepEqual(running, { phase: "running", request: REQUEST, startedAt: 1000 });
  assert.equal(step(running, { type: "started", request: { ...REQUEST, focus: null }, at: 2000 }), running);
  assert.deepEqual(step(running, { type: "compared" }), { phase: "idle" });
});

test("a failed run keeps its request, so Try again sends it again and dismissing it returns to the picker", () => {
  const failed = step(step(idle, { type: "started", request: REQUEST, at: 1000 }), { type: "failed", error: LIMITED });
  assert.deepEqual(failed, { phase: "failed", request: REQUEST, error: LIMITED });
  assert.deepEqual(step(failed, { type: "started", request: REQUEST, at: 5000 }), {
    phase: "running", request: REQUEST, startedAt: 5000,
  });
  assert.deepEqual(step(failed, { type: "dismissed" }), { phase: "idle" });
});

test("a result that arrives for no run in flight changes nothing", () => {
  const failed: Run = { phase: "failed", request: REQUEST, error: LIMITED };
  assert.equal(step(failed, { type: "compared" }), failed);
  assert.equal(step(idle, { type: "failed", error: LIMITED }), idle);
  const running: Run = { phase: "running", request: REQUEST, startedAt: 1 };
  assert.equal(step(running, { type: "dismissed" }), running);
});

test("only the differing rows are left, each still named by its index among all the rows", () => {
  const rows = captured("compare-201-policy-quote").rows;
  assert.deepEqual(visibleRows(rows, true).map(({ row, index }) => `${index} ${row.field}`), [
    "5 Medical Expense Limit",
    "7 General Aggregate Limit",
    "9 Property Damage Deductible",
    "10 Estimated Annual Payroll",
    "11 Rate per $1,000 of Payroll",
  ]);
  assert.deepEqual(visibleRows(rows, false).map(({ index }) => index), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
});

test("a cell's URL resolves to its field, its document and its page; an empty or absent cell to nothing", () => {
  assert.deepEqual(cellAt(captured("compare-201-policy-quote"), { row: 9, col: 1 }), {
    field: "Property Damage Deductible",
    doc: { id: RENEWAL, filename: "sample-renewal-quote.pdf", version: null, deleted: false, pagesRead: 3, pageCount: 3 },
    cell: {
      value: "$5,000 per occurrence",
      pageLabel: "p. 2",
      source: { documentId: RENEWAL, page: 2, quote: "Property Damage Deductible $5,000 per occurrence" },
    },
  });
  const three = captured("compare-201-three-documents");
  assert.equal(cellAt(three, { row: 1, col: 1 }), null);
  assert.equal(cellAt(three, { row: 13, col: 0 }), null);
  assert.equal(cellAt(three, { row: 0, col: 3 }), null);
  assert.equal(cellAt(three, { row: 1, col: 2 })?.cell.pageLabel, "section 1");
});

test("Try again is offered unless the refusal is final; a document that is not ready becomes ready on its own", () => {
  const refused = (code: string, retryable: boolean | null): ApiError => ({
    kind: "refused", status: 400, code, message: "", retryable, retryAt: null, documents: [], quota: null, requestId: null,
  });
  assert.deepEqual(
    [
      worthRetrying(LIMITED, new Date("2026-11-12T12:00:02Z")),
      worthRetrying(refused("documents_not_ready", false)),
      worthRetrying(refused("document_not_found", false)),
      worthRetrying(refused("invalid_request", false)),
      worthRetrying(refused("ai_unavailable", null)),
      worthRetrying({ kind: "offline" }),
      worthRetrying({ kind: "broken", status: 502, requestId: null }),
    ],
    [true, true, false, false, true, true, true],
  );
});

test("the columns are the ticked ready documents in the order they were ticked", () => {
  const docs = parseDocumentPage(JSON.parse(readFileSync(new URL("../api/fixtures/documents-page.json", import.meta.url), "utf8")).body).docs;
  const failed = "3e7c685f-ac8e-4c9f-8a86-f65e5f0a21a7" as DocumentId;
  const gone = "00000000-0000-4000-8000-000000000000" as DocumentId;
  assert.deepEqual(columns(new Set([POLICY, failed, gone, RENEWAL]), docs).map((d) => d.filename), [
    "sample-cgl-policy.pdf",
    "sample-renewal-quote.pdf",
  ]);
  assert.deepEqual(columns(new Set([RENEWAL, POLICY]), docs).map((d) => d.filename), [
    "sample-renewal-quote.pdf",
    "sample-cgl-policy.pdf",
  ]);
});

test("a document older than the live list's newest 100 is offered after them, once, and can be a column", () => {
  const docs = parseDocumentPage(JSON.parse(readFileSync(new URL("../api/fixtures/documents-page.json", import.meta.url), "utf8")).body).docs;
  const policy = docs.find((d) => d.id === POLICY)!;
  const older: Doc = { ...policy, id: "0b6f5f1e-6a43-4d55-9b8e-3c2a1d0e9f77" as DocumentId, filename: "older-policy.pdf" };
  const offered = withDocs(docs, [older]);
  assert.deepEqual(offered.map((d) => d.filename), [
    "live-scan.pdf", "ui-drop-check.txt", "ui-renewal-memo-4.txt", "ui-renewal-memo-2.txt", "ui-renewal-memo-3.txt",
    "ui-textless.pdf", "ui-renewal-memo-1.txt", "u5-check.txt", "sample-endorsement-additional-insured.docx",
    "sample-renewal-quote.pdf", "sample-cgl-policy.pdf", "older-policy.pdf",
  ]);
  assert.deepEqual(columns(new Set([older.id, RENEWAL]), offered).map((d) => d.filename), [
    "older-policy.pdf",
    "sample-renewal-quote.pdf",
  ]);
  assert.deepEqual([withDocs(docs, [policy]).length, withDocs(docs, []).length], [11, 11]);
});

test("Change the documents after a failed Compare with version N reads the version the list lacks and keeps both ticked", () => {
  const docs = parseDocumentPage(JSON.parse(readFileSync(new URL("../api/fixtures/documents-page.json", import.meta.url), "utf8")).body).docs;
  const latest = docs.find((d) => d.id === POLICY)!;
  const first: Doc = { ...latest, id: "0b6f5f1e-6a43-4d55-9b8e-3c2a1d0e9f77" as DocumentId, version: { ...latest.version, number: 1 } };
  const chosen = [first.id, POLICY];
  assert.deepEqual(unlisted(chosen, docs), [first.id]);
  assert.deepEqual(columns(new Set(chosen), withDocs(docs, [first])).map((d) => d.id), chosen);
});

test("a column header marks any version after the first, and version 1 when another column shares its filename", () => {
  const col = (id: string, filename: string, version: number | null) =>
    ({ id: id as DocumentId, filename, version, deleted: false, pagesRead: 1, pageCount: 1 });
  const twins = [col("a", "terms.txt", 1), col("b", "terms.txt", 2)];
  const strangers = [col("a", "policy.pdf", 1), col("b", "quote.pdf", 3)];
  const before = [col("a", "terms.txt", null), col("b", "terms.txt", null)];
  assert.deepEqual(twins.map((d) => columnVersion(twins, d)), [1, 2]);
  assert.deepEqual(strangers.map((d) => columnVersion(strangers, d)), [null, 3]);
  assert.deepEqual(before.map((d) => columnVersion(before, d)), [null, null]);
});

test("a used-up spending cap is not worth retrying until it resets, and is from that instant on", () => {
  const { status, headers, body } = JSON.parse(readFileSync(new URL("../api/fixtures/compare-429-quota-exceeded.json", import.meta.url), "utf8"));
  const capped = parseError(status, body, headers["retry-after"], headers["x-request-id"]);
  const malformed = parseError(status, { error: { ...body.error, details: {} } }, headers["retry-after"]);
  const at = (iso: string) => new Date(iso);
  assert.deepEqual(
    [
      worthRetrying(capped, at("2026-11-12T02:01:22Z")),
      worthRetrying(capped, at("2026-11-12T23:59:59.999Z")),
      worthRetrying(capped, at("2026-11-13T00:00:00Z")),
      worthRetrying(capped, at("2026-11-13T00:00:01Z")),
      worthRetrying(malformed, at("2026-11-12T02:01:22Z")),
    ],
    [false, false, true, true, true],
  );
});

test("Try again comes back at the cap's reset without a reload: the timer wakes then, not a millisecond before", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: Date.parse("2026-11-12T23:59:00Z") });
  const { status, headers, body } = JSON.parse(readFileSync(new URL("../api/fixtures/compare-429-quota-exceeded.json", import.meta.url), "utf8"));
  const capped = parseError(status, body, headers["retry-after"], headers["x-request-id"]);
  const offered: boolean[] = [];
  whenRetryChanges(capped, () => offered.push(worthRetrying(capped)));
  t.mock.timers.tick(59_999);
  assert.deepEqual([offered, worthRetrying(capped)], [[], false]);
  t.mock.timers.tick(1);
  assert.deepEqual([offered, worthRetrying(capped)], [[true], true]);
});

test("a rate limit is not worth retrying until its Retry-After has passed, and the timer wakes then", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: Date.parse("2026-11-12T12:00:00Z") });
  const { status, headers, body } = JSON.parse(readFileSync(new URL("../api/fixtures/upload-429-rate-limited.json", import.meta.url), "utf8"));
  const limited = parseError(status, body, headers["retry-after"], headers["x-request-id"]);
  const offered: boolean[] = [];
  whenRetryChanges(limited, () => offered.push(worthRetrying(limited)));
  t.mock.timers.tick(1_999);
  assert.deepEqual([offered, worthRetrying(limited)], [[], false]);
  t.mock.timers.tick(1);
  assert.deepEqual([offered, worthRetrying(limited)], [[true], true]);
});

test("an answer that cannot change with time schedules no wake, and a cancelled wake never comes", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: Date.parse("2026-11-12T12:00:00Z") });
  const { status, headers, body } = JSON.parse(readFileSync(new URL("../api/fixtures/summary-429-quota-exceeded.json", import.meta.url), "utf8"));
  const capped = parseError(status, body, headers["retry-after"], headers["x-request-id"]);
  let wakes = 0;
  const waited = { ...LIMITED, retryAt: new Date("2026-11-12T11:59:59Z") };
  for (const error of [waited, { kind: "offline" } as const, refusedFinal()]) whenRetryChanges(error, () => wakes++);
  whenRetryChanges(capped, () => wakes++)();
  t.mock.timers.tick(13 * 3600 * 1000);
  assert.deepEqual([wakes, worthRetrying(capped)], [0, true]);
});

function refusedFinal(): ApiError {
  return {
    kind: "refused", status: 404, code: "document_not_found", message: "", retryable: false, retryAt: null, documents: [],
    quota: null, requestId: null,
  };
}
