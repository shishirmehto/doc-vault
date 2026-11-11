import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseDocEvent } from "./parse.ts";
import { createSseReader } from "./sse.ts";
import type { DocEvent } from "./domain.ts";

const captured = readFileSync(new URL("./fixtures/events-documents.txt", import.meta.url));

function read(chunks: readonly Uint8Array[]): DocEvent[] {
  const decoder = new TextDecoder();
  const sse = createSseReader();
  return chunks.flatMap((chunk) => sse.push(decoder.decode(chunk, { stream: true }))).map(parseDocEvent);
}

function summary(e: DocEvent): string {
  switch (e.kind) {
    case "document": {
      const l = e.doc.lifecycle;
      return l.status === "embedding" ? `embedding ${l.embedded}/${l.total}` : l.status;
    }
    case "deleted":
      return `deleted ${e.id}`;
    case "resync":
      return "resync";
  }
}

const CAPTURED_SUMMARY = [
  "parsing",
  "embedding 128/1604",
  "embedding 512/1604",
  "embedding 896/1604",
  "embedding 1280/1604",
  "ready",
  "resync",
  "deleted e84608c0-522b-44af-a62f-427e7b5b7784",
];

test("the captured stream is six document updates, a resync and a delete, and its comments are no message", () => {
  const events = read([captured]);
  assert.deepEqual(events.map(summary), CAPTURED_SUMMARY);
  assert.deepEqual(events[5], {
    kind: "document",
    doc: {
      id: "e84608c0-522b-44af-a62f-427e7b5b7784",
      filename: "phase-b-capture-large.txt",
      kind: "txt",
      sizeBytes: 2515470,
      lifecycle: { status: "ready" },
      insights: "ready",
      documentType: "other",
      layout: { pageCount: 866, unit: "section", emptyPages: [] },
      processingMs: 1051,
      createdAt: new Date("2026-11-11T14:14:42.021Z"),
    },
  });
  assert.deepEqual(createSseReader().push(": connected\n\n: keepalive\n\n"), []);
});

test("the captured stream read in two chunks split at every byte gives the same events", () => {
  for (let at = 0; at <= captured.length; at++) {
    assert.deepEqual(read([captured.subarray(0, at), captured.subarray(at)]).map(summary), CAPTURED_SUMMARY, `split at ${at}`);
  }
});

test("an event name or data the contract does not name throws", () => {
  assert.throws(() => parseDocEvent({ event: "surprise", data: "{}" }), { message: "unknown document event: surprise" });
  assert.throws(() => parseDocEvent({ event: "message", data: "{}" }), { message: "unknown document event: message" });
  assert.throws(() => parseDocEvent({ event: "deleted", data: '{"id": "nope"}' }), { message: "not a document id: nope" });
  assert.throws(() => parseDocEvent({ event: "deleted", data: "{}" }), { message: "a deleted event without an id" });
  assert.throws(() => parseDocEvent({ event: "document", data: "[]" }), { message: "a document event without a document" });
  assert.throws(() => parseDocEvent({ event: "resync", data: "" }), SyntaxError);
});
