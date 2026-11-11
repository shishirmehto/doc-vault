import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { locateQuote } from "./quote.ts";

// The first three cases are the backend's tests/test_pages.py, offsets included.
test("case, whitespace runs, typographic quotes and dashes are ignored (test_pages.py)", () => {
  const source = 'The "Each  Occurrence\nLimit" is $1,000,000 - per the Declarations.';
  assert.deepEqual(locateQuote(source, "the “each occurrence limit” is $1,000,000 – per"), { kind: "exact", start: 0, end: 48 });
  assert.deepEqual(locateQuote(source, "each occurrence limit is $2,000,000"), { kind: "missing" });
  assert.deepEqual(locateQuote(source, "  "), { kind: "missing" });
});

test("a key fact's collapsed quote finds its span across the page's line break (test_pages.py)", () => {
  const { body } = JSON.parse(readFileSync(new URL("../api/fixtures/page-pdf-2.json", import.meta.url), "utf8"));
  const match = locateQuote(body.text, "Each Occurrence Limit $1,000,000");
  assert.deepEqual(match, { kind: "exact", start: 106, end: 138 });
  assert.equal(body.text.slice(106, 138), "Each Occurrence Limit\n$1,000,000");
});

test("offsets count UTF-16 units, so text after an emoji is still marked exactly", () => {
  assert.deepEqual(locateQuote("\u{1F4C4} Limit $5,000", "limit $5,000"), { kind: "exact", start: 3, end: 15 });
  assert.deepEqual(locateQuote("Limit \u{1F4C4}", "limit \u{1F4C4}"), { kind: "exact", start: 0, end: 8 });
});

test("whitespace follows Python's isspace: U+001F separates words, U+FEFF does not", () => {
  assert.deepEqual(locateQuote("Each\u001fOccurrence", "each occurrence"), { kind: "exact", start: 0, end: 15 });
  assert.deepEqual(locateQuote("Each﻿Occurrence", "each occurrence"), { kind: "missing" });
});
