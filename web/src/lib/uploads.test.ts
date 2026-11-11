import { test } from "node:test";
import assert from "node:assert/strict";
import { admit, nextToSend, type Upload } from "./uploads.ts";

const waiting = (key: string): Upload => ({ key, filename: `${key}.txt`, phase: "waiting" });
const sending = (key: string): Upload => ({ key, filename: `${key}.txt`, phase: "sending", sent: 1, total: 2 });

test("at most three uploads are in flight, and the oldest waiting files go first", () => {
  const keys = (q: Upload[]) => nextToSend(q).map((u) => u.key);
  assert.deepEqual(keys([waiting("a"), waiting("b"), waiting("c"), waiting("d")]), ["a", "b", "c"]);
  assert.deepEqual(keys([sending("a"), sending("b"), waiting("c"), waiting("d")]), ["c"]);
  assert.deepEqual(keys([sending("a"), sending("b"), sending("c"), waiting("d")]), []);
});

test("a file over the API's limit is refused before sending, and an unknown limit sends it", () => {
  const LIMIT = 52428800;
  assert.deepEqual(admit("u1", "ui-huge.bin", LIMIT + 1, LIMIT), {
    key: "u1",
    filename: "ui-huge.bin",
    phase: "too_large",
    sizeBytes: 52428801,
    limitBytes: 52428800,
  });
  assert.deepEqual(admit("u2", "exact.pdf", LIMIT, LIMIT), { key: "u2", filename: "exact.pdf", phase: "waiting" });
  assert.deepEqual(admit("u3", "ui-huge.bin", LIMIT + 1, null), { key: "u3", filename: "ui-huge.bin", phase: "waiting" });
  assert.deepEqual(nextToSend([admit("u1", "ui-huge.bin", LIMIT + 1, LIMIT)]), []);
});
