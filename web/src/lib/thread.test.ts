import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseHistory } from "../api/parse.ts";
import type { AskProgress, ChatId, Turn } from "../api/domain.ts";
import { askStatus } from "../copy.ts";
import { asAskId, heard, told, type TabMessage } from "./tabs.ts";
import { asked, canAsk, offered, opening, started, step, type AskId, type Thread, type ThreadEvent } from "./thread.ts";

const captured = JSON.parse(readFileSync(new URL("../api/fixtures/history-policy-four-turns.json", import.meta.url), "utf8"));
const [deductible, perOccurrence, worldCup, exclusions] = parseHistory(captured.body) as [Turn, Turn, Turn, Turn];

function run(events: ThreadEvent[], from: Thread = started): Thread {
  return events.reduce(step, from);
}

function seen(t: Thread) {
  return { ask: t.ask.phase, turns: t.history.kind === "loaded" ? t.history.turns.map((turn) => turn.question) : t.history.kind };
}

/** No answer and no stream: the request or its response was lost before the server accepted the question. */
const lost: ThreadEvent = { type: "ask-failed", error: { kind: "offline" }, accepted: false };
/** The server accepted the question with a 200 and its stream broke before a turn or an error event. */
const broken: ThreadEvent = { type: "ask-failed", error: { kind: "offline" }, accepted: true };

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
    { type: "history", result: { ok: false, error: { kind: "broken", status: 502, requestId: null } } },
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

test("a stream that breaks mid-answer drops its text and checks history for a turn that may still land", () => {
  const t = run([{ type: "sent", question: worldCup.question }, ...progress({ kind: "stage", stage: "searching" }), broken]);
  assert.deepEqual(t.ask, {
    phase: "checking", question: "Who won the 2022 football World Cup?", error: { kind: "offline" }, mayLand: true,
  });
});

test("only the latest turn's suggestions are offered, and only while nothing is in flight or failed", () => {
  const two = run([{ type: "answered", turn: deductible }, { type: "answered", turn: exclusions }]);
  assert.deepEqual(offered(two), ["What is the Estimated Annual Payroll?", "What is the Rate per $1,000 of Payroll?", "What is the Policy Number?"]);
  const asking = step(two, { type: "sent", question: perOccurrence.question });
  assert.deepEqual(offered(asking), []);
  assert.deepEqual(offered(run([lost, { type: "history", result: { ok: true, value: [deductible, exclusions] } }], asking)), []);
  assert.deepEqual(offered(started), []);
});

test("a broken stream keeps checking history until its turn lands, and never offers a retry", () => {
  const checking = run([
    { type: "answered", turn: deductible },
    { type: "sent", question: perOccurrence.question },
    ...progress({ kind: "stage", stage: "answering" }, { kind: "text", text: "Property" }),
    broken,
  ]);
  const early = (value: readonly Turn[]): ThreadEvent => ({ type: "history-early", result: { ok: true, value } });
  const notYet = run([early([deductible]), { type: "history-early", result: { ok: false, error: { kind: "offline" } } }], checking);
  assert.deepEqual([notYet.ask.phase, canAsk(notYet)], ["checking", false]);
  assert.deepEqual(seen(step(notYet, early([deductible, perOccurrence]))), {
    ask: "idle",
    turns: ["What is the property damage deductible?", "Does it apply per occurrence?"],
  });
  const waiting = step(notYet, { type: "history", result: { ok: true, value: [deductible] } });
  assert.deepEqual([waiting.ask.phase, canAsk(waiting)], ["waiting", false]);
  assert.equal(run([{ type: "recheck" }, { type: "history", result: { ok: true, value: [deductible] } }], waiting).ask.phase, "waiting");
  assert.equal(step(waiting, { type: "dismissed" }), waiting);
  assert.deepEqual(seen(step(waiting, early([deductible, perOccurrence]))).ask, "idle");
  const unread = step(notYet, { type: "history", result: { ok: false, error: { kind: "offline" } } });
  assert.equal(run([{ type: "recheck" }, { type: "history", result: { ok: true, value: [deductible] } }], unread).ask.phase, "waiting");
});

