"use client";

import { useState } from "react";
import type { ApiError, ChatId, Doc, DocumentId, NonEmpty } from "@/api/domain";
import { threads } from "@/hooks/useThread";
import { docMeta, explain, listNames, statusLine } from "@/copy";
import { Composer } from "./Composer";

/** The screen with nothing selected: choose ready documents and ask the first question. */
export function NewChat({ docs, onStarted }: { docs: readonly Doc[] | null; onStarted(id: ChatId): void }) {
  const [chosen, setChosen] = useState<ReadonlySet<DocumentId>>(() => new Set());
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<ApiError | null>(null);

  const ready = (docs ?? []).filter((d) => d.lifecycle.status === "ready");
  const scope = nonEmpty(ready.filter((d) => chosen.has(d.id)).map((d) => d.id));

  const toggle = (id: DocumentId) =>
    setChosen((was) => {
      const next = new Set(was);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  const start = async () => {
    if (scope === null || question.trim() === "" || busy) return;
    setBusy(true);
    setRefusal(null);
    const result = await threads.start(scope, question);
    setBusy(false);
    if (result.ok) onStarted(result.value.id);
    else setRefusal(result.error);
  };

  return (
    <main className="centre">
      <section className="new-chat" aria-labelledby="new-chat-title">
        <h1 id="new-chat-title">Ask your documents</h1>
        <p className="lead">
          Choose the documents to ask about. Every answer cites the page it came from, with the passage marked, so you
          can check it against the source.
        </p>
        {docs === null ? (
          <div aria-busy="true">{[70, 55, 62].map((w) => <span key={w} className="skeleton line" style={{ width: `${w}%` }} />)}</div>
        ) : docs.length === 0 ? (
          <p className="notice">Upload a document to start. Questions open up once it is ready.</p>
        ) : (
          <fieldset className="picker">
            <legend>Documents</legend>
            {ready.length === 0 && <p className="quiet">Questions open up once a document is ready.</p>}
            <ul>
              {docs.map((d) => {
                const isReady = d.lifecycle.status === "ready";
                return (
                  <li key={d.id}>
                    <label className="pick" data-disabled={!isReady || undefined}>
                      <input type="checkbox" checked={isReady && chosen.has(d.id)} disabled={!isReady || busy}
                        onChange={() => toggle(d.id)} />
                      <span className="pick-name">{d.filename}</span>
                      <span className="pick-meta">{isReady ? docMeta(d) : statusLine(d).text}</span>
                    </label>
                  </li>
                );
              })}
            </ul>
          </fieldset>
        )}
        {refusal && <StartRefusal error={refusal} docs={docs ?? []} />}
        <Composer
          value={question}
          onChange={setQuestion}
          onSend={() => void start()}
          ready={scope !== null && !busy}
          label={busy ? "Starting" : "Ask"}
          placeholder={
            scope === null
              ? "Choose documents above, then ask"
              : `Ask about ${scope.length === 1 ? ready.find((d) => d.id === scope[0])?.filename : `these ${scope.length} documents`}`
          }
        />
      </section>
    </main>
  );
}

/** A 409 names the documents that are not ready; they are named here with their current status. */
function StartRefusal({ error, docs }: { error: ApiError; docs: readonly Doc[] }) {
  const notReady = error.kind === "refused" && error.code === "documents_not_ready" ? error.documents : [];
  const names = notReady.map(({ id, filename }) => {
    const doc = docs.find((d) => d.id === id);
    return doc ? `${doc.filename} (${statusLine(doc).text.toLowerCase()})` : (filename ?? "a document no longer in the vault");
  });
  return (
    <div className="start-refusal" role="alert">
      <p>The chat was not started. {explain(error)}</p>
      {names.length > 0 && (
        <p>
          Not ready: {listNames(names)}. Untick {names.length === 1 ? "it" : "them"}, or wait until{" "}
          {names.length === 1 ? "it is" : "they are"} ready.
        </p>
      )}
    </div>
  );
}

function nonEmpty<T>(items: readonly T[]): NonEmpty<T> | null {
  const [first, ...rest] = items;
  return first === undefined ? null : [first, ...rest];
}
