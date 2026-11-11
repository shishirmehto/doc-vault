import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseDocumentPage, parseSummaryPage } from "../api/parse.ts";
import type { PaneRequest, SummaryCite, SummaryId } from "../api/domain.ts";
import { citedFact, citedSource, summarySourceView } from "./summaryPane.ts";

function fixture(name: string) {
  return JSON.parse(readFileSync(new URL(`../api/fixtures/${name}.json`, import.meta.url), "utf8")).body;
}

const listed = { ok: true as const, value: parseSummaryPage(fixture("summaries-page")) };
const doc = parseDocumentPage(fixture("documents-page")).docs[0]!;
const BRIEF = "8b9267e2-7aa6-409a-9e54-84740ab28388" as SummaryId;
const SECOND_SOURCE: SummaryCite = { kind: "summary", summary: BRIEF, index: 1 };

test("a summary's source marks no key fact, and a key fact marks no summary source", () => {
  const panes: (PaneRequest | null)[] = [
    { kind: "cite", cite: SECOND_SOURCE, page: null },
    { kind: "cite", cite: { kind: "fact", index: 1 }, page: 3 },
    { kind: "page", page: 1 },
    null,
  ];
  assert.deepEqual(panes.map((pane) => [citedFact(pane), citedSource(pane)]), [
    [null, SECOND_SOURCE],
    [1, null],
    [null, null],
    [null, null],
  ]);
});

test("a summary's source opens its page with the quote marked, and pages away without the mark", () => {
  assert.deepEqual(summarySourceView(listed, SECOND_SOURCE, null, doc), {
    kind: "page", doc, page: 3, citedPage: 3,
    mark: { quote: "The deductible is paid by the insured before the insurer pays any claim.", label: "Rewritten summary" },
  });
  assert.deepEqual(summarySourceView(listed, SECOND_SOURCE, 1, doc), { kind: "page", doc, page: 1, citedPage: 3, mark: null });
});

test("a summary's source waits for the list, and says why when the list failed or the summary or source is not there", () => {
  const other = { ...SECOND_SOURCE, summary: "9f0c6a52-36a3-4a8e-9a0b-4c6f2f0e1d11" as SummaryId };
  assert.deepEqual(
    [
      summarySourceView(null, SECOND_SOURCE, null, doc),
      summarySourceView({ ok: false, error: { kind: "offline" } }, SECOND_SOURCE, null, doc),
      summarySourceView(listed, other, null, doc),
      summarySourceView({ ok: true, value: { ...listed.value, complete: false } }, other, null, doc),
      summarySourceView(listed, { ...SECOND_SOURCE, index: 2 }, null, doc),
    ],
    [
      { kind: "resolving" },
      { kind: "unknown", message: "Can't reach the vault. Check that the web server and the API are running." },
      { kind: "unknown", message: "This document has no rewritten summary with that link, so there is no passage to open." },
      { kind: "unknown", message: "This rewritten summary is not among the document's newest 100, so its passage cannot be opened here." },
      { kind: "unknown", message: "This rewritten summary has no source with that number, so there is no passage to open." },
    ],
  );
});
