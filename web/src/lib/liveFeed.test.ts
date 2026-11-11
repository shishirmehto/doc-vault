import { test, type TestContext } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { LiveFeed, type FeedEvent, type FeedSource, type LiveStatus } from "./liveFeed.ts";

const [parsingData] = readFileSync(new URL("../api/fixtures/events-documents.txt", import.meta.url), "utf8")
  .split("\n")
  .filter((line) => line.startsWith("data: {\"id\""))
  .map((line) => line.slice("data: ".length));

class FakeSource implements FeedSource {
  readyState = 0;
  closed = false;
  private handlers = new Map<string, ((event: MessageEvent<string>) => void)[]>();

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.handlers.set(type, [...(this.handlers.get(type) ?? []), listener]);
  }

  close(): void {
    this.readyState = 2;
    this.closed = true;
  }

  send(type: string, data = ""): void {
    (this.handlers.get(type) ?? []).forEach((h) => h({ data } as MessageEvent<string>));
  }

  open(): void {
    this.readyState = 1;
    this.send("open");
  }

  /** A network drop: the browser keeps the source and retries. */
  drop(): void {
    this.readyState = 0;
    this.send("error");
  }

  /** A non-200 answer: the browser closes the source for good. */
  refuse(): void {
    this.readyState = 2;
    this.send("error");
  }
}

function describe(e: FeedEvent): string {
  switch (e.kind) {
    case "document":
      return `document ${e.doc.filename} ${e.doc.lifecycle.status}`;
    case "deleted":
      return `deleted ${e.id}`;
    default:
      return e.kind;
  }
}

function harness(t: TestContext) {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const sources: FakeSource[] = [];
  const feed = new LiveFeed(() => {
    const source = new FakeSource();
    sources.push(source);
    return source;
  });
  const statuses: LiveStatus[] = [];
  feed.watch(() => statuses.push(feed.status()));
  const latest = () => sources.at(-1)!;
  return { feed, sources, statuses, latest, tick: (ms: number) => t.mock.timers.tick(ms) };
}

test("one stream opens for the first listener, every listener hears it, and it closes when the last one leaves", (t) => {
  const { feed, sources, latest } = harness(t);
  assert.equal(sources.length, 0);
  const heard: string[] = [];
  const leaveA = feed.subscribe((e) => heard.push(describe(e)));
  const leaveB = feed.subscribe(() => {});
  assert.equal(sources.length, 1);
  latest().open();
  latest().send("document", parsingData);
  latest().send("deleted", '{"id": "e84608c0-522b-44af-a62f-427e7b5b7784"}');
  latest().send("resync", "{}");
  latest().send("message", "{}");
  latest().send("document", "{not json");
  assert.deepEqual(heard, [
    "open",
    "document phase-b-capture-large.txt parsing",
    "deleted e84608c0-522b-44af-a62f-427e7b5b7784",
    "resync",
    "resync",
    "resync",
  ]);
  assert.equal(feed.open, true);
  leaveA();
  assert.equal(latest().closed, false);
  leaveB();
  assert.deepEqual([latest().closed, feed.open, feed.status(), sources.length], [true, false, "off", 1]);
});

test("a refused stream opens again after 2 s, doubling to 30 s, and the delay starts over once it opens", (t) => {
  const { feed, sources, latest, tick } = harness(t);
  feed.subscribe(() => {});
  const waits: number[] = [];
  const waitForNext = () => {
    const before = sources.length;
    let waited = 0;
    while (sources.length === before && waited < 60_000) {
      tick(100);
      waited += 100;
    }
    return waited;
  };
  for (let i = 0; i < 6; i++) {
    latest().refuse();
    waits.push(waitForNext());
  }
  latest().open();
  latest().refuse();
  waits.push(waitForNext());
  assert.deepEqual(waits, [2000, 4000, 8000, 16000, 30000, 30000, 2000]);
  assert.deepEqual(sources.map((s) => s.closed), [true, true, true, true, true, true, true, false]);
  latest().drop();
  assert.equal(waitForNext(), 60_000);
});

test("status reads live for 3 s while connecting, then paused, and live again once open", (t) => {
  const { feed, statuses, latest, tick } = harness(t);
  const heard: string[] = [];
  const leave = feed.subscribe((e) => heard.push(describe(e)));
  tick(2999);
  assert.deepEqual(statuses, ["live"]);
  tick(1);
  latest().open();
  assert.deepEqual(statuses, ["live", "paused", "live"]);
  latest().drop();
  tick(2000);
  latest().open();
  tick(5000);
  assert.deepEqual(statuses, ["live", "paused", "live"]);
  latest().drop();
  tick(2000);
  latest().drop();
  tick(1000);
  assert.deepEqual(statuses, ["live", "paused", "live", "paused"]);
  latest().refuse();
  tick(2000);
  latest().open();
  leave();
  assert.deepEqual(statuses, ["live", "paused", "live", "paused", "live", "off"]);
  assert.deepEqual(heard, ["open", "down", "open", "down", "open"]);
});
