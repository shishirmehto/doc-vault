"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type {
  ApiError, ChatDocument, ChatId, ChatPane, Citation, Doc, NonEmpty, Selection, Turn, TurnCite,
} from "@/api/domain";
import { useChatDocs } from "@/hooks/useChatDocs";
import { useFetched } from "@/hooks/useFetched";
import { useListedDoc } from "@/hooks/useListedDoc";
import { threads, useThread } from "@/hooks/useThread";
import { useWorthRetrying } from "@/hooks/useWorthRetrying";
import { canAsk, offered, type Ask, type Thread } from "@/lib/thread";
import { chatVersion } from "@/lib/versions";
import { askStatus, chatAsks, documentNames, explain, listNames, NOT_FOUND, usageLine, withVersion } from "@/copy";
import { Composer } from "./Composer";
import { MissingChat } from "./Missing";
import { SourcePane, type SourceView } from "./SourcePane";
import { NewerVersionNote } from "./Versions";

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
  const { result: header, reload: reloadHeader } = useFetched(`chat:${id}`, () => api.chat(id));
  const documents = header?.ok ? header.value.documents : null;
  const deleted = documents?.filter((d) => d.deleted).map((d) => d.filename) ?? [];
  const accepting = deleted.length === 0;
  const asked = useChatDocs(documents, docs).map(({ asked, now }) => ({ asked, now, version: chatVersion(now, docs) }));
  const named = documents && asked.map((a) => ({ ...a.asked, filename: withVersion(a.asked.filename, a.version?.number ?? null) }));
  const [draft, setDraft] = useState("");
  const turns = thread?.history.kind === "loaded" ? thread.history.turns : null;
  const open = (cite: TurnCite) => push({ kind: "chat", chat: id, pane: { cite, page: null } });

  const cited = pane && turns ? citationAt(turns, pane.cite) : null;
  const { doc: citedDoc, loading: resolving } = useListedDoc(cited?.source.documentId ?? null, docs);

  const column = useRef<HTMLDivElement>(null);
  const turnCount = turns?.length ?? 0;
  const phase = thread?.ask.phase;
  useEffect(() => {
    column.current?.scrollTo({ top: column.current.scrollHeight, behavior: "smooth" });
  }, [turnCount, phase]);
  // A streaming answer grows below the fold, so the column follows the text from the moment a question is sent
  // until the reader scrolls up.
  const following = useRef(false);
  const streamed = thread?.ask.phase === "streaming" ? thread.ask.text.length : 0;
  useEffect(() => {
    if (phase === "asking") following.current = true;
  }, [phase]);
  useEffect(() => {
    if (streamed > 0 && following.current) column.current?.scrollTo({ top: column.current.scrollHeight });
  }, [streamed]);

  const reload = () => {
    threads.reload(id);
    reloadHeader();
  };

  if (header && !header.ok && header.error.kind === "refused" && header.error.status === 404) {
    return <main className="centre"><MissingChat /></main>;
  }

  const send = () => {
    if (!thread || !canAsk(thread) || !accepting || draft.trim() === "") return;
    void threads.ask(id, draft);
    setDraft("");
  };

  return (
    <>
      <main className="centre with-composer">
        <div className="centre-scroll" ref={column} onWheel={(e) => { if (e.deltaY < 0) following.current = false; }}
          onTouchMove={() => { following.current = false; }}>
          <section className="chat" aria-label="Chat">
            <header className="chat-header">
              <h1>{named ? `Asking ${documentNames(named)}` : "Chat"}</h1>
              <p className="quiet">
                {!documents
                  ? null
                  : accepting
                    ? "Answers come only from these documents, and each one cites the page it used. "
                    : `${listNames(deleted)} ${deleted.length === 1 ? "was" : "were"} deleted, so this chat takes no more questions. Its answers still cite their pages. `}
                <button type="button" className="text-button" onClick={() => push({ kind: "none" })}>New chat</button>
              </p>
              {asked.map(({ now, version }) => now && version?.newer && (
                <NewerVersionNote key={now.id} lead={chatAsks(now)} doc={now} newest={version.newer.newest}
                  onOpen={(doc) => push({ kind: "doc", doc, pane: null })} />
              ))}
            </header>
            <Turns thread={thread} pane={pane} onOpen={open} accepting={accepting} onReload={reload}
              onSuggest={(question) => void threads.ask(id, question)} />
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
          </section>
        </div>
        <div className="centre-footer">
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
        </div>
      </main>
      {pane && (
        <SourcePane
          view={sourceView(pane, turns, citedDoc, resolving)}
          onPage={(page) => replace({ kind: "chat", chat: id, pane: { ...pane, page } })}
          onClose={() => push({ kind: "chat", chat: id, pane: null })}
        />
      )}
    </>
  );
}

