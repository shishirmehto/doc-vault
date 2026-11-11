"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { ChatDocument, ChatId, ChatPane, Citation, Doc, NonEmpty, Selection, Turn, TurnCite } from "@/api/domain";
import { useFetched } from "@/hooks/useFetched";
import { threads, useThread } from "@/hooks/useThread";
import { canAsk, type Ask, type Thread } from "@/lib/thread";
import { explain, listNames, NOT_FOUND, searching, streamingStatus, usageLine } from "@/copy";
import { Composer } from "./Composer";
import { SourcePane, type SourceView } from "./SourcePane";

type Props = {
  id: ChatId;
  docs: readonly Doc[] | null;
  pane: ChatPane | null;
  push(s: Selection): void;
  replace(s: Selection): void;
};

/** One chat: its turns oldest first, the question in flight, the composer, and the cited page beside them. */
export function ChatScreen({ id, docs, pane, push, replace }: Props) {
  const thread = useThread(id);
  const header = useFetched(`chat:${id}`, () => api.chat(id)).result;
  const documents = header?.ok ? header.value.documents : null;
  const deleted = documents?.filter((d) => d.deleted).map((d) => d.filename) ?? [];
  const accepting = deleted.length === 0;
  const [draft, setDraft] = useState("");
  const turns = thread?.history.kind === "loaded" ? thread.history.turns : null;
  const open = (cite: TurnCite) => push({ kind: "chat", chat: id, pane: { cite, page: null } });

  const column = useRef<HTMLElement>(null);
  const turnCount = turns?.length ?? 0;
  const phase = thread?.ask.phase;
  useEffect(() => {
    column.current?.scrollTo({ top: column.current.scrollHeight, behavior: "smooth" });
  }, [turnCount, phase]);

  const send = () => {
    if (!thread || !canAsk(thread) || !accepting || draft.trim() === "") return;
    void threads.ask(id, draft);
    setDraft("");
  };

  return (
    <>
      <main className="centre" ref={column}>
        <section className="chat" aria-label="Chat">
          <header className="chat-header">
            <h1>{documents ? `Asking ${listNames(documents.map((d) => (d.deleted ? `${d.filename} (deleted)` : d.filename)))}` : "Chat"}</h1>
            <p className="quiet">
              {!documents
                ? null
                : accepting
                  ? "Answers come only from these documents, and each one cites the page it used. "
                  : `${listNames(deleted)} ${deleted.length === 1 ? "was" : "were"} deleted, so this chat takes no more questions. Its answers still cite their pages. `}
              <button type="button" className="text-button" onClick={() => push({ kind: "none" })}>New chat</button>
            </p>
          </header>
          <Turns thread={thread} pane={pane} onOpen={open} accepting={accepting} />
          {thread && (
            <PendingTurn
              ask={thread.ask}
              scope={documents}
              onRetry={(question) => void threads.ask(id, question)}
              onEdit={(question) => {
                threads.dismiss(id);
                setDraft(question);
              }}
              onRecheck={() => void threads.recheck(id)}
            />
          )}
          <Composer
            value={draft}
            onChange={setDraft}
            onSend={send}
            ready={thread !== undefined && canAsk(thread) && accepting}
            label="Ask"
            placeholder={
              !accepting
                ? "Start a new chat to ask more"
                : documents
                  ? `Ask about ${documents.length === 1 ? documents[0].filename : `these ${documents.length} documents`}`
                  : "Ask a question"
            }
          />
        </section>
      </main>
      {pane && (
        <SourcePane
          view={sourceView(pane, turns, docs)}
          onPage={(page) => replace({ kind: "chat", chat: id, pane: { ...pane, page } })}
          onClose={() => push({ kind: "chat", chat: id, pane: null })}
        />
      )}
    </>
  );
}

/** The URL names a turn and a citation number; the loaded history and the document list resolve them. */
function sourceView(pane: ChatPane, turns: readonly Turn[] | null, docs: readonly Doc[] | null): SourceView {
  if (turns === null || docs === null) return { kind: "resolving" };
  const outcome = turns.find((t) => t.id === pane.cite.turn)?.outcome;
  const citation = outcome?.kind === "answered" ? outcome.citations.find((c) => c.ref === pane.cite.ref) : undefined;
  if (!citation) return { kind: "unknown", message: "This citation is not in the chat's history." };
  const doc = docs.find((d) => d.id === citation.source.documentId);
  if (!doc) return { kind: "unknown", message: `${citation.filename} is no longer in the vault.` };
  const page = pane.page ?? citation.source.page;
  const mark = page === citation.source.page ? { quote: citation.source.quote, label: `source ${citation.ref}`, flag: String(citation.ref) } : null;
  return { kind: "page", doc, page, mark, citedPage: citation.source.page };
}