test("a malformed event mid-stream is no verdict either, so its turn may still land", () => {
  const malformed: ThreadEvent = { type: "ask-failed", error: { kind: "unexpected", detail: "bad event" }, accepted: true };
  const t = run([{ type: "sent", question: worldCup.question }, ...progress({ kind: "stage", stage: "searching" }), malformed,
    { type: "history", result: { ok: true, value: [] } }]);
  assert.deepEqual([t.ask.phase, canAsk(t)], ["waiting", false]);
});

test("an error event is the server's verdict, so Try again is offered once history lacks the turn", () => {
  const refused: ThreadEvent = {
    type: "ask-failed", accepted: true,
    error: { kind: "refused", status: 200, code: "ai_unavailable", message: "", retryable: true, retryAt: null,
      documents: [], quota: null, requestId: null },
  };
  const t = run([{ type: "sent", question: worldCup.question }, refused, { type: "history", result: { ok: true, value: [] } }]);
  assert.deepEqual([t.ask.phase, canAsk(t)], ["failed", true]);
});

test("a follow-up's status line follows the server's steps, after a neutral line until the first one", () => {
  const states = [
    { type: "sent", question: perOccurrence.question },
    ...progress({ kind: "stage", stage: "condensing" }, { kind: "stage", stage: "searching" }, { kind: "stage", stage: "answering" }),
    ...progress({ kind: "text", text: "Yes" }),
    { type: "quiet" },
  ].reduce<Thread[]>((seen, e) => [...seen, step(seen.at(-1)!, e as ThreadEvent)], [run([{ type: "answered", turn: deductible }])]);
  const lines = states.slice(1).map((t) => (t.ask.phase === "asking" || t.ask.phase === "streaming" ? askStatus(t.ask, 2) : t.ask.phase));
  assert.deepEqual(lines, [
    "Sending the question",
    "Reading the follow-up against the conversation",
    "Searching 2 documents",
    "Writing the answer",
    "Writing the answer",
    "Checking citations",
  ]);
});

test("text before any step names the answering step, never an invented search", () => {
  const t = run([{ type: "sent", question: deductible.question }, ...progress({ kind: "text", text: "Each" })]);
  assert.equal(t.ask.phase === "streaming" && t.ask.stage, "answering");
  const sent = run([{ type: "sent", question: deductible.question }, ...progress({ kind: "reset" })]);
  assert.equal(sent.ask.phase === "streaming" && askStatus(sent.ask, 1), "Sending the question");
});

const CHAT = "579aa94e-85de-458e-b749-cb774b9d6ca7" as ChatId;
const A = asAskId("a1");
const B = asAskId("b1");

/** What a tab makes of another tab's message, sent as the channel sends it: a structured clone, read by `heard`. */
function deliver(to: Thread, message: TabMessage | null): Thread {
  const m = message && heard(structuredClone(message));
  return m && "from" in m ? step(to, { type: "relayed", from: m.from, ask: m.ask, turns: m.turns }) : to;
}

type Tabs = { asker: Thread; other: Thread };

function relay(tabs: Tabs, from: AskId, events: ThreadEvent[]): Tabs {
  return events.reduce(({ asker, other }, e) => {
    const after = step(asker, e);
    return { asker: after, other: deliver(other, told(CHAT, from, asker, after)) };
  }, tabs);
}

const oneTurn = run([{ type: "answered", turn: deductible }]);
const bothOpen: Tabs = { asker: oneTurn, other: oneTurn };
const read = (...value: Turn[]): ThreadEvent => ({ type: "history", result: { ok: true, value } });

test("another tab mirrors the question, its step and its text, then shows the turn once and no mirror", () => {
  const sent = relay(bothOpen, A, [{ type: "sent", question: perOccurrence.question }]);
  assert.deepEqual([sent.other.ask, sent.other.elsewhere], [
    { phase: "idle" },
    { from: "a1", ask: { phase: "asking", question: "Does it apply per occurrence?" } },
  ]);
  const streaming = relay(sent, A, progress({ kind: "stage", stage: "searching" }, { kind: "text", text: "Yes" }, { kind: "text", text: ", each" }));
  assert.deepEqual(streaming.other.elsewhere, {
    from: "a1", ask: { phase: "streaming", question: "Does it apply per occurrence?", stage: "answering", text: "Yes, each", quiet: false },
  });
  const done = relay(streaming, A, [{ type: "answered", turn: perOccurrence }]).other;
  const both = { ask: "idle", turns: ["What is the property damage deductible?", "Does it apply per occurrence?"] };
  assert.deepEqual([seen(done), done.elsewhere, canAsk(done)], [both, null, true]);
  assert.deepEqual(seen(step(done, read(deductible, perOccurrence))), both);
});

