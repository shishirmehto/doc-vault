import { test } from "node:test";
import assert from "node:assert/strict";
import { nextToSend, type Upload } from "./uploads.ts";

const waiting = (key: string): Upload => ({ key, filename: `${key}.txt`, phase: "waiting" });
const sending = (key: string): Upload => ({ key, filename: `${key}.txt`, phase: "sending", sent: 1, total: 2 });

test("at most three uploads are in flight, and the oldest waiting files go first", () => {
  const keys = (q: Upload[]) => nextToSend(q).map((u) => u.key);
  assert.deepEqual(keys([waiting("a"), waiting("b"), waiting("c"), waiting("d")]), ["a", "b", "c"]);
  assert.deepEqual(keys([sending("a"), sending("b"), waiting("c"), waiting("d")]), ["c"]);
  assert.deepEqual(keys([sending("a"), sending("b"), sending("c"), waiting("d")]), []);
});
