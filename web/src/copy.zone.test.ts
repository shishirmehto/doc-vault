import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Node runs each test file in its own process, and copy.ts builds its date formatters when it loads, so the zone set
// here, before the import, is the one every sentence below is written in: India, where 00:00 UTC is 05:30.
process.env.TZ = "Asia/Kolkata";
const { cacheSince, capFigures, capReached, compareFailed, explain } = await import("./copy.ts");
const { parseError } = await import("./api/parse.ts");
const { readAskStream } = await import("./api/client.ts");

function refusal(name: string) {
  const { status, headers, body } = JSON.parse(readFileSync(new URL(`./api/fixtures/${name}.json`, import.meta.url), "utf8"));
  return parseError(status, body, headers["retry-after"] ?? null, headers["x-request-id"] ?? null);
}

const SENTENCE =
  "Today's AI spending cap of $0.000001 is used up. Asking, comparing and rewriting start again at 05:30 on 13 Nov, when it resets.";

test("the cap's sentence names the cap with its own decimals and the reset in the reader's time", () => {
  assert.deepEqual(
    [
      capReached({ limitUsd: 5, spentTodayUsd: 5.01, resetsAt: new Date("2026-11-13T00:00:00Z") }),
      capReached({ limitUsd: 0.25, spentTodayUsd: 0.25, resetsAt: new Date("2026-12-31T00:00:00Z") }),
    ],
    [
      "Today's AI spending cap of $5.00 is used up. Asking, comparing and rewriting start again at 05:30 on 13 Nov, when it resets.",
      "Today's AI spending cap of $0.25 is used up. Asking, comparing and rewriting start again at 05:30 on 31 Dec, when it resets.",
    ],
  );
});

test("Ask, the streamed ask, Compare and Rewrite the summary all say the same sentence for a used-up cap", async () => {
  const stream = readFileSync(new URL("./api/fixtures/constructed/ask-stream-error-quota-exceeded.txt", import.meta.url));
  const streamed = await readAskStream(new Response(stream).body!, () => {}, "stream-id");
  assert.ok(!streamed.ok);
  assert.deepEqual(
    [
      explain(refusal("ask-429-quota-exceeded")),
      explain(refusal("ask-stream-429-quota-exceeded")),
      explain(streamed.error),
      compareFailed(refusal("compare-429-quota-exceeded"), []),
      explain(refusal("summary-429-quota-exceeded")),
    ],
    [SENTENCE, SENTENCE, SENTENCE, [`The comparison was not made. ${SENTENCE}`], SENTENCE],
  );
});

test("the Spending cap table: the cap, today's spend, what is left and the reset in local time, with no line under the cap", () => {
  assert.deepEqual(capFigures({ limitUsd: 5, spentTodayUsd: 1.25 }, new Date("2026-11-12T20:00:00Z")), {
    rows: [["Cap per day (UTC)", "$5.00"], ["Spent today", "$1.25"], ["Left", "$3.75"], ["Resets", "05:30 on 13 Nov"]],
    stopped: null,
  });
  assert.deepEqual(capFigures({ limitUsd: 5, spentTodayUsd: 0.0123456 }, new Date("2026-11-12T23:59:59Z")).rows[2], ["Left", "$4.9876544"]);
});

test("over the cap, nothing is left below $0 and the line above the table says what stops, until when, and what keeps working", () => {
  assert.deepEqual(capFigures({ limitUsd: 0.000001, spentTodayUsd: 0.01 }, new Date("2026-11-12T02:01:22Z")), {
    rows: [["Cap per day (UTC)", "$0.000001"], ["Spent today", "$0.01"], ["Left", "$0.00"], ["Resets", "05:30 on 13 Nov"]],
    stopped:
      "The cap is used up. Asking, comparing, rewriting and new insights stop until 05:30 on 13 Nov. Uploads are still " +
      "indexed. Raise DAILY_AI_BUDGET_USD for the api and worker to go on sooner.",
  });
  const atTheCap = capFigures({ limitUsd: 5, spentTodayUsd: 5 }, new Date("2026-11-12T19:00:00Z"));
  assert.deepEqual([atTheCap.rows[2], atTheCap.stopped?.startsWith("The cap is used up.")], [["Left", "$0.00"], true]);
});

test("the cache's lead line gives the first count in local time, says it does not follow the window, and when it restarts", () => {
  assert.equal(
    cacheSince(new Date("2026-11-12T02:00:55.418Z")),
    "These counts run from 07:30 on 12 Nov and do not follow the window chosen above. They start again from zero when the " +
      "cache restarts, or drops them to make room.",
  );
});
