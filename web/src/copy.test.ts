import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseComparison, parseComparisonPage, parseDocumentPage } from "./api/parse.ts";
import type { ApiError, DocumentId } from "./api/domain.ts";
import {
  cannotOpen, compareBlocked, compareFailed, comparing, deletedColumns, differCount, docMeta, droppedCells, fieldCount,
  focusLine, notReadyNames, partlyRead,
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
    [comparing(plain.documents), comparing(gone.documents), focusLine(plain.focus), focusLine(focused.focus),
      fieldCount(plain.rows), fieldCount(focused.rows), fieldCount([]), fieldCount(gone.rows.slice(1))],
    [
      "Comparing sample-cgl-policy.pdf and sample-renewal-quote.pdf",
      "Comparing sample-cgl-policy.pdf and phase-c-shared-86f623.txt (deleted)",
      "No focus: the fields a broker compares",
      "Focus: deductible",
      "12 fields, 5 differ",
      "1 field, 1 differs",
      "No field was found in more than one document",
      "1 field, none differ",
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
  const memo = { id: "70eebfea-0db0-468f-b3e3-58c67c2f6d5f" as DocumentId, filename: "ui-renewal-memo-2.txt", deleted: false, pagesRead: 40, pageCount: 254 };
  assert.deepEqual(
    [droppedCells(1), droppedCells(3), partlyRead(memo, "section"), partlyRead({ ...memo, pagesRead: 1, pageCount: 2 }, "page"),
      deletedColumns(["a.txt"]), deletedColumns(["a.txt", "b.pdf"])],
    [
      "1 value was left out because its quote was not on the page the model cited.",
      "3 values were left out because their quotes were not on the pages the model cited.",
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
    retryAfterS: null,
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
    retryAfterS: null, documents: [],
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
