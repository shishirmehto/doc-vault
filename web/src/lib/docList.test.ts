import { test } from "node:test";
import assert from "node:assert/strict";
import type { Doc, DocumentId, FamilyId, Lifecycle } from "../api/domain.ts";
import { applyDocEvent, upsertDoc, type DocList } from "./docList.ts";

function doc(n: number, second: number, lifecycle: Lifecycle = { status: "ready" }): Doc {
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
    createdAt: new Date(Date.UTC(2026, 10, 11, 12, 0, 0, second * 1000)),
    version: { family: `00000000-0000-4000-8000-${String(n).padStart(12, "0")}` as FamilyId, number: 1, latest: 1 },
  };
}

const names = (list: DocList) => list.docs.map((d) => d.filename);
const id = (n: number) => doc(n, 0).id;
const list = (complete: boolean, ...docs: Doc[]): DocList => ({ docs, complete });

test("a known document is replaced in place, and the order stays", () => {
  const next = upsertDoc(list(true, doc(3, 30), doc(2, 20), doc(1, 10)), doc(2, 20, { status: "embedding", embedded: 5, total: 9 }));
  assert.deepEqual(next.docs.map((d) => `${d.filename} ${d.lifecycle.status}`), ["d3.txt ready", "d2.txt embedding", "d1.txt ready"]);
});

test("an unknown document goes where the server lists it: newest first, then the larger id first", () => {
  const next = [doc(4, 40), doc(5, 15), doc(6, 20), doc(0, 10)].reduce(
    (l, d) => applyDocEvent(l, { kind: "document", doc: d }),
    list(true, doc(3, 30), doc(2, 20), doc(1, 10)),
  );
  assert.deepEqual(names(next), ["d4.txt", "d3.txt", "d6.txt", "d2.txt", "d5.txt", "d1.txt", "d0.txt"]);
  assert.equal(next.complete, true);
});

test("a hundred and first document drops the oldest and marks the list incomplete", () => {
  const full = list(true, ...Array.from({ length: 100 }, (_, i) => doc(100 - i, 100 - i)));
  const next = upsertDoc(full, doc(101, 101));
  assert.deepEqual([next.docs.length, names(next)[0], names(next).at(-1), next.complete], [100, "d101.txt", "d2.txt", false]);
});

test("an incomplete list ignores an unknown document older than its last, and still takes a newer one", () => {
  const partial = list(false, doc(3, 30), doc(2, 20));
  assert.deepEqual(names(upsertDoc(partial, doc(1, 10))), ["d3.txt", "d2.txt"]);
  assert.deepEqual(upsertDoc(partial, doc(4, 40)), list(false, doc(4, 40), doc(3, 30), doc(2, 20)));
  assert.deepEqual(names(upsertDoc(list(true, doc(3, 30), doc(2, 20)), doc(1, 10))), ["d3.txt", "d2.txt", "d1.txt"]);
});

test("a delete removes the document and leaves complete as it was", () => {
  assert.deepEqual(applyDocEvent(list(false, doc(3, 30), doc(2, 20), doc(1, 10)), { kind: "deleted", id: id(2) }), list(false, doc(3, 30), doc(1, 10)));
  assert.deepEqual(applyDocEvent(list(true, doc(3, 30), doc(2, 20)), { kind: "deleted", id: id(3) }), list(true, doc(2, 20)));
  assert.deepEqual(applyDocEvent(list(true, doc(3, 30)), { kind: "deleted", id: id(9) }), list(true, doc(3, 30)));
});

/** Version `number` of `latest` in the family of document 1, uploaded at `second`. */
function version(n: number, second: number, number: number, latest: number): Doc {
  return { ...doc(n, second), filename: `terms v${number}.txt`, version: { family: id(1) as string as FamilyId, number, latest } };
}

test("the list holds one row per family, its latest version, whichever order the events arrive in", () => {
  const before = list(true, doc(7, 70), version(1, 10, 1, 1));
  const arrived = [version(2, 80, 2, 2), version(1, 10, 1, 2)];

  for (const order of [arrived, [...arrived].reverse()]) {
    const next = order.reduce((l, d) => applyDocEvent(l, { kind: "document", doc: d }), before);
    assert.deepEqual(names(next), ["terms v2.txt", "d7.txt"]);
  }
});

test("deleting the latest version brings the one before it back, where its upload time puts it", () => {
  const two = list(true, version(2, 80, 2, 2), doc(7, 70));
  const deleted = applyDocEvent(two, { kind: "deleted", id: id(2) });
  const back = applyDocEvent(deleted, { kind: "document", doc: version(1, 10, 1, 1) });
  assert.deepEqual(names(back), ["d7.txt", "terms v1.txt"]);
});

test("version 1's event can come before version 2's delete, and the list still ends on version 1", () => {
  const two = list(true, version(2, 80, 2, 2), doc(7, 70));
  const first = applyDocEvent(two, { kind: "document", doc: version(1, 10, 1, 1) });
  const back = applyDocEvent(first, { kind: "deleted", id: id(2) });
  assert.deepEqual(names(back), ["d7.txt", "terms v1.txt"]);
});

test("version 1 read before version 2 was uploaded gives way to version 2's event, which the server sends after it", () => {
  const stale = upsertDoc(list(true, version(2, 80, 2, 2)), version(1, 10, 1, 1));
  const settled = upsertDoc(stale, version(2, 80, 2, 2));
  assert.deepEqual(names(settled), ["terms v2.txt"]);
});
