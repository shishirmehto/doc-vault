import { test } from "node:test";
import assert from "node:assert/strict";
import type { Doc, DocEvent, DocumentId, Lifecycle } from "../api/domain.ts";
import { applyDocEvent, type DocList } from "./docList.ts";
import { ListSync } from "./listSync.ts";

function doc(n: number, lifecycle: Lifecycle = { status: "ready" }): Doc {
  return {
    id: `00000000-0000-4000-8000-${String(n).padStart(12, "0")}` as DocumentId,
    filename: `d${n}.txt`,
    kind: "txt",
    sizeBytes: 1,
    lifecycle,
    insights: "ready",
    documentType: null,
    layout: null,
    processingMs: null,
    createdAt: new Date(Date.UTC(2026, 10, 11, 12, 0, n)),
  };
}

const embedding = (embedded: number): Lifecycle => ({ status: "embedding", embedded, total: 1604 });
const listOf = (...docs: Doc[]): DocList => ({ docs, complete: true });
const event = (e: Exclude<DocEvent, { kind: "resync" }>) => (list: DocList) => applyDocEvent(list, e);
const changed = (d: Doc) => event({ kind: "document", doc: d });

function rows(sync: ListSync): string[] | null {
  return sync.shown?.docs.map((d) => (d.lifecycle.status === "embedding" ? `${d.filename} ${d.lifecycle.embedded}` : `${d.filename} ${d.lifecycle.status}`)) ?? null;
}

function loaded(...docs: Doc[]): ListSync {
  const sync = new ListSync();
  sync.resolve(sync.startFetch(), listOf(...docs));
  return sync;
}

test("an event the server applied before reading the list is applied again, and the list keeps it", () => {
  const sync = loaded(doc(1, embedding(128)));
  const fetch = sync.startFetch();
  sync.apply(changed(doc(1)));
  assert.deepEqual(rows(sync), ["d1.txt ready"]);
  assert.equal(sync.resolve(fetch, listOf(doc(2, { status: "queued" }), doc(1))), true);
  assert.deepEqual(rows(sync), ["d2.txt queued", "d1.txt ready"]);
});

test("an event the server applied after reading the list survives the older snapshot", () => {
  const sync = loaded(doc(3), doc(1, embedding(128)));
  const fetch = sync.startFetch();
  sync.apply(changed(doc(1, embedding(1280))));
  sync.apply(event({ kind: "deleted", id: doc(3).id }));
  sync.resolve(fetch, listOf(doc(3), doc(1, embedding(512))));
  assert.deepEqual(rows(sync), ["d1.txt 1280"]);
});

test("of two overlapping fetches the earlier one is dropped, and only updates since the later one are replayed", () => {
  const sync = loaded(doc(1, embedding(128)));
  const first = sync.startFetch();
  sync.apply(changed(doc(1, embedding(512))));
  const second = sync.startFetch();
  sync.apply(changed(doc(2, { status: "parsing" })));
  assert.equal(sync.resolve(first, listOf(doc(1, embedding(512)))), false);
  assert.deepEqual(rows(sync), ["d2.txt parsing", "d1.txt 512"]);
  assert.equal(sync.resolve(second, listOf(doc(1))), true);
  assert.deepEqual(rows(sync), ["d2.txt parsing", "d1.txt ready"]);
});

test("updates that arrive before the first list are replayed onto it", () => {
  const sync = new ListSync();
  const fetch = sync.startFetch();
  sync.apply(changed(doc(2, { status: "queued" })));
  assert.equal(rows(sync), null);
  sync.resolve(fetch, listOf(doc(1)));
  assert.deepEqual(rows(sync), ["d2.txt queued", "d1.txt ready"]);
});

test("a dropped fetch's failure does not stop the newer fetch's replay", () => {
  const sync = loaded(doc(1));
  const first = sync.startFetch();
  const second = sync.startFetch();
  assert.equal(sync.fail(first), false);
  sync.apply(changed(doc(2, { status: "queued" })));
  sync.resolve(second, listOf(doc(1)));
  assert.deepEqual(rows(sync), ["d2.txt queued", "d1.txt ready"]);
  const third = sync.startFetch();
  assert.equal(sync.fail(third), true);
  assert.deepEqual(rows(sync), ["d2.txt queued", "d1.txt ready"]);
});
