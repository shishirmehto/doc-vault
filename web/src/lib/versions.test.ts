import { test } from "node:test";
import assert from "node:assert/strict";
import type { Doc, DocumentId, FamilyId, Lifecycle } from "../api/domain.ts";
import { applyVersionEvent, familyOf, hasVersions, laterVersion } from "./versions.ts";

const FAMILY = "00000000-0000-4000-8000-000000000001" as FamilyId;

/** Version `number` of `latest` in FAMILY, with the id ...00n. */
function version(number: number, latest: number, lifecycle: Lifecycle = { status: "ready" }, family = FAMILY): Doc {
  return {
    id: `00000000-0000-4000-8000-${String(number).padStart(12, "0")}` as DocumentId,
    filename: "terms.txt",
    kind: "txt",
    sizeBytes: 1,
    lifecycle,
    insights: "ready",
    documentType: null,
    layout: null,
    processingMs: null,
    createdAt: new Date(Date.UTC(2026, 10, 12, 10, number)),
    version: { family, number, latest },
  };
}

const numbers = (docs: readonly Doc[] | null) => docs?.map((d) => d.version.number) ?? null;

test("a mark shows from version 2 on, and version 1 of 1 has no family to show", () => {
  assert.deepEqual([laterVersion(version(1, 2)), laterVersion(version(2, 2)), laterVersion(version(7, 9))], [null, 2, 7]);
  assert.deepEqual([hasVersions(version(1, 1)), hasVersions(version(1, 2)), hasVersions(version(2, 2))], [false, true, true]);
});

test("the latest version links its earlier ones and compares with the newest of them", () => {
  const f = familyOf(version(3, 3), [version(3, 3), version(2, 3), version(1, 3)]);
  assert.deepEqual([numbers(f.others), f.newest, f.compareWith?.version.number], [[1, 2], null, 2]);
});

test("an earlier version points at the latest and compares with it", () => {
  const f = familyOf(version(1, 3), [version(1, 3), version(2, 3), version(3, 3)]);
  assert.deepEqual([numbers(f.others), f.newest?.version.number, f.compareWith?.version.number], [[2, 3], 3, 3]);
});

test("a version whose others were all deleted has nothing to link or compare", () => {
  const f = familyOf(version(2, 2), [version(2, 2)]);
  assert.deepEqual([numbers(f.others), f.newest, f.compareWith], [[], null, null]);
});

test("the family follows its own events: a new version, a status change and a delete; another family's are ignored", () => {
  const read = [version(1, 1)];
  const arrived = applyVersionEvent(read, FAMILY, { kind: "document", doc: version(2, 2, { status: "parsing" }) });
  const updated = applyVersionEvent(arrived, FAMILY, { kind: "document", doc: version(1, 2) });
  const ready = applyVersionEvent(updated, FAMILY, { kind: "document", doc: version(2, 2) });
  const deleted = applyVersionEvent(ready, FAMILY, { kind: "deleted", id: version(2, 2).id });
  const stranger = version(5, 5, { status: "ready" }, "00000000-0000-4000-8000-000000000009" as FamilyId);

  assert.deepEqual(arrived.map((d) => [d.version.number, d.version.latest, d.lifecycle.status]), [[1, 1, "ready"], [2, 2, "parsing"]]);
  assert.deepEqual(ready.map((d) => [d.version.number, d.version.latest, d.lifecycle.status]), [[1, 2, "ready"], [2, 2, "ready"]]);
  assert.deepEqual(numbers(deleted), [1]);
  assert.equal(applyVersionEvent(ready, FAMILY, { kind: "document", doc: stranger }), ready);
  assert.equal(applyVersionEvent(ready, FAMILY, { kind: "deleted", id: stranger.id }), ready);
});
