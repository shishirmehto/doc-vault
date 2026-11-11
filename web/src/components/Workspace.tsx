"use client";

import { useCallback } from "react";
import { api } from "@/api/client";
import type { ApiError, Doc, DocumentId } from "@/api/domain";
import { useSelection } from "@/hooks/useSelection";
import { usePolled } from "@/hooks/usePolled";
import { useUploads } from "@/hooks/useUploads";
import { isSettled } from "@/lib/lifecycle";
import { threads } from "@/hooks/useThread";
import { Rail } from "./Rail";
import { ChatScreen } from "./ChatScreen";
import { DocumentScreen } from "./DocumentScreen";
import { NewChat } from "./NewChat";

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

  const docs = documents.data?.docs ?? null;
  const selectedDoc = selection.kind === "doc" ? selection.doc : null;
  const current = selectedDoc && docs ? (docs.find((d) => d.id === selectedDoc) ?? null) : null;
  const paneOpen = selection.kind === "chat" ? selection.pane !== null : current !== null && selection.kind === "doc" && selection.pane !== null;

  return (
    <div className="workspace" data-pane={paneOpen ? "open" : "closed"}>
      <Rail
        docs={docs}
        connection={documents.error}
        uploads={uploads.uploads}
        selected={selectedDoc}
        onFiles={uploads.add}
        onDismiss={uploads.dismiss}
        onOpen={(id) => push({ kind: "doc", doc: id, pane: null })}
        onRetry={retry}
      />
      {selection.kind === "none" && (
        <NewChat docs={docs} onStarted={(chat) => push({ kind: "chat", chat, pane: null })} />
      )}
      {selection.kind === "chat" && (
        <ChatScreen key={selection.chat} id={selection.chat} docs={docs} pane={selection.pane} push={push} replace={replace} />
      )}
      {selection.kind === "doc" && (
        <DocumentScreen
          key={selection.doc}
          id={selection.doc}
          doc={current}
          loading={docs === null}
          pane={selection.pane}
          push={push}
          replace={replace}
          onRetry={retry}
          onAsk={askAbout}
        />
      )}
    </div>
  );
}
