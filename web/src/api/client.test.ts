import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { api } from "./client.ts";
import type { ChatId, DocumentId } from "./domain.ts";

const POLICY = "6e67edc4-27c9-4d1b-9090-4b47a40b3c29" as DocumentId;
const RENEWAL = "23b7e49c-b644-4ccf-8768-5716b69cb9cc" as DocumentId;

test("compare sends the focus trimmed, and a blank one as null", async (t) => {
  const sent: unknown[] = [];
  t.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    sent.push(JSON.parse(String(init.body)));
    return new Response("{}", { status: 503 });
  });

  await api.compare([POLICY, RENEWAL], "  deductibles and limits \n");
  await api.compare([POLICY, RENEWAL], " \n ");

  assert.deepEqual(sent, [
    { document_ids: [POLICY, RENEWAL], focus: "deductibles and limits" },
    { document_ids: [POLICY, RENEWAL], focus: null },
  ]);
});

test("summarize posts the options to the document's summaries, a missing focus as null", async (t) => {
  const sent: unknown[] = [];
  t.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    sent.push([init.method, url, JSON.parse(String(init.body))]);
    return new Response("{}", { status: 503 });
  });

  await api.summarize(POLICY, { length: "brief", focus: "deductibles", tone: "client" });
  await api.summarize(POLICY, { length: "standard", focus: null, tone: "plain" });

  assert.deepEqual(sent, [
    ["POST", `/api/documents/${POLICY}/summaries`, { length: "brief", focus: "deductibles", tone: "client" }],
    ["POST", `/api/documents/${POLICY}/summaries`, { length: "standard", focus: null, tone: "plain" }],
  ]);
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function captured(name: string): { status: number; headers: Record<string, string>; body: unknown } {
  return JSON.parse(readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), "utf8"));
}

function response(name: string): Response {
  const { status, headers, body } = captured(name);
  return new Response(JSON.stringify(body), { status, headers });
}

const sentId = (init: RequestInit) => (init.headers as Record<string, string>)["X-Request-ID"];

test("each request sends a fresh request id; a failure carries the id the API echoed, else the one it sent", async (t) => {
  const sent: string[] = [];
  const replies = [response("constructed/error-500-internal"), new Response("Bad gateway", { status: 502 })];
  t.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    sent.push(sentId(init)!);
    return replies.shift()!;
  });

  const echoed = await api.documents();
  const unechoed = await api.document(POLICY);

  assert.equal(sent.length, 2);
  assert.match(sent[0]!, UUID);
  assert.match(sent[1]!, UUID);
  assert.notEqual(sent[0], sent[1]);
  assert.deepEqual(
    [!echoed.ok && echoed.error.kind === "refused" && [echoed.error.code, echoed.error.requestId], !unechoed.ok && unechoed.error],
    [["internal_error", "phase-f-500-probe"], { kind: "broken", status: 502, requestId: sent[1] }],
  );
});

test("a streamed ask refused before its stream, or ended by an error event, carries its request id", async (t) => {
  const stream = readFileSync(new URL("./fixtures/constructed/ask-stream-error-quota-exceeded.txt", import.meta.url));
  const sent: string[] = [];
  const replies = [
    response("ask-stream-429-quota-exceeded"),
    new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream", "X-Request-ID": "echoed-stream" } }),
    new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
  ];
  t.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    sent.push(sentId(init)!);
    return replies.shift()!;
  });
  const chat = "3b127078-958a-4485-9695-e8e7802a960b" as ChatId;
  const ids = [];
  for (let i = 0; i < 3; i++) {
    const r = await api.askStream(chat, "What is the property damage deductible?", () => {});
    ids.push(!r.ok && r.error.kind === "refused" && [r.error.status, r.error.code, r.error.requestId, r.error.quota?.limitUsd]);
  }
  assert.deepEqual(ids, [
    [429, "quota_exceeded", "0ed48ac56b3a4b04a2f27815b1eb4b28", 0.000001],
    [200, "quota_exceeded", "echoed-stream", 0.000001],
    [200, "quota_exceeded", sent[2], 0.000001],
  ]);
});

test("an upload sends a request id on its XHR, and its refusal carries the id the API echoed", async () => {
  const { status, headers, body } = captured("upload-429-rate-limited");
  const sent: Record<string, string>[] = [];
  class FakeXhr {
    status = 0;
    responseText = "";
    upload = { onprogress: null };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    private headers: Record<string, string> = {};
    open() {}
    setRequestHeader(name: string, value: string) {
      this.headers[name] = value;
    }
    getResponseHeader(name: string): string | null {
      return { "retry-after": headers["retry-after"]!, "x-request-id": "echoed-upload" }[name.toLowerCase()] ?? null;
    }
    send() {
      sent.push(this.headers);
      this.status = status;
      this.responseText = JSON.stringify(body);
      queueMicrotask(() => this.onload?.());
    }
  }
  const real = globalThis.XMLHttpRequest;
  globalThis.XMLHttpRequest = FakeXhr as unknown as typeof XMLHttpRequest;
  try {
    const r = await api.upload(new File(["x"], "a.txt"), () => {});
    assert.deepEqual(!r.ok && r.error.kind === "refused" && [r.error.code, r.error.retryAfterS, r.error.requestId], [
      "rate_limited", 2, "echoed-upload",
    ]);
    assert.match(sent[0]?.["X-Request-ID"] ?? "", UUID);
  } finally {
    globalThis.XMLHttpRequest = real;
  }
});

function cutMidBody(status: number): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('{"items": ['));
      controller.error(new TypeError("network error"));
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "application/json" } });
}

test("a body cut mid-read is offline, never a rejected promise", async (t) => {
  const CHAT = "579aa94e-85de-458e-b749-cb774b9d6ca7" as ChatId;
  const calls: [string, number, () => Promise<unknown>][] = [
    ["documents", 200, () => api.documents()],
    ["history", 200, () => api.history(CHAT)],
    ["compare", 201, () => api.compare([POLICY, RENEWAL], null)],
    ["summarize", 201, () => api.summarize(POLICY, { length: "brief", focus: null, tone: "plain" })],
    ["a refused streamed ask", 429, () => api.askStream(CHAT, "What is the deductible?", () => {})],
  ];
  const outcomes: [string, unknown][] = [];
  for (const [name, status, run] of calls) {
    t.mock.method(globalThis, "fetch", async () => cutMidBody(status));
    outcomes.push([name, await run().catch((err: unknown) => `rejected: ${String(err)}`)]);
  }
  assert.deepEqual(outcomes, calls.map(([name]) => [name, { ok: false, error: { kind: "offline" } }]));
});