test("a tab that opens the chat mid-answer is told the question, its step and the text so far", () => {
  const { asker } = relay(bothOpen, A, [
    { type: "sent", question: perOccurrence.question }, ...progress({ kind: "stage", stage: "answering" }, { kind: "text", text: "Yes" }),
  ]);
  const joined = deliver(opening, { chat: CHAT, from: A, ask: asker.ask, turns: [] });
  assert.deepEqual([joined.history.kind, joined.elsewhere], [
    "loading",
    { from: "a1", ask: { phase: "streaming", question: "Does it apply per occurrence?", stage: "answering", text: "Yes", quiet: false } },
  ]);
  const loaded = step(joined, read(deductible));
  assert.deepEqual([seen(loaded), loaded.elsewhere?.ask.phase, canAsk(loaded)], [
    { ask: "idle", turns: ["What is the property damage deductible?"] }, "streaming", false,
  ]);
  const done = relay({ asker, other: loaded }, A, [{ type: "answered", turn: perOccurrence }]).other;
  assert.deepEqual([seen(done), done.elsewhere], [
    { ask: "idle", turns: ["What is the property damage deductible?", "Does it apply per occurrence?"] }, null,
  ]);
});

test("a turn relayed while history is still loading is left to the history read, which shows it once", () => {
  const { asker } = relay(bothOpen, A, [{ type: "sent", question: perOccurrence.question }]);
  const joined = deliver(opening, { chat: CHAT, from: A, ask: asker.ask, turns: [] });
  const done = relay({ asker, other: joined }, A, [{ type: "answered", turn: perOccurrence }]).other;
  assert.deepEqual([joined.elsewhere?.ask.phase, done.history.kind, done.elsewhere], ["asking", "loading", null]);
  assert.deepEqual(seen(run([read(deductible), read(deductible, perOccurrence)], done)), {
    ask: "idle", turns: ["What is the property damage deductible?", "Does it apply per occurrence?"],
  });
});

test("a history read that left before a relayed turn was stored keeps the turn", () => {
  const { other } = relay(bothOpen, A, [{ type: "sent", question: perOccurrence.question }, { type: "answered", turn: perOccurrence }]);
  assert.deepEqual(seen(step(other, read(deductible))).turns, ["What is the property damage deductible?", "Does it apply per occurrence?"]);
  const first = relay({ asker: started, other: started }, A, [{ type: "sent", question: deductible.question }, { type: "answered", turn: deductible }]);
  assert.deepEqual(seen(step(first.other, read())).turns, ["What is the property damage deductible?"]);
});

test("a history read drops the shown turns that fell out of its window and keeps the ones newer than it", () => {
  const three = run([{ type: "answered", turn: deductible }, { type: "answered", turn: perOccurrence }, { type: "answered", turn: worldCup }]);
  assert.deepEqual(seen(step(three, read(perOccurrence, worldCup, exclusions))).turns, [
    "Does it apply per occurrence?", "Who won the 2022 football World Cup?", "Exclusions?",
  ]);
  assert.deepEqual(seen(step(three, read(perOccurrence))).turns, ["Does it apply per occurrence?", "Who won the 2022 football World Cup?"]);
});

test("the stream's own turn is not shown twice when a history read brought it first", () => {
  const t = run([{ type: "sent", question: perOccurrence.question }, read(deductible, perOccurrence), { type: "answered", turn: perOccurrence }], oneTurn);
  assert.deepEqual(seen(t), { ask: "idle", turns: ["What is the property damage deductible?", "Does it apply per occurrence?"] });
});

test("while another tab's question is in flight this tab cannot ask, and no suggestion is offered", () => {
  const two = run([{ type: "answered", turn: deductible }, { type: "answered", turn: exclusions }]);
  assert.deepEqual([canAsk(two), offered(two).length], [true, 3]);
  const { other } = relay({ asker: two, other: two }, A, [{ type: "sent", question: perOccurrence.question }]);
  assert.deepEqual([canAsk(other), offered(other)], [false, []]);
  assert.deepEqual(step(other, { type: "sent", question: worldCup.question }).ask, { phase: "idle" });
});

