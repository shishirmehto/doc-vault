import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { readAskStream } from "./client.ts";
import type { AskProgress, Result, Turn } from "./domain.ts";

const captured = readFileSync(new URL("./fixtures/ask-stream-deductible.txt", import.meta.url));

function body(chunks: readonly Uint8Array[], failure?: Error): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(chunk));
      if (failure) controller.error(failure);
      else controller.close();
    },
  });
}

/** The X-Request-ID of the stream's 200, which an error event carries. */
const STREAM_ID = "9f0c2d6e4b8a4f1e8c3d5a7b9e1f3a5c";

async function read(chunks: readonly Uint8Array[], failure?: Error): Promise<{ progress: AskProgress[]; result: Result<Turn> }> {
  const progress: AskProgress[] = [];
  const result = await readAskStream(body(chunks, failure), (p) => progress.push(p), STREAM_ID);
  return { progress, result };
}

const bytes = (text: string) => new TextEncoder().encode(text);

const DEDUCTIBLE_PROGRESS: AskProgress[] = [
  { kind: "stage", stage: "searching" },
  { kind: "stage", stage: "answering" },
  ...["Property", " Damage", " Deductible", " $2,500", " per", " occurrence"].map((text) => ({ kind: "text" as const, text })),
];

test("a captured stream read in two chunks split at every byte gives the same progress and turn", async () => {
  for (let at = 0; at <= captured.length; at++) {
    const { progress, result } = await read([captured.subarray(0, at), captured.subarray(at)]);
    assert.deepEqual(progress, DEDUCTIBLE_PROGRESS, `split at ${at}`);
    assert.ok(result.ok, `split at ${at}`);
    assert.deepEqual(
      [result.value.id, result.value.outcome.kind, result.value.suggestions],
      ["c433e1d9-9af3-403b-b06a-21187f685f22", "answered", ["What is the Policy Number?", "What is the Named Insured?", "What is the Mailing Address?"]],
    );
  }
});

test("a character split across network chunks arrives whole, and a reset passes through in order", async () => {
  const stream = bytes('event: delta\ndata: {"text": "Café "}\n\nevent: reset\ndata: {}\n\nevent: delta\ndata: {"text": "Café"}\n\n');
  const at = stream.indexOf(0xc3) + 1;
  const { progress } = await read([stream.subarray(0, at), stream.subarray(at)]);
  assert.deepEqual(progress, [{ kind: "text", text: "Café " }, { kind: "reset" }, { kind: "text", text: "Café" }]);
});

test("an error event ends the stream as the backend's refusal", async () => {
  const { progress, result } = await read([bytes(
    'event: stage\ndata: {"stage": "searching"}\n\nevent: error\ndata: {"error": {"code": "ai_unavailable", ' +
    '"message": "The AI provider could not complete the request (server_error).", "retryable": true, "details": {}}}\n\n',
  )]);
  assert.deepEqual(progress, [{ kind: "stage", stage: "searching" }]);
  assert.deepEqual(result, {
    ok: false,
    error: {
      kind: "refused", status: 200, code: "ai_unavailable", retryable: true, retryAfterS: null, documents: [],
      message: "The AI provider could not complete the request (server_error).", quota: null, requestId: STREAM_ID,
    },
  });
});

test("a body that ends or breaks before the turn is a lost response, and an unknown event is unexpected", async () => {
  const cut = captured.subarray(0, captured.indexOf(bytes("event: turn")));
  assert.deepEqual((await read([cut])).result, { ok: false, error: { kind: "offline" } });
  assert.deepEqual((await read([cut], new TypeError("network error"))).result, { ok: false, error: { kind: "offline" } });
  assert.deepEqual((await read([bytes('event: surprise\ndata: {}\n\n')])).result, {
    ok: false,
    error: { kind: "unexpected", detail: "unknown stream event: surprise" },
  });
});

test("a cached answer streams as one piece of text, then its turn marked cached with no model call", async () => {
  const stream = readFileSync(new URL("./fixtures/ask-stream-cached.txt", import.meta.url));
  for (let at = 0; at <= stream.length; at++) {
    const { progress, result } = await read([stream.subarray(0, at), stream.subarray(at)]);
    assert.deepEqual(progress, [{ kind: "text", text: "Property Damage Deductible: $2,500 per occurrence" }], `split at ${at}`);
    assert.deepEqual(result.ok && [result.value.id, result.value.cached, result.value.usage.calls], [
      "400823cd-69d1-421e-979a-d550e7f236da", true, 0,
    ], `split at ${at}`);
  }
});

test("the cap reached after the stream started ends it with the quota refusal and the stream's request id", async () => {
  const stream = readFileSync(new URL("./fixtures/constructed/ask-stream-error-quota-exceeded.txt", import.meta.url));
  for (let at = 0; at <= stream.length; at++) {
    const { progress, result } = await read([stream.subarray(0, at), stream.subarray(at)]);
    assert.deepEqual(progress, [{ kind: "stage", stage: "searching" }, { kind: "stage", stage: "answering" }], `split at ${at}`);
    assert.deepEqual(result, {
      ok: false,
      error: {
        kind: "refused", status: 200, code: "quota_exceeded", retryable: true, retryAfterS: null, documents: [],
        message: "Today's AI spending cap of $0.000001 is used up. It resets at midnight UTC.",
        quota: { limitUsd: 0.000001, spentTodayUsd: 0.01, resetsAt: new Date("2026-11-13T00:00:00Z") },
        requestId: STREAM_ID,
      },
    }, `split at ${at}`);
  }
});
