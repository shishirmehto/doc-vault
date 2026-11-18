import { test } from "node:test";
import assert from "node:assert/strict";
import remarkParse from "remark-parse";
import remarkRehype from "remark-rehype";
import { unified } from "unified";
import { remarkCitations } from "./citations.ts";

function tree(markdown: string, rehype = false): unknown {
  const processor = rehype
    ? unified().use(remarkParse).use(remarkCitations).use(remarkRehype)
    : unified().use(remarkParse).use(remarkCitations);
  const out = processor.runSync(processor.parse(markdown));
  return JSON.parse(JSON.stringify(out, (key, value) => (key === "position" ? undefined : value)));
}

const chip = (ref: number) => ({ type: "cite", data: { hName: "button", hProperties: { dataRef: ref } } });
const text = (value: string) => ({ type: "text", value });
const item = (...children: unknown[]) => ({
  type: "listItem", spread: false, checked: null, children: [{ type: "paragraph", children }],
});

test("each list item ends in its chip, on the word or bold figure before it", () => {
  assert.deepEqual(tree("The quote changes two terms:\n\n- The aggregate is **$3,000,000** [1]\n- The deductible is $5,000 [2]"), {
    type: "root",
    children: [
      { type: "paragraph", children: [text("The quote changes two terms:")] },
      {
        type: "list", ordered: false, start: null, spread: false,
        children: [
          item(text("The aggregate is "), { type: "strong", children: [text("$3,000,000")] }, chip(1)),
          item(text("The deductible is $5,000"), chip(2)),
        ],
      },
    ],
  });
});

test("a marker inside bold becomes a chip inside the bold", () => {
  assert.deepEqual(tree("**$1,000,000 [3]** each occurrence"), {
    type: "root",
    children: [{
      type: "paragraph",
      children: [{ type: "strong", children: [text("$1,000,000"), chip(3)] }, text(" each occurrence")],
    }],
  });
});

test("adjacent markers become adjacent chips", () => {
  assert.deepEqual(tree("Both set it at $1,000,000 [1][2]. Neither lowers it."), {
    type: "root",
    children: [{ type: "paragraph", children: [text("Both set it at $1,000,000"), chip(1), chip(2), text(". Neither lowers it.")] }],
  });
});

test("text with no markers is left as it was parsed", () => {
  assert.deepEqual(tree("The documents do not mention flood cover."), {
    type: "root",
    children: [{ type: "paragraph", children: [text("The documents do not mention flood cover.")] }],
  });
});

test("a marker of two digits is one chip", () => {
  assert.deepEqual(tree("See the schedule [12]."), {
    type: "root",
    children: [{ type: "paragraph", children: [text("See the schedule"), chip(12), text(".")] }],
  });
});

test("remark-rehype turns a chip into a button that carries its number", () => {
  assert.deepEqual(tree("Limit **$1,000,000** [1].", true), {
    type: "root",
    children: [{
      type: "element", tagName: "p", properties: {},
      children: [
        text("Limit "),
        { type: "element", tagName: "strong", properties: {}, children: [text("$1,000,000")] },
        { type: "element", tagName: "button", properties: { dataRef: 1 }, children: [] },
        text("."),
      ],
    }],
  });
});

test("a marker at the very start of a paragraph is a chip", () => {
  assert.deepEqual(tree("[1] The policy period is one year."), {
    type: "root",
    children: [{ type: "paragraph", children: [chip(1), text(" The policy period is one year.")] }],
  });
});
