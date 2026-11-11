import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseHistory } from "../api/parse.ts";
import type { AskProgress, Turn } from "../api/domain.ts";
import { canAsk, offered, started, step, type Thread, type ThreadEvent } from "./thread.ts";

const captured = JSON.parse(readFileSync(new URL("../api/fixtures/history-policy-four-turns.json", import.meta.url), "utf8"));
const [deductible, perOccurrence, worldCup, exclusions] = parseHistory(captured.body) as [Turn, Turn, Turn, Turn];

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

const progress = (...events: AskProgress[]): ThreadEvent[] => events.map((p) => ({ type: "progress", progress: p }));

test("a streaming answer shows its step and text, a reset clears the text, and the turn replaces it", () => {
  const streaming = run([
    { type: "sent", question: deductible.question },
    ...progress({ kind: "stage", stage: "searching" }, { kind: "stage", stage: "answering" }, { kind: "text", text: "Property" }),
    ...progress({ kind: "reset" }, { kind: "text", text: "Property Damage" }, { kind: "text", text: " Deductible" }),
  ]);
  assert.deepEqual([streaming.ask, canAsk(streaming)], [
    { phase: "streaming", question: "What is the property damage deductible?", stage: "answering", text: "Property Damage Deductible", quiet: false },
    false,
  ]);
  const writing = (t: Thread) => (t.ask.phase === "streaming" ? { text: t.ask.text, quiet: t.ask.quiet } : t.ask.phase);
  const quiet = step(streaming, { type: "quiet" });
  assert.deepEqual(writing(quiet), { text: "Property Damage Deductible", quiet: true });
  assert.deepEqual(writing(run(progress({ kind: "text", text: " $2,500" }), quiet)), { text: "Property Damage Deductible $2,500", quiet: false });
  assert.deepEqual(seen(step(quiet, { type: "answered", turn: deductible })), { ask: "idle", turns: ["What is the property damage deductible?"] });
});

test("quiet before any text keeps the step as it is, and progress after the end is ignored", () => {
  const searching = run([{ type: "sent", question: deductible.question }, ...progress({ kind: "stage", stage: "searching" })]);
  assert.deepEqual(step(searching, { type: "quiet" }), searching);
  const done = step(searching, { type: "answered", turn: deductible });
  assert.deepEqual(run(progress({ kind: "text", text: "late" }), done), done);
});

test("a stream that breaks mid-answer drops its text and checks history like any lost answer", () => {
  const t = run([{ type: "sent", question: worldCup.question }, ...progress({ kind: "stage", stage: "searching" }), lost]);
  assert.deepEqual(t.ask, { phase: "checking", question: "Who won the 2022 football World Cup?", error: { kind: "offline" } });
});

test("only the latest turn's suggestions are offered, and only while nothing is in flight or failed", () => {
  const two = run([{ type: "answered", turn: deductible }, { type: "answered", turn: exclusions }]);
  assert.deepEqual(offered(two), ["What is the Estimated Annual Payroll?", "What is the Rate per $1,000 of Payroll?", "What is the Policy Number?"]);
  const asking = step(two, { type: "sent", question: perOccurrence.question });
  assert.deepEqual(offered(asking), []);
  assert.deepEqual(offered(run([lost, { type: "history", result: { ok: true, value: [deductible, exclusions] } }], asking)), []);
  assert.deepEqual(offered(started), []);
});

test("a broken stream keeps checking history until its turn lands, and offers a retry only on the final read", () => {
  const checking = run([
    { type: "answered", turn: deductible },
    { type: "sent", question: perOccurrence.question },
    ...progress({ kind: "stage", stage: "answering" }, { kind: "text", text: "Property" }),
    lost,
  ]);
  const early = (value: readonly Turn[]): ThreadEvent => ({ type: "history-early", result: { ok: true, value } });
  const notYet = run([early([deductible]), { type: "history-early", result: { ok: false, error: { kind: "offline" } } }], checking);
  assert.deepEqual([notYet.ask.phase, canAsk(notYet)], ["checking", false]);
  assert.deepEqual(seen(step(notYet, early([deductible, perOccurrence]))), {
    ask: "idle",
    turns: ["What is the property damage deductible?", "Does it apply per occurrence?"],
  });
  assert.equal(step(notYet, { type: "history", result: { ok: true, value: [deductible] } }).ask.phase, "failed");
});