test("a question another tab asks dismisses this tab's own failed ask", () => {
  const failed = run([{ type: "sent", question: worldCup.question }, lost, read(deductible)], oneTurn);
  assert.deepEqual([failed.ask.phase, canAsk(failed)], ["failed", true]);
  const { other } = relay({ asker: oneTurn, other: failed }, A, [{ type: "sent", question: perOccurrence.question }]);
  assert.deepEqual([other.ask, other.elsewhere?.ask, canAsk(other)], [
    { phase: "idle" }, { phase: "asking", question: "Does it apply per occurrence?" }, false,
  ]);
});

test("the asking tab's failed request is mirrored while it checks, then the mirror clears and Try again stays with the asking tab", () => {
  const checking = relay(bothOpen, A, [{ type: "sent", question: worldCup.question }, lost]);
  assert.deepEqual([checking.other.elsewhere?.ask, canAsk(checking.other)], [
    { phase: "checking", question: "Who won the 2022 football World Cup?", error: { kind: "offline" }, mayLand: false }, false,
  ]);
  const { asker, other } = relay(checking, A, [read(deductible)]);
  assert.deepEqual([asker.ask.phase, canAsk(asker)], ["failed", true]);
  assert.deepEqual([other.ask, other.elsewhere, canAsk(other)], [{ phase: "idle" }, null, true]);
});

test("when the asking tab goes mid-answer, the other tab checks history for the question and adopts its turn", () => {
  const { other } = relay(bothOpen, A, [{ type: "sent", question: perOccurrence.question }, ...progress({ kind: "text", text: "Yes" })]);
  assert.equal(step(other, { type: "orphaned", from: B }), other);
  const orphaned = step(other, { type: "orphaned", from: A });
  assert.deepEqual([orphaned.ask, orphaned.elsewhere, canAsk(orphaned)], [
    { phase: "checking", question: "Does it apply per occurrence?", error: { kind: "offline" }, mayLand: true }, null, false,
  ]);
  const notYet = step(orphaned, { type: "history-early", result: { ok: true, value: [deductible] } });
  assert.equal(notYet.ask.phase, "checking");
  assert.deepEqual(seen(step(notYet, { type: "history-early", result: { ok: true, value: [deductible, perOccurrence] } })), {
    ask: "idle", turns: ["What is the property damage deductible?", "Does it apply per occurrence?"],
  });
});

test("a question taken over from a tab that is gone keeps what that tab knew of it", () => {
  const takenOver = (events: ThreadEvent[]) => step(relay(bothOpen, A, events).other, { type: "orphaned", from: A }).ask;
  const sent: ThreadEvent = { type: "sent", question: worldCup.question };
  const refused: ThreadEvent = { type: "ask-failed", error: { kind: "broken", status: 502, requestId: null }, accepted: false };
  const unread: ThreadEvent = { type: "history", result: { ok: false, error: { kind: "offline" } } };
  const question = "Who won the 2022 football World Cup?";
  assert.deepEqual(takenOver([sent]), { phase: "checking", question, error: { kind: "offline" }, mayLand: true });
  assert.deepEqual(takenOver([sent, refused]), {
    phase: "checking", question, error: { kind: "broken", status: 502, requestId: null }, mayLand: false,
  });
  assert.deepEqual(takenOver([sent, refused, unread]), {
    phase: "checking", question, error: { kind: "broken", status: 502, requestId: null }, mayLand: false,
  });
  assert.deepEqual(takenOver([sent, broken, unread]), { phase: "checking", question, error: { kind: "offline" }, mayLand: true });
  assert.deepEqual(takenOver([sent, broken, read(deductible)]), { phase: "checking", question, error: { kind: "offline" }, mayLand: true });
});

test("a question that was never recorded can be asked again from the tab that took it over", () => {
  const refused: ThreadEvent = { type: "ask-failed", error: { kind: "broken", status: 502, requestId: null }, accepted: false };
  const { other } = relay(bothOpen, A, [{ type: "sent", question: worldCup.question }, refused]);
  const settled = run([{ type: "orphaned", from: A }, read(deductible)], other);
  assert.deepEqual([settled.ask, canAsk(settled)], [
    { phase: "failed", question: "Who won the 2022 football World Cup?", error: { kind: "broken", status: 502, requestId: null } }, true,
  ]);
});

