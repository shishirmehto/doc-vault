import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseError, parseSummary, parseSummaryPage } from "../api/parse.ts";
import type { Result, SummaryId, SummaryList } from "../api/domain.ts";
import { worthRetrying } from "./compare.ts";
import { idle, mergeSummary, normaliseFocus, paneList, shownSummary, step } from "./summary.ts";

function captured(name: string) {
  return JSON.parse(readFileSync(new URL(`../api/fixtures/${name}.json`, import.meta.url), "utf8"));
}

function fixture(name: string) {
  return captured(name).body;
}

const page = parseSummaryPage(fixture("summaries-page")).summaries;
const cached = parseSummary(fixture("summary-200-cached"));
const BRIEF = "8b9267e2-7aa6-409a-9e54-84740ab28388" as SummaryId;
const DETAILED = "b083fceb-16ca-4457-9d7d-e1809d72c2b3" as SummaryId;
const STANDARD = "03172f80-a399-4fe4-bb8d-e8b8b07d5a50" as SummaryId;
const BRIEF_CLIENT = { length: "brief", focus: "property damage deductibles", tone: "client" } as const;

test("a focus is trimmed, lowercased and single-spaced as the backend stores it; a blank one is none", () => {
  assert.deepEqual(
    ["  Property   DAMAGE\tDeductibles ", "deductibles", " \n\t ", ""].map(normaliseFocus),
    ["property damage deductibles", "deductibles", null, null],
  );
});

test("a run starts once, lands back at idle, and keeps its options when it fails until it is reset", () => {
  const running = step(idle, { type: "start", options: BRIEF_CLIENT, at: 1000 });
  assert.deepEqual(running, { kind: "running", options: BRIEF_CLIENT, startedAt: 1000 });
  assert.equal(step(running, { type: "start", options: { length: "detailed", focus: null, tone: "plain" }, at: 2000 }), running);
  assert.deepEqual(step(running, { type: "succeeded" }), { kind: "idle" });

  const failed = step(running, { type: "failed", error: { kind: "offline" } });
  assert.deepEqual(failed, { kind: "failed", options: BRIEF_CLIENT, error: { kind: "offline" } });
  assert.deepEqual(step(failed, { type: "start", options: BRIEF_CLIENT, at: 3000 }), { kind: "running", options: BRIEF_CLIENT, startedAt: 3000 });
  assert.deepEqual(step(failed, { type: "reset" }), { kind: "idle" });

  assert.deepEqual(
    [step(idle, { type: "succeeded" }), step(idle, { type: "failed", error: { kind: "offline" } }), step(running, { type: "reset" })],
    [{ kind: "idle" }, { kind: "idle" }, running],
  );
});

test("a summary joins the list at its head, and one already listed moves there instead of repeating", () => {
  assert.deepEqual(page.map((s) => s.id), [STANDARD, DETAILED, BRIEF]);
  assert.deepEqual(mergeSummary(page, cached).map((s) => [s.id, s.cached]), [[BRIEF, true], [STANDARD, true], [DETAILED, true]]);
  const fresh = { ...parseSummary(fixture("summary-201-brief-focus-client")), id: "9f0c6a52-36a3-4a8e-9a0b-4c6f2f0e1d11" as SummaryId };
  assert.deepEqual(mergeSummary(page, fresh).map((s) => s.id), [fresh.id, STANDARD, DETAILED, BRIEF]);
  assert.deepEqual(mergeSummary([], cached).map((s) => s.id), [BRIEF]);
});

test("the disclosure shows the summary the URL cites, else the chosen one, else none", () => {
  const cite = { kind: "summary", summary: BRIEF, index: 1 } as const;
  assert.equal(shownSummary(page, DETAILED, cite)?.id, BRIEF);
  assert.equal(shownSummary(page, DETAILED, null)?.id, DETAILED);
  assert.equal(shownSummary(page, DETAILED, { ...cite, summary: "9f0c6a52-36a3-4a8e-9a0b-4c6f2f0e1d11" as SummaryId }), null);
  assert.equal(shownSummary(page, null, null), null);
});

test("the source pane resolves a cite of a summary made this session while the earlier rewrites load or after they fail", () => {
  const cite = { kind: "summary", summary: BRIEF, index: 0 } as const;
  const failed: Result<SummaryList> = { ok: false, error: { kind: "offline" } };
  const listed: Result<SummaryList> = { ok: true, value: { summaries: page, complete: true } };
  assert.deepEqual(paneList(null, [cached], cite), { ok: true, value: { summaries: [cached], complete: false } });
  assert.deepEqual(paneList(failed, [cached], cite), { ok: true, value: { summaries: [cached], complete: false } });
  assert.deepEqual(paneList(failed, [cached], { ...cite, summary: DETAILED }), { ok: false, error: { kind: "offline" } });
  assert.deepEqual(paneList(failed, [], cite), { ok: false, error: { kind: "offline" } });
  assert.equal(paneList(null, [cached], null), null);
  assert.equal(paneList(listed, [cached], cite), listed);
});

test("a failed rewrite offers Try again for a refusal that can pass on a second try, not for a missing document or bad options", () => {
  const error = (name: string) => {
    const { status, body, headers } = captured(name);
    return parseError(status, body, headers["retry-after"] ?? null);
  };
  const badOutput = parseError(502, { error: { code: "ai_bad_output", message: "The model returned an empty summary.", retryable: true, details: {} } }, null);
  assert.deepEqual(
    [
      error("summaries-404-not-found"),
      error("summary-422-length"),
      error("summary-409-not-ready"),
      error("upload-429-rate-limited"),
      error("constructed/error-503-ai-unavailable"),
      badOutput,
      parseError(502, null, null),
      { kind: "offline" } as const,
    ].map((e) => worthRetrying(e)),
    [false, false, true, true, true, true, true, true],
  );
});

test("a rewrite refused by the spending cap offers Try again only once the cap has reset", () => {
  const { status, body, headers } = captured("summary-429-quota-exceeded");
  const capped = parseError(status, body, headers["retry-after"], headers["x-request-id"]);
  assert.deepEqual(
    [worthRetrying(capped, new Date("2026-11-12T18:29:59Z")), worthRetrying(capped, new Date("2026-11-13T00:00:00Z"))],
    [false, true],
  );
});