function Turns({ thread, pane, onOpen, accepting }: {
  thread: Thread | undefined;
  pane: ChatPane | null;
  onOpen(cite: TurnCite): void;
  accepting: boolean;
}) {
  if (!thread || thread.history.kind === "loading") {
    return (
      <div className="turn" aria-busy="true">
        {[60, 92, 80].map((w) => <span key={w} className="skeleton line" style={{ width: `${w}%` }} />)}
      </div>
    );
  }
  if (thread.history.kind === "failed") {
    return <p className="notice" data-tone="failed">{explain(thread.history.error)}</p>;
  }
  if (thread.history.turns.length === 0 && thread.ask.phase === "idle") {
    return accepting ? <p className="quiet">Ask a question to start. Follow-ups can refer back to earlier answers.</p> : null;
  }
  return thread.history.turns.map((turn) => (
    <TurnView key={turn.id} turn={turn} active={pane?.cite.turn === turn.id ? pane.cite.ref : null} onOpen={onOpen} />
  ));
}

function TurnView({ turn, active, onOpen }: { turn: Turn; active: number | null; onOpen(cite: TurnCite): void }) {
  const cite = (c: Citation) => onOpen({ turn: turn.id, ref: c.ref });
  return (
    <article className="turn" aria-labelledby={`q-${turn.id}`}>
      <h2 className="question" id={`q-${turn.id}`}>{turn.question}</h2>
      {turn.readAs && <p className="read-as">Read as: {turn.readAs}</p>}
      {turn.outcome.kind === "answered" ? (
        <>
          <p className="answer">
            {turn.outcome.parts.map((part, i) =>
              part.kind === "text" ? (
                <span key={i}>{part.text}</span>
              ) : (
                <button key={i} type="button" className="cite" aria-pressed={active === part.citation.ref}
                  aria-label={`Source ${part.citation.ref}: ${part.citation.filename}, ${part.citation.pageLabel}`}
                  title={`${part.citation.filename}, ${part.citation.pageLabel}`} onClick={() => cite(part.citation)}>
                  {part.citation.ref}
                </button>
              ),
            )}
          </p>
          {turn.outcome.citations.length > 0 && (
            <ol className="sources" aria-label="Sources">
              {turn.outcome.citations.map((c) => (
                <li key={c.ref}>
                  <button type="button" className="source" aria-pressed={active === c.ref} onClick={() => cite(c)}>
                    <span className="source-ref">{c.ref}</span>
                    <span className="source-where">{c.filename}, {c.pageLabel}</span>
                    <span className="source-quote">{c.source.quote}</span>
                  </button>
                </li>
              ))}
            </ol>
          )}
        </>
      ) : (
        <div className="not-found">
          <p>{turn.outcome.message}</p>
          <p className="quiet">{NOT_FOUND[turn.outcome.reason]}</p>
        </div>
      )}
      <p className="usage">{usageLine(turn)}</p>
    </article>
  );
}

function PendingTurn({ ask, scope, onRetry, onEdit, onRecheck }: {
  ask: Ask;
  scope: NonEmpty<ChatDocument> | null;
  onRetry(question: string): void;
  onEdit(question: string): void;
  onRecheck(): void;
}) {
  switch (ask.phase) {
    case "idle":
      return null;
    case "asking":
      return (
        <article className="turn" aria-busy="true">
          <h2 className="question">{ask.question}</h2>
          <p className="quiet" role="status">{searching(scope?.length ?? null)}</p>
          <div className="progress indeterminate"><span /></div>
        </article>
      );
    case "streaming":
      return (
        <article className="turn" aria-busy="true">
          <h2 className="question">{ask.question}</h2>
          {/* Plain text, no citations yet: the grounded turn replaces it when it lands. */}
          {ask.text !== "" && <p className="answer provisional">{ask.text}</p>}
          <p className="quiet" role="status">{streamingStatus(ask.stage, ask.quiet, scope?.length ?? null)}</p>
          <div className="progress indeterminate"><span /></div>
        </article>
      );
    case "checking":
      return (
        <article className="turn" aria-busy="true">
          <h2 className="question">{ask.question}</h2>
          <p className="quiet" role="status">The answer did not arrive. Checking whether the question was recorded.</p>
        </article>
      );
    case "failed":
      return (
        <article className="turn ask-failed">
          <h2 className="question">{ask.question}</h2>
          <p role="alert">{explain(ask.error)}</p>
          <p className="actions">
            {/* A refusal the backend marks as final (a deleted document, a missing chat) would only fail again. */}
            {!(ask.error.kind === "refused" && ask.error.retryable === false) && (
              <button type="button" className="button" onClick={() => onRetry(ask.question)}>Try again</button>
            )}
            <button type="button" className="text-button" onClick={() => onEdit(ask.question)}>Edit the question</button>
          </p>
        </article>
      );
    case "unconfirmed":
      return (
        <article className="turn ask-failed">
          <h2 className="question">{ask.question}</h2>
          <p role="alert">
            {explain(ask.error)} The chat&apos;s history could not be read either, so it is not known whether the question
            was recorded.
          </p>
          <p className="actions">
            <button type="button" className="button" onClick={onRecheck}>Check again</button>
          </p>
        </article>
      );
    default: {
      const unknown: never = ask;
      return unknown;
    }
  }
}
