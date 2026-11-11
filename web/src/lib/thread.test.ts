import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseHistory } from "../api/parse.ts";
import type { Turn } from "../api/domain.ts";
import { canAsk, started, step, type Thread, type ThreadEvent } from "./thread.ts";

const captured = JSON.parse(readFileSync(new URL("../api/fixtures/history-policy-four-turns.json", import.meta.url), "utf8"));
const [deductible, perOccurrence, worldCup] = parseHistory(captured.body) as [Turn, Turn, Turn, Turn];

function run(events: ThreadEvent[], from: Thread = started): Thread {
  return events.reduce(step, from);
}

function seen(t: Thread) {
  return { ask: t.ask.phase, turns: t.history.kind === "loaded" ? t.history.turns.map((turn) => turn.question) : t.history.kind };
}

const lost: ThreadEvent = { type: "ask-failed", error: { kind: "offline" } };

test("an answer is appended and the thread is free for the next question", () => {
  const t = run([{ type: "sent", question: deductible.question }, { type: "answered", turn: deductible }]);
  assert.deepEqual(seen(t), { ask: "idle", turns: ["What is the property damage deductible?"] });
});

test("a failed ask whose turn reached the backend is adopted from history, with no retry offered", () => {
  const t = run([
    { type: "answered", turn: deductible },
    { type: "sent", question: perOccurrence.question },
    lost,
    { type: "history", result: { ok: true, value: [deductible, perOccurrence] } },
  ]);
  assert.deepEqual(seen(t), {
    ask: "idle",
    turns: ["What is the property damage deductible?", "Does it apply per occurrence?"],
  });
});

test("retry is offered only after history shows the question was not recorded", () => {
  const checking = run([{ type: "answered", turn: deductible }, { type: "sent", question: worldCup.question }, lost]);
  assert.deepEqual([seen(checking).ask, canAsk(checking)], ["checking", false]);
  const failed = step(checking, { type: "history", result: { ok: true, value: [deductible] } });
  assert.deepEqual([failed.ask, canAsk(failed)], [
    { phase: "failed", question: "Who won the 2022 football World Cup?", error: { kind: "offline" } },
    true,
  ]);
});

test("an earlier turn with the same question does not count as the lost one", () => {
  const t = run([
    { type: "answered", turn: deductible },
    { type: "sent", question: deductible.question },
    lost,
    { type: "history", result: { ok: true, value: [deductible] } },
  ]);
  assert.deepEqual(seen(t), { ask: "failed", turns: ["What is the property damage deductible?"] });
});

test("when history cannot be read either, only a recheck is offered, never a second ask", () => {
  const unconfirmed = run([
    { type: "sent", question: worldCup.question },
    lost,
    { type: "history", result: { ok: false, error: { kind: "broken", status: 502 } } },
  ]);
  assert.deepEqual([seen(unconfirmed).ask, canAsk(unconfirmed)], ["unconfirmed", false]);
  assert.equal(step(unconfirmed, { type: "sent", question: worldCup.question }).ask.phase, "unconfirmed");
  const recorded = run([{ type: "recheck" }, { type: "history", result: { ok: true, value: [worldCup] } }], unconfirmed);
  assert.deepEqual(seen(recorded), { ask: "idle", turns: ["Who won the 2022 football World Cup?"] });
});

test("a second question cannot be sent while one is in flight", () => {
  const t = run([
    { type: "sent", question: deductible.question },
    { type: "sent", question: perOccurrence.question },
  ]);
  assert.deepEqual(t.ask, { phase: "asking", question: "What is the property damage deductible?" });
});