test("a tab that is gone after its question ended changes nothing", () => {
  const asking = relay(bothOpen, A, [{ type: "sent", question: perOccurrence.question }]);
  assert.equal(step(asking.other, { type: "orphaned", from: A }).ask.phase, "checking");
  const { other } = relay(asking, A, [{ type: "answered", turn: perOccurrence }]);
  assert.equal(step(other, { type: "orphaned", from: A }), other);
  assert.equal(deliver(other, { chat: CHAT, from: A, ask: { phase: "idle" }, turns: [perOccurrence] }), other);
});

test("two tabs that ask in the same instant both send, each mirrors the other, and each ends with both turns", () => {
  const a = step(oneTurn, { type: "sent", question: perOccurrence.question });
  const b = step(oneTurn, { type: "sent", question: exclusions.question });
  const fromA = told(CHAT, A, oneTurn, a);
  const fromB = told(CHAT, B, oneTurn, b);
  const [a1, b1, c1] = [deliver(a, fromB), deliver(b, fromA), deliver(deliver(oneTurn, fromA), fromB)];
  assert.deepEqual([a1.ask, a1.elsewhere], [
    { phase: "asking", question: "Does it apply per occurrence?" }, { from: "b1", ask: { phase: "asking", question: "Exclusions?" } },
  ]);
  assert.deepEqual([b1.ask, b1.elsewhere], [
    { phase: "asking", question: "Exclusions?" }, { from: "a1", ask: { phase: "asking", question: "Does it apply per occurrence?" } },
  ]);

  const a2 = step(a1, { type: "answered", turn: perOccurrence });
  const aDone = told(CHAT, A, a1, a2);
  const [b2, c2] = [deliver(b1, aDone), deliver(c1, aDone)];
  assert.deepEqual([a2.ask.phase, a2.elsewhere?.from, b2.ask.phase, b2.elsewhere], ["idle", "b1", "asking", null]);
  assert.deepEqual([seen(c2).turns, c2.elsewhere?.from, canAsk(c2)], [
    ["What is the property damage deductible?", "Does it apply per occurrence?"], "b1", false,
  ]);

  const b3 = step(b2, { type: "answered", turn: exclusions });
  const bDone = told(CHAT, B, b2, b3);
  const all = { ask: "idle", turns: ["What is the property damage deductible?", "Does it apply per occurrence?", "Exclusions?"] };
  assert.deepEqual([deliver(a2, bDone), b3, deliver(c2, bDone)].map((t) => [seen(t), t.elsewhere]), [[all, null], [all, null], [all, null]]);
});

test("a tab's own question in flight stays its own when the tab it mirrors is gone", () => {
  const b = step(oneTurn, { type: "sent", question: exclusions.question });
  const mirroring = deliver(b, told(CHAT, A, oneTurn, step(oneTurn, { type: "sent", question: perOccurrence.question })));
  const alone = step(mirroring, { type: "orphaned", from: A });
  assert.deepEqual([alone.ask, alone.elsewhere], [{ phase: "asking", question: "Exclusions?" }, null]);
});

test("the questions to recall are each text once, oldest first, with the one in flight here and then the mirrored one last", () => {
  const two = run([{ type: "answered", turn: deductible }, { type: "answered", turn: perOccurrence }]);
  assert.deepEqual(asked(two), ["What is the property damage deductible?", "Does it apply per occurrence?"]);
  assert.deepEqual(asked(step(two, { type: "sent", question: deductible.question })), [
    "Does it apply per occurrence?", "What is the property damage deductible?",
  ]);
  const failed = run([{ type: "sent", question: worldCup.question }, lost, read(deductible, perOccurrence)], two);
  assert.deepEqual(asked(failed), ["What is the property damage deductible?", "Does it apply per occurrence?", "Who won the 2022 football World Cup?"]);
  const { other } = relay({ asker: two, other: two }, A, [{ type: "sent", question: exclusions.question }]);
  assert.deepEqual(asked(other), ["What is the property damage deductible?", "Does it apply per occurrence?", "Exclusions?"]);
  assert.deepEqual(asked(opening), []);
});
