import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseComparison, parseDocumentPage } from "../api/parse.ts";
import type { ApiError, Comparison, DocumentId } from "../api/domain.ts";
import { cellAt, columns, idle, step, visibleRows, worthRetrying, type CompareRequest, type Run } from "./compare.ts";

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
  retryable: true, retryAfterS: 2, documents: [],
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
    doc: { id: RENEWAL, filename: "sample-renewal-quote.pdf", deleted: false, pagesRead: 3, pageCount: 3 },
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
    kind: "refused", status: 400, code, message: "", retryable, retryAfterS: null, documents: [],
  });
  assert.deepEqual(
    [
      worthRetrying(LIMITED),
      worthRetrying(refused("documents_not_ready", false)),
      worthRetrying(refused("document_not_found", false)),
      worthRetrying(refused("invalid_request", false)),
      worthRetrying(refused("ai_unavailable", null)),
      worthRetrying({ kind: "offline" }),
      worthRetrying({ kind: "broken", status: 502 }),
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
