import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseDoc } from "../api/parse.ts";
import { isSettled, stageProgress } from "./lifecycle.ts";

const doc = (name: string) =>
  parseDoc(JSON.parse(readFileSync(new URL(`../api/fixtures/${name}.json`, import.meta.url), "utf8")).body);

const queued = doc("document-txt-queued-insights-pending");
const parsing = doc("document-txt-parsing-insights-pending");
const embedding = doc("document-txt-embedding-insights-pending");
const readyPending = doc("document-txt-ready-insights-pending");
const ready = doc("document-txt-ready-insights-ready");
const failed = doc("document-pdf-failed-insights-unavailable");

test("polling stops only when nothing is processing and no summary is being written", () => {
  assert.equal(isSettled([ready, failed]), true);
  assert.equal(isSettled([]), true);
  assert.equal(isSettled([ready, readyPending]), false);
  assert.equal(isSettled([failed, embedding]), false);
  assert.equal(isSettled([queued]), false);
  assert.equal(isSettled([parsing]), false);
});

test("the stage bar fills parse, then embed by chunk count, then ready", () => {
  assert.deepEqual(stageProgress(queued.lifecycle), [{ kind: "todo" }, { kind: "todo" }, { kind: "todo" }]);
  assert.deepEqual(stageProgress(parsing.lifecycle), [{ kind: "active", fraction: null }, { kind: "todo" }, { kind: "todo" }]);
  assert.deepEqual(stageProgress(embedding.lifecycle), [{ kind: "done" }, { kind: "active", fraction: 0 }, { kind: "todo" }]);
  assert.deepEqual(stageProgress({ status: "embedding", embedded: 6, total: 24 }),
    [{ kind: "done" }, { kind: "active", fraction: 0.25 }, { kind: "todo" }]);
  assert.deepEqual(stageProgress(ready.lifecycle), [{ kind: "done" }, { kind: "done" }, { kind: "done" }]);
});

test("a failure marks the stage that failed", () => {
  assert.deepEqual(stageProgress(failed.lifecycle), [{ kind: "failed" }, { kind: "todo" }, { kind: "todo" }]);
  const failure = { code: "internal_error", message: "x", retryable: true };
  assert.deepEqual(stageProgress({ status: "failed", stage: "embed", failure }), [{ kind: "done" }, { kind: "failed" }, { kind: "todo" }]);
});
