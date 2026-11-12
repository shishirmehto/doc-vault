import { test } from "node:test";
import assert from "node:assert/strict";
import { stillFollowing } from "./follow.ts";

const column = (top: number) => ({ top, height: 3000, client: 600 });

test("moving the column up by any means stops following a streamed answer, and nothing else does", () => {
  assert.deepEqual(
    [
      stillFollowing(true, 2400, column(2100)),
      stillFollowing(true, 1800, column(2400)),
      stillFollowing(true, 2400, column(2400)),
      stillFollowing(true, 2600, { top: 2200, height: 2800, client: 600 }),
      stillFollowing(false, 1800, column(2400)),
    ],
    [false, true, true, true, false],
  );
});
