import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseComparison, parseDocumentPage } from "../api/parse.ts";
import type { DocumentId } from "../api/domain.ts";
import { cellView } from "./comparisonPane.ts";

function fixture(name: string) {
  return JSON.parse(readFileSync(new URL(`../api/fixtures/${name}.json`, import.meta.url), "utf8")).body;
}

const RENEWAL = "23b7e49c-b644-4ccf-8768-5716b69cb9cc" as DocumentId;
const DEDUCTIBLE_IN_RENEWAL = { cite: { row: 9, col: 1 }, page: null };

test("a cell's pane says why when the comparison could not be read, and waits while it loads", () => {
  assert.deepEqual(
    [
      cellView({ ok: false, error: { kind: "offline" } }, DEDUCTIBLE_IN_RENEWAL, null, false),
      cellView({ ok: false, error: { kind: "broken", status: 502, requestId: null } }, DEDUCTIBLE_IN_RENEWAL, null, false),
      cellView(null, DEDUCTIBLE_IN_RENEWAL, null, true),
    ],
    [
      { kind: "unknown", message: "Can't reach the vault. Check that the web server and the API are running." },
      { kind: "unknown", message: "The vault service couldn't handle this request (HTTP 502)." },
      { kind: "resolving" },
    ],
  );
});

test("a cell's pane opens its page with the quote marked, and pages away without the mark", () => {
  const comparison = { ok: true as const, value: parseComparison(fixture("compare-201-policy-quote")) };
  const renewal = parseDocumentPage(fixture("documents-page")).docs.find((d) => d.id === RENEWAL)!;
  assert.deepEqual(cellView(comparison, DEDUCTIBLE_IN_RENEWAL, renewal, false), {
    kind: "page", doc: renewal, page: 2, citedPage: 2,
    mark: { quote: "Property Damage Deductible $5,000 per occurrence", label: "Property Damage Deductible" },
  });
  assert.deepEqual(cellView(comparison, { ...DEDUCTIBLE_IN_RENEWAL, page: 3 }, renewal, false), {
    kind: "page", doc: renewal, page: 3, citedPage: 2, mark: null,
  });
});