function citationAt(turns: readonly Turn[], cite: TurnCite): Citation | null {
  const outcome = turns.find((t) => t.id === cite.turn)?.outcome;
  return (outcome?.kind === "answered" ? outcome.citations.find((c) => c.ref === cite.ref) : undefined) ?? null;
}

function sourceView(pane: ChatPane, turns: readonly Turn[] | null, doc: Doc | null, resolving: boolean): SourceView {
  if (turns === null || resolving) return { kind: "resolving" };
  const citation = citationAt(turns, pane.cite);
  if (!citation) return { kind: "unknown", message: "This citation is not in the chat's history." };
  if (!doc) return { kind: "unknown", message: `${citation.filename} is no longer in the vault.` };
  const page = pane.page ?? citation.source.page;
  const mark = page === citation.source.page ? { quote: citation.source.quote, label: `source ${citation.ref}`, flag: String(citation.ref) } : null;
  return { kind: "page", doc, page, mark, citedPage: citation.source.page };
}

function Turns({ thread, pane, onOpen, accepting, onReload, onSuggest }: {
  thread: Thread | undefined;
  pane: ChatPane | null;
  onOpen(cite: TurnCite): void;
  accepting: boolean;
  onReload(): void;
  onSuggest(question: string): void;
}) {
  if (!thread || thread.history.kind === "loading") {
    return (
      <div className="turn" aria-busy="true">
        {[60, 92, 80].map((w) => <span key={w} className="skeleton line" style={{ width: `${w}%` }} />)}
      </div>
    );
  }
  if (thread.history.kind === "failed") {
    return (
      <div className="load-failed">
        <p className="notice" data-tone="failed" role="alert">{explain(thread.history.error)}</p>
        <button type="button" className="button" onClick={onReload}>Try again</button>
      </div>
    );
  }
  if (thread.history.turns.length === 0 && thread.ask.phase === "idle") {
    return accepting ? <p className="quiet">Ask a question to start. Follow-ups can refer back to earlier answers.</p> : null;
  }
  const suggestions = accepting ? offered(thread) : [];
  return (
    <>
      {thread.history.turns.map((turn) => (
        <TurnView key={turn.id} turn={turn} active={pane?.cite.turn === turn.id ? pane.cite.ref : null} onOpen={onOpen} />
      ))}
      {suggestions.length > 0 && (
        <div className="suggestions" role="group" aria-label="Suggested questions">
          {suggestions.map((question) => (
            <button key={question} type="button" className="suggestion" aria-label={`Ask: ${question}`}
              onClick={() => onSuggest(question)}>
              {question}
            </button>
          ))}
        </div>
      )}
    </>
  );
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
          <p className="quiet" role="status">{askStatus(ask, scope?.length ?? null)}</p>
          <div className="progress indeterminate"><span /></div>
        </article>
      );
    case "streaming":
      return (
        <article className="turn" aria-busy="true">
          <h2 className="question">{ask.question}</h2>
          {ask.text !== "" && <p className="answer provisional">{ask.text}</p>}
          <p className="quiet" role="status">{askStatus(ask, scope?.length ?? null)}</p>
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
      return <FailedAsk question={ask.question} error={ask.error} onRetry={onRetry} onEdit={onEdit} />;
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

/**
 * The question was not recorded. Try again is left out for a refusal that would only fail again: a final one (a
 * deleted document, a missing chat), or a used-up spending cap until it resets, when Try again comes back by itself.
 */
function FailedAsk({ question, error, onRetry, onEdit }: {
  question: string;
  error: ApiError;
  onRetry(question: string): void;
  onEdit(question: string): void;
}) {
  const retry = useWorthRetrying(error);
  return (
    <article className="turn ask-failed">
      <h2 className="question">{question}</h2>
      <p role="alert">{explain(error)}</p>
      <p className="actions">
        {retry && <button type="button" className="button" onClick={() => onRetry(question)}>Try again</button>}
        <button type="button" className="text-button" onClick={() => onEdit(question)}>Edit the question</button>
      </p>
    </article>
  );
}
