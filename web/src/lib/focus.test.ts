import { test } from "node:test";
import assert from "node:assert/strict";
import { headingTakesFocus } from "./focus.ts";

const body = {};
const findField = {};

test("a screen's heading takes the focus that fell to the page when the reader moved there, and no other", () => {
  assert.deepEqual(
    [
      headingTakesFocus(true, { activeElement: body, body }),
      headingTakesFocus(true, { activeElement: null, body }),
      headingTakesFocus(false, { activeElement: body, body }),
      headingTakesFocus(true, { activeElement: findField, body }),
    ],
    [true, true, false, false],
  );
});
