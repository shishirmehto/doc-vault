import { test } from "node:test";
import assert from "node:assert/strict";
import { recalled, type Caret } from "./recall.ts";

const DEDUCTIBLE = "What is the property damage deductible?";
const PER_OCCURRENCE = "Does it apply per occurrence?";
const EXCLUSIONS = "Which exclusions apply?";
const ASKED = [DEDUCTIBLE, PER_OCCURRENCE, EXCLUSIONS];

const at = (offset: number): Caret => ({ start: offset, end: offset });
const atEnd = (value: string): Caret => at(value.length);

test("Up in an empty box takes the newest question, and Down there stays a cursor key", () => {
  assert.equal(recalled(ASKED, "", "older", at(0)), "Which exclusions apply?");
  assert.equal(recalled(ASKED, "", "newer", at(0)), null);
});

test("Up walks back through the questions, from anywhere on a one-line question, and stops at the oldest", () => {
  assert.equal(recalled(ASKED, EXCLUSIONS, "older", atEnd(EXCLUSIONS)), "Does it apply per occurrence?");
  assert.equal(recalled(ASKED, PER_OCCURRENCE, "older", at(4)), "What is the property damage deductible?");
  assert.equal(recalled(ASKED, DEDUCTIBLE, "older", atEnd(DEDUCTIBLE)), null);
});

test("Down walks forward through the questions and empties the box past the newest", () => {
  assert.equal(recalled(ASKED, DEDUCTIBLE, "newer", atEnd(DEDUCTIBLE)), "Does it apply per occurrence?");
  assert.equal(recalled(ASKED, PER_OCCURRENCE, "newer", at(0)), "Which exclusions apply?");
  assert.equal(recalled(ASKED, EXCLUSIONS, "newer", atEnd(EXCLUSIONS)), "");
});

test("a typed draft is left alone by both keys, and so is a recalled question once it is edited", () => {
  const draft = "What is the policy number?";
  assert.equal(recalled(ASKED, draft, "older", at(0)), null);
  assert.equal(recalled(ASKED, draft, "newer", atEnd(draft)), null);
  const edited = `${PER_OCCURRENCE} And per claim?`;
  assert.equal(recalled(ASKED, edited, "older", at(0)), null);
  assert.equal(recalled(ASKED, edited, "newer", atEnd(edited)), null);
  assert.equal(recalled(ASKED, " ", "older", at(0)), null);
  assert.equal(recalled(ASKED, PER_OCCURRENCE, "older", at(0)), "What is the property damage deductible?");
});

test("in a recalled question of several lines the keys move the cursor until it is on the first or the last line", () => {
  const lines = "Compare the two policies:\ntheir deductibles\ntheir exclusions";
  const asked = [DEDUCTIBLE, lines, EXCLUSIONS];
  const second = lines.indexOf("their deductibles");
  assert.equal(recalled(asked, lines, "older", atEnd(lines)), null);
  assert.equal(recalled(asked, lines, "older", at(second)), null);
  assert.equal(recalled(asked, lines, "older", at(second - 1)), "What is the property damage deductible?");
  assert.equal(recalled(asked, lines, "newer", at(0)), null);
  assert.equal(recalled(asked, lines, "newer", at(second)), null);
  assert.equal(recalled(asked, lines, "newer", at(lines.indexOf("their exclusions"))), "Which exclusions apply?");
});

test("a selection is left to the textarea", () => {
  assert.equal(recalled(ASKED, EXCLUSIONS, "older", { start: 0, end: 5 }), null);
  assert.equal(recalled(ASKED, EXCLUSIONS, "newer", { start: 0, end: EXCLUSIONS.length }), null);
  assert.equal(recalled(ASKED, EXCLUSIONS, "older", at(5)), "Does it apply per occurrence?");
});

test("a chat with no questions recalls nothing", () => {
  assert.equal(recalled([], "", "older", at(0)), null);
  assert.equal(recalled([], "", "newer", at(0)), null);
  assert.equal(recalled([DEDUCTIBLE], "", "older", at(0)), "What is the property damage deductible?");
});
