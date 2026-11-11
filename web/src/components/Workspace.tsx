"use client";

import { startTransition, useCallback, useState } from "react";
import { api } from "@/api/client";
import type { ApiError, Doc, DocumentId, Result } from "@/api/domain";
import { useSelection } from "@/hooks/useSelection";
import { usePolled } from "@/hooks/usePolled";
import { useUploads } from "@/hooks/useUploads";
import { useFetched } from "@/hooks/useFetched";
import { useHealth } from "@/hooks/useHealth";
import { isSettled } from "@/lib/lifecycle";
import { threads } from "@/hooks/useThread";
import { Rail } from "./Rail";
import { ChatScreen } from "./ChatScreen";
import { DocumentScreen } from "./DocumentScreen";
import { NewChat } from "./NewChat";
import { Metrics } from "./Metrics";
import { MissingChat, MissingDocument } from "./Missing";

const POLL_MS = 1000;

type DocList = { docs: Doc[]; complete: boolean };

/** Puts a document the server just returned into the list: in place if known, newest first if not. */
function upsert(doc: Doc) {
  return (list: DocList): DocList => ({
    ...list,
    docs: list.docs.some((d) => d.id === doc.id) ? list.docs.map((d) => (d.id === doc.id ? doc : d)) : [doc, ...list.docs],
  });
}

/**
 * Three columns: your documents; a chat, or what the system says about one document; and what the
 * document itself says. The URL holds the selection; the document list is polled until nothing in it
 * can still change.
 */
export function Workspace() {
  const { selection, push, replace } = useSelection();
  const documents = usePolled(api.documents, (list) => (isSettled(list.docs) ? null : POLL_MS));
  const { patch, refresh } = documents;
  const uploads = useUploads((doc) => patch(upsert(doc)));
  const health = useHealth();
  const uploadLimit = health?.ok ? health.value.maxUploadBytes : null;

  const retry = useCallback(
    async (id: DocumentId): Promise<ApiError | null> => {
      const result = await api.retry(id);
      if (result.ok) {
        patch(upsert(result.value));
        return null;
      }
      refresh();
      return result.error.kind === "refused" && result.error.code === "nothing_to_retry" ? null : result.error;
    },
    [patch, refresh],
  );

  const askAbout = useCallback(
    async (id: DocumentId): Promise<ApiError | null> => {
      const result = await threads.start([id], null);
      if (!result.ok) return result.error;
      push({ kind: "chat", chat: result.value.id, pane: null });
      return null;
    },
    [push],
  );

  // Shown on the home screen after a delete, until the reader dismisses it or leaves the home screen.
  const [deleted, setDeleted] = useState<string | null>(null);
  const [screen, setScreen] = useState(selection.kind);
  if (screen !== selection.kind) {
    setScreen(selection.kind);
    if (selection.kind !== "none") setDeleted(null);
  }

  const remove = useCallback(
    async (doc: Doc): Promise<ApiError | null> => {
      const result = await api.deleteDocument(doc.id);
      if (!result.ok && !(result.error.kind === "refused" && result.error.status === 404)) return result.error;
      // Replace, so Back does not land on the deleted document. Next applies the URL in a transition, so the
      // list changes in one too; otherwise one frame shows the document page with its document gone.
      replace({ kind: "none" });
      startTransition(() => {
        patch((list) => ({ ...list, docs: list.docs.filter((d) => d.id !== doc.id) }));
        setDeleted(doc.filename);
      });
      return null;
    },
    [patch, replace],
  );

  const docs = documents.data?.docs ?? null;
  const complete = documents.data?.complete ?? true;
  const selectedDoc = selection.kind === "doc" ? selection.doc : null;
  const listed = selectedDoc && docs ? (docs.find((d) => d.id === selectedDoc) ?? null) : null;
  // The list holds the newest 100. A link to an older document reads that one document instead.
  const lookUp = selectedDoc !== null && docs !== null && !complete && listed === null ? selectedDoc : null;
  const older = useFetched(`older:${lookUp}`, (): Promise<Result<Doc | null>> => (lookUp ? api.document(lookUp) : Promise.resolve({ ok: true, value: null })));
  const current = listed ?? (older.result?.ok ? older.result.value : null);
  const paneOpen = selection.kind === "chat" ? selection.pane !== null : current !== null && selection.kind === "doc" && selection.pane !== null;

  return (
    <div className="workspace" data-pane={paneOpen ? "open" : "closed"}>
      <Rail
        docs={docs}
        complete={complete}
        connection={documents.error}
        uploads={uploads.uploads}
        selected={selectedDoc}
        onFiles={(files) => uploads.add(files, uploadLimit)}
        onDismiss={uploads.dismiss}
        onOpen={(id) => push({ kind: "doc", doc: id, pane: null })}
        onRetry={retry}
      />
      {selection.kind === "none" && (
        <NewChat docs={docs} failed={documents.error} deleted={deleted} onDismiss={() => setDeleted(null)}
          onFiles={(files) => uploads.add(files, uploadLimit)} onOpen={(chat) => push({ kind: "chat", chat, pane: null })} />
      )}
      {selection.kind === "missing" && (
        <main className="centre">{selection.what === "doc" ? <MissingDocument /> : <MissingChat />}</main>
      )}
      {selection.kind === "metrics" && (
        <Metrics window={selection.window} onWindow={(window) => replace({ kind: "metrics", window })} />
      )}
      {selection.kind === "chat" && (
        <ChatScreen key={selection.chat} id={selection.chat} docs={docs} pane={selection.pane} push={push} replace={replace} />
      )}
      {selection.kind === "doc" && (
        <DocumentScreen
          key={selection.doc}
          id={selection.doc}
          doc={current}
          loading={docs === null || (lookUp !== null && older.result === null)}
          pane={selection.pane}
          push={push}
          replace={replace}
          onRetry={retry}
          onAsk={askAbout}
          onDelete={remove}
        />
      )}
    </div>
  );
}
