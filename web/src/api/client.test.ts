import { test } from "node:test";
import assert from "node:assert/strict";
import { api } from "./client.ts";
import type { DocumentId } from "./domain.ts";

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
