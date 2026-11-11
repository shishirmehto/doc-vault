import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseDocumentPage } from "./api/parse.ts";
import { docMeta } from "./copy.ts";

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
