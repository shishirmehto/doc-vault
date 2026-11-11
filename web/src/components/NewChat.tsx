"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { ApiError, Chat, ChatId, Doc, DocumentId, NonEmpty } from "@/api/domain";
import { useFetched } from "@/hooks/useFetched";
import { useFileDrop } from "@/hooks/useFileDrop";
import { threads } from "@/hooks/useThread";
import { useCompareRun } from "@/hooks/useCompareRun";
import { COMPARE_MIN, columns, type CompareRequest } from "@/lib/compare";
import { CHAT_MAX_DOCUMENTS } from "@/lib/find";
import { COMPARE_BUSY, COMPARE_FOCUS_HINT, compareBlocked, documentNames, explain, formatWhen, listNames, notReadyNames } from "@/copy";
import { Composer } from "./Composer";
import { DropZone } from "./DropZone";
import { InAppLink } from "./InAppLink";
import { Picker } from "./Picker";

/**
 * The screen with nothing selected: choose ready documents and ask the first question, compare them, or reopen a
 * recent chat. An empty vault shows the steps from upload to first question instead.
 */
export function NewChat({ docs, failed, deleted, onDismiss, onOpen, onFiles, onCompare }: {
  docs: readonly Doc[] | null;
  /** The last failed read of the document list, which is retried on its own. */
  failed: ApiError | null;
  /** The filename of a document just deleted, to confirm it. */
  deleted: string | null;
  onDismiss(): void;
  onOpen(id: ChatId): void;
  onFiles(files: File[]): void;
  /** Compares the ticked documents, with the question as the focus, on the compare screen. */
  onCompare(request: CompareRequest): void;
}) {
  const [chosen, setChosen] = useState<ReadonlySet<DocumentId>>(() => new Set());
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<ApiError | null>(null);

  const ready = (docs ?? []).filter((d) => d.lifecycle.status === "ready");
  const scope = nonEmpty(ready.filter((d) => chosen.has(d.id)).map((d) => d.id));
  const picked = columns(chosen, ready);
  const comparable = picked.length >= COMPARE_MIN;
  const running = useCompareRun().phase === "running";
  const compareBlock = !comparable ? null : running ? COMPARE_BUSY : compareBlocked(picked.length, question);

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
            <Picker docs={docs} chosen={chosen} onChange={setChosen} findLabel="Find a document to ask about" busy={busy}
              limit={{ kind: "select-all", max: CHAT_MAX_DOCUMENTS }} noneReady="Questions open up once a document is ready." />
          )}
        </section>
        <RecentChats onOpen={onOpen} />
      </div>
      {ready.length > 0 && (
        <div className="centre-footer">
          {refusal && <StartRefusal error={refusal} docs={docs ?? []} />}
          {scope === null && question.trim() !== "" && <p className="quiet">Tick at least one ready document above to ask.</p>}
          {comparable && <p className="quiet">{compareBlock ?? COMPARE_FOCUS_HINT}</p>}
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
            extra={comparable && (
              <button type="button" className="button secondary" disabled={busy || compareBlock !== null}
                onClick={() => onCompare({ documents: picked.map((d) => ({ id: d.id, filename: d.filename })), focus: question || null })}>
                Compare
              </button>
            )}
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

/** Titles are usually empty and many chats share a document, so each row also says when it started. */
function RecentChats({ onOpen }: { onOpen(id: ChatId): void }) {
  const { result, reload } = useFetched("recent-chats", api.recentChats);
  if (result === null || (result.ok && result.value.length === 0)) return null;
  return (
    <section className="recent" aria-labelledby="recent-chats-title">
      <h2 id="recent-chats-title">Recent chats</h2>
      {result.ok ? (
        <ul>
          {result.value.map((chat) => (
            <li key={chat.id}>
              <InAppLink to={{ kind: "chat", chat: chat.id, pane: null }} onFollow={() => onOpen(chat.id)}>{chatName(chat)}</InAppLink>
              <time dateTime={chat.createdAt.toISOString()}>{formatWhen(chat.createdAt)}</time>
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
  return chat.title ?? documentNames(chat.documents);
}

/** A 409 names the documents that are not ready; they are named here with their current status. */
function StartRefusal({ error, docs }: { error: ApiError; docs: readonly Doc[] }) {
  const names = notReadyNames(error, docs);
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
