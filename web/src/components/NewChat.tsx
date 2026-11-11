"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { ApiError, Chat, ChatId, Doc, DocumentId, NonEmpty } from "@/api/domain";
import { selectionQuery } from "@/api/parse";
import { useFetched } from "@/hooks/useFetched";
import { useFileDrop } from "@/hooks/useFileDrop";
import { threads } from "@/hooks/useThread";
import { CHAT_MAX_DOCUMENTS, FIND_AFTER, allChosen, chooseAll, findDocs } from "@/lib/find";
import { docMeta, explain, listNames, statusLine } from "@/copy";
import { Composer } from "./Composer";
import { DropZone } from "./DropZone";
import { FindField } from "./FindField";

/**
 * The screen with nothing selected: choose ready documents and ask the first question, or reopen a recent chat.
 * An empty vault shows the steps from upload to first question instead.
 */
export function NewChat({ docs, failed, deleted, onDismiss, onOpen, onFiles }: {
  docs: readonly Doc[] | null;
  /** The last failed read of the document list, which is retried on its own. */
  failed: ApiError | null;
  /** The filename of a document just deleted, to confirm it. */
  deleted: string | null;
  onDismiss(): void;
  onOpen(id: ChatId): void;
  onFiles(files: File[]): void;
}) {
  const [chosen, setChosen] = useState<ReadonlySet<DocumentId>>(() => new Set());
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<ApiError | null>(null);
  const [query, setQuery] = useState("");
  const [capped, setCapped] = useState(false);

  const ready = (docs ?? []).filter((d) => d.lifecycle.status === "ready");
  const scope = nonEmpty(ready.filter((d) => chosen.has(d.id)).map((d) => d.id));
  const finding = docs !== null && docs.length > FIND_AFTER;
  const shown = docs && finding ? findDocs(docs, query) : (docs ?? []);
  const shownReady = shown.filter((d) => d.lifecycle.status === "ready").map((d) => d.id);
  const allShownChosen = allChosen(chosen, shownReady, CHAT_MAX_DOCUMENTS);
  const hiddenChosen = (scope ?? []).filter((id) => !shown.some((d) => d.id === id)).length;
  const selectAll = () => {
    const next = chooseAll(chosen, shownReady, CHAT_MAX_DOCUMENTS);
    setChosen(next.chosen);
    setCapped(next.capped);
  };

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
    if (result.ok) onOpen(result.value.id);
    else setRefusal(result.error);
  };

  return (
    <main className="centre with-composer">
      <div className="centre-scroll">
        <section className="new-chat" aria-labelledby="new-chat-title">
          {deleted && <DeletedNote key={deleted} filename={deleted} onDismiss={onDismiss} />}
          <h1 id="new-chat-title">Ask your documents</h1>
          <p className="lead">
            {docs?.length === 0 ? "" : "Choose the documents to ask about. "}Every answer cites the page it came from,
            with the passage marked, so you can check it against the source.
          </p>
          {docs === null && failed ? (
            <p className="notice" data-tone="failed">{explain(failed)} Retrying.</p>
          ) : docs === null ? (
            <div aria-busy="true">{[70, 55, 62].map((w) => <span key={w} className="skeleton line" style={{ width: `${w}%` }} />)}</div>
          ) : docs.length === 0 ? (
            <FirstSteps onFiles={onFiles} />
          ) : (
            <fieldset className="picker">
              <legend>Documents</legend>
              <div className="picker-tools">
                {finding && <FindField label="Find a document to ask about" value={query} onChange={setQuery} />}
                <p className="pick-count" aria-live="polite">
                  {scope === null ? "None chosen" : `${scope.length} chosen`}
                  {hiddenChosen > 0 && `, ${hiddenChosen} not shown by the search`}
                </p>
                {ready.length > 1 && (
                  <button type="button" className="text-button" disabled={busy || shownReady.length === 0} onClick={selectAll}>
                    {allShownChosen ? "Unselect all ready" : "Select all ready"}
                  </button>
                )}
              </div>
              {capped && (
                <p className="quiet">A chat answers from at most {CHAT_MAX_DOCUMENTS} documents, so only the first {CHAT_MAX_DOCUMENTS} are chosen.</p>
              )}
              {ready.length === 0 && <p className="quiet">Questions open up once a document is ready.</p>}
              {finding && shown.length === 0 && (
                <p className="quiet">
                  No document matches &ldquo;{query.trim()}&rdquo;.{" "}
                  <button type="button" className="text-button" onClick={() => setQuery("")}>Clear</button>
                </p>
              )}
              <ul>
                {shown.map((d) => {
                  const isReady = d.lifecycle.status === "ready";
                  return (
                    <li key={d.id}>
                      <label className="pick" data-disabled={!isReady || undefined}>
                        <input type="checkbox" checked={isReady && chosen.has(d.id)} disabled={!isReady || busy}
                          aria-labelledby={`pick-${d.id}`} aria-describedby={`pick-meta-${d.id}`} onChange={() => toggle(d.id)} />
                        <span className="pick-name" id={`pick-${d.id}`}>{d.filename}</span>
                        <span className="pick-meta" id={`pick-meta-${d.id}`}>{isReady ? docMeta(d) : statusLine(d).text}</span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            </fieldset>
          )}
        </section>
        <RecentChats onOpen={onOpen} />
      </div>
      {ready.length > 0 && (
        <div className="centre-footer">
          {refusal && <StartRefusal error={refusal} docs={docs ?? []} />}
          {scope === null && question.trim() !== "" && <p className="quiet">Tick at least one ready document above to ask.</p>}
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
        </div>
      )}
    </main>
  );
}

/** An empty vault: the real sequence from upload to first question, with the upload target in step 1. */
function FirstSteps({ onFiles }: { onFiles(files: File[]): void }) {
  const drop = useFileDrop(onFiles);
  return (
    <ol className="steps">
      <li>
        <p className="step-title">Upload policies, quotes or endorsements.</p>
        <div className="step-drop" {...drop.handlers}>
          <DropZone dragging={drop.dragging} onFiles={onFiles} large>
            <p className="dropzone-hint">The repo&apos;s samples folder has three insurance documents to try.</p>
          </DropZone>
        </div>
      </li>
      <li>
        <p className="step-title">Wait until each shows Ready, when its pages are read and its key facts found.</p>
      </li>
      <li>
        <p className="step-title">Ask a question.</p>
      </li>
    </ol>
  );
}

/** Takes focus when it appears, because the screen the reader acted on is gone. */
function DeletedNote({ filename, onDismiss }: { filename: string; onDismiss(): void }) {
  const note = useRef<HTMLParagraphElement>(null);
  useEffect(() => note.current?.focus(), []);
  return (
    <p className="done-note" role="status" tabIndex={-1} ref={note}>
      Deleted {filename}. Chats that used it keep their answers and citations.{" "}
      <button type="button" className="text-button" onClick={onDismiss}>Dismiss</button>
    </p>
  );
}

const WHEN = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** Titles are usually empty and many chats share a document, so each row also says when it started. */
function RecentChats({ onOpen }: { onOpen(id: ChatId): void }) {
  const { result, reload } = useFetched("recent-chats", api.recentChats);
  if (result === null || (result.ok && result.value.length === 0)) return null;
  return (
    <section className="recent-chats" aria-labelledby="recent-chats-title">
      <h2 id="recent-chats-title">Recent chats</h2>
      {result.ok ? (
        <ul>
          {result.value.map((chat) => (
            <li key={chat.id}>
              <a
                href={selectionQuery({ kind: "chat", chat: chat.id, pane: null })}
                onClick={(e) => {
                  if (e.metaKey || e.ctrlKey || e.shiftKey) return;
                  e.preventDefault();
                  onOpen(chat.id);
                }}
              >
                {chatName(chat)}
              </a>
              <time dateTime={chat.createdAt.toISOString()}>{WHEN.format(chat.createdAt)}</time>
            </li>
          ))}
        </ul>
      ) : (
        <p className="quiet">
          {explain(result.error)}{" "}
          <button type="button" className="text-button" onClick={reload}>Try again</button>
        </p>
      )}
    </section>
  );
}

function chatName(chat: Chat): string {
  return chat.title ?? listNames(chat.documents.map((d) => (d.deleted ? `${d.filename} (deleted)` : d.filename)));
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
