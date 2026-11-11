import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseDocumentPage } from "../api/parse.ts";
import { chooseAll, findDocs } from "./find.ts";

const page = JSON.parse(readFileSync(new URL("../api/fixtures/documents-page.json", import.meta.url), "utf8"));
const docs = parseDocumentPage(page.body).docs;
const names = (query: string) => findDocs(docs, query).map((d) => d.filename);

test("find matches every word against the filename and the document type, ignoring case", () => {
  assert.deepEqual(names("policy"), ["sample-cgl-policy.pdf"]);
  assert.deepEqual(names("QUOTE"), ["sample-renewal-quote.pdf"]);
  assert.deepEqual(names("memo 2"), ["ui-renewal-memo-2.txt"]);
  assert.deepEqual(names("endorsement docx"), ["sample-endorsement-additional-insured.docx"]);
  assert.deepEqual(names("nothing-like-this"), []);
  assert.equal(names("  ").length, 11);
});

test("select all ready chooses the shown ids up to the cap, and a second press clears them", () => {
  const some = chooseAll(new Set(["x"]), ["a", "b", "c"], 50);
  assert.deepEqual([...some.chosen], ["x", "a", "b", "c"]);
  assert.equal(some.capped, false);
  assert.deepEqual([...chooseAll(some.chosen, ["a", "b", "c"], 50).chosen], ["x"]);
  const capped = chooseAll(new Set(["x"]), ["a", "b", "c"], 3);
  assert.deepEqual([...capped.chosen], ["x", "a", "b"]);
  assert.equal(capped.capped, true);
});
