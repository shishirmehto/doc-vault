import { test } from "node:test";
import assert from "node:assert/strict";
import { focusDropped } from "./focus.ts";

const body = {};
const findField = {};

test("focus counts as dropped when it fell to the page body or nowhere, and not while a control holds it", () => {
  assert.deepEqual(
    [
      focusDropped({ activeElement: body, body }),
      focusDropped({ activeElement: null, body }),
      focusDropped({ activeElement: findField, body }),
    ],
    [true, true, false],
  );
});
