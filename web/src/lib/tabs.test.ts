import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseHistory } from "../api/parse.ts";
import type { ChatId, Turn } from "../api/domain.ts";
import { asAskId, heard, told } from "./tabs.ts";
import { started, step } from "./thread.ts";

const captured = JSON.parse(readFileSync(new URL("../api/fixtures/history-policy-four-turns.json", import.meta.url), "utf8"));
const [deductible, perOccurrence] = parseHistory(captured.body) as [Turn, Turn, Turn, Turn];

const CHAT = "579aa94e-85de-458e-b749-cb774b9d6ca7" as ChatId;
const FROM = asAskId("a1");

test("a hello and a snapshot are heard as they were posted", () => {
  assert.deepEqual(heard(structuredClone({ chat: CHAT, hello: true })), { chat: "579aa94e-85de-458e-b749-cb774b9d6ca7", hello: true });
  const snapshot = { chat: CHAT, from: FROM, ask: { phase: "asking", question: "Does it apply per occurrence?" }, turns: [] };
  assert.deepEqual(heard(structuredClone(snapshot)), {
    chat: "579aa94e-85de-458e-b749-cb774b9d6ca7", from: "a1", ask: { phase: "asking", question: "Does it apply per occurrence?" }, turns: [],
  });
});

test("anything else on the channel is not heard: junk, a message with no chat, an unknown shape", () => {
  const ask = { phase: "idle" };
  const unheard: unknown[] = [
    "hello", 7, null, undefined, [], {},
    { hello: true },
    { from: "a1", ask, turns: [] },
    { chat: "not-a-chat-id", hello: true },
    { chat: CHAT },
    { chat: CHAT, hello: false },
    { chat: CHAT, from: 7, ask, turns: [] },
    { chat: CHAT, from: "a1", turns: [] },
    { chat: CHAT, from: "a1", ask: "idle", turns: [] },
    { chat: CHAT, from: "a1", ask: { phase: "paused" }, turns: [] },
    { chat: CHAT, from: "a1", ask: { phase: "toString" }, turns: [] },
    { chat: CHAT, from: "a1", ask },
    { chat: CHAT, from: "a1", ask, turns: "none" },
  ];
  assert.deepEqual(unheard.map(heard), unheard.map(() => null));
  assert.deepEqual(heard({ chat: CHAT, from: "a1", ask, turns: [] }), { chat: CHAT, from: "a1", ask: { phase: "idle" }, turns: [] });
});

test("a step tells the other tabs the ask as it stands and only the turns it added, and nothing when the ask is as it was", () => {
  const one = step(started, { type: "answered", turn: deductible });
  const asking = step(one, { type: "sent", question: perOccurrence.question });
  assert.deepEqual(told(CHAT, FROM, one, asking), {
    chat: "579aa94e-85de-458e-b749-cb774b9d6ca7", from: "a1", ask: { phase: "asking", question: "Does it apply per occurrence?" }, turns: [],
  });
  assert.equal(told(CHAT, FROM, asking, step(asking, { type: "history", result: { ok: true, value: [deductible] } })), null);
  assert.equal(told(CHAT, FROM, asking, step(asking, { type: "quiet" })), null);
  const done = step(asking, { type: "answered", turn: perOccurrence });
  const last = told(CHAT, FROM, asking, done);
  assert.deepEqual(last && "turns" in last && [last.ask, last.turns.map((turn) => turn.question)], [{ phase: "idle" }, ["Does it apply per occurrence?"]]);
});
