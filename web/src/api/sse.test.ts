import { test } from "node:test";
import assert from "node:assert/strict";
import { createSseReader, type SseMessage } from "./sse.ts";

function read(chunks: readonly string[]): SseMessage[] {
  const reader = createSseReader();
  return chunks.flatMap((chunk) => reader.push(chunk));
}

const TURN =
  String.raw`{"id": "579aa94e-85de-458e-b749-cb774b9d6ca7", "question": "What is the property damage deductible at the café? 🏠", ` +
  String.raw`"interpreted_as": "What is the property damage deductible at the café? 🏠", "status": "answered", "reason": null, ` +
  String.raw`"answer": "Property Damage Deductible $2,500 per occurrence [1]", "citations": [{"ref": 1, ` +
  String.raw`"document_id": "6e67edc4-27c9-4d1b-9090-4b47a40b3c29", "filename": "sample-cgl-policy.pdf", "page": 2, ` +
  String.raw`"page_label": "p. 2", "quote": "DEDUCTIBLES\nProperty Damage Deductible $2,500 per occurrence\n", ` +
  String.raw`"chunk_id": "016551f1-822d-4f35-838c-d7fbca9ac1e2", "page_url": "/documents/6e67edc4-27c9-4d1b-9090-4b47a40b3c29/pages/2"}], ` +
  String.raw`"usage": {"calls": 2, "input_tokens": 1960, "output_tokens": 11, "cost_usd": 0.0}, "latency_ms": 13, ` +
  String.raw`"created_at": "2026-11-11T03:40:37.110989Z"}`;

const STREAM = `event: stage
data: {"stage": "searching"}

event: delta
data: {"text": "Property Damage Deductible "}

event: delta
data: {"text": "is $5,000 "}

event: delta
data: {"text": "per claim"}

event: reset
data: {}

event: delta
data: {"text": "Property Damage Deductible "}

event: delta
data: {"text": "$2,500 per occurrence [1]"}

event: turn
data: ${TURN}

`;

const MESSAGES: SseMessage[] = [
  { event: "stage", data: '{"stage": "searching"}' },
  { event: "delta", data: '{"text": "Property Damage Deductible "}' },
  { event: "delta", data: '{"text": "is $5,000 "}' },
  { event: "delta", data: '{"text": "per claim"}' },
  { event: "reset", data: "{}" },
  { event: "delta", data: '{"text": "Property Damage Deductible "}' },
  { event: "delta", data: '{"text": "$2,500 per occurrence [1]"}' },
  { event: "turn", data: TURN },
];

for (const [name, eol] of [["LF", "\n"], ["CRLF", "\r\n"], ["CR", "\r"]] as const) {
  const stream = STREAM.replaceAll("\n", eol);
  test(`every split of a stream with ${name} line endings yields the same messages`, () => {
    for (let i = 0; i <= stream.length; i++) {
      assert.deepEqual(read([stream.slice(0, i), stream.slice(i)]), MESSAGES, `split at ${i}`);
    }
    assert.deepEqual(read([...stream]), MESSAGES, "one character per chunk");
  });
}

test("a CR then an LF in the next chunk is one line ending, and a CR dispatches without waiting for it", () => {
  const reader = createSseReader();
  assert.deepEqual(
    [reader.push("data: a\r"), reader.push(""), reader.push("\ndata: b\r\n\r"), reader.push("\n"), reader.push("data: c\r\n\r\n")],
    [[], [], [{ event: "message", data: "a\nb" }], [], [{ event: "message", data: "c" }]],
  );
});

test("a bare CR ends a line", () => {
  assert.deepEqual(read(["event: delta\rdata: one\rdata: two\r\rdata: three\r\r"]), [
    { event: "delta", data: "one\ntwo" },
    { event: "message", data: "three" },
  ]);
});

test("comment lines are skipped, as a keep-alive and inside a message", () => {
  assert.deepEqual(read([": keep-alive\n\nevent: stage\n:data: not data\ndata: {}\n:\n\n"]), [{ event: "stage", data: "{}" }]);
});

test("data lines are joined with LF, and an empty data line keeps its place", () => {
  assert.deepEqual(read(['data: {"text":\ndata:\ndata: "two lines"}\n\n']), [
    { event: "message", data: '{"text":\n\n"two lines"}' },
  ]);
});

test("only one space after the colon is dropped", () => {
  assert.deepEqual(read(["event:delta\ndata:no space\ndata:  two spaces\ndata:\ttab\n\n"]), [
    { event: "delta", data: "no space\n two spaces\n\ttab" },
  ]);
});

test("a line with no colon is a field with an empty value", () => {
  assert.deepEqual(read(["event: delta\nevent\ndata\n\n"]), [{ event: "message", data: "" }]);
});

test("a message with no data lines is not dispatched, and its event type does not carry over", () => {
  assert.deepEqual(read(["event: reset\n\ndata: x\n\nevent: reset\ndata: {}\n\n"]), [
    { event: "message", data: "x" },
    { event: "reset", data: "{}" },
  ]);
});

test("id, retry, unknown fields and other-case field names are ignored", () => {
  assert.deepEqual(read(["id: 7\nretry: 1000\nfoo: bar\nData: no\nEvent: no\ndata: yes\n\n"]), [
    { event: "message", data: "yes" },
  ]);
});

test("a body that closes before a message's blank line drops that message", () => {
  const stage = 'event: stage\ndata: {"stage": "searching"}\n\n';
  for (const cut of ['event: turn\ndata: {"id": "579aa94e"}\n', 'event: turn\ndata: {"id": "57', "event: turn\r"]) {
    const reader = createSseReader();
    assert.deepEqual(reader.push(stage + cut), [{ event: "stage", data: '{"stage": "searching"}' }], cut);
  }
});
