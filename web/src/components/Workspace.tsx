"use client";

import { useCallback } from "react";
import { api } from "@/api/client";
import type { ApiError, Doc, DocumentId } from "@/api/domain";
import { useSelection } from "@/hooks/useSelection";
import { usePolled } from "@/hooks/usePolled";
import { useUploads } from "@/hooks/useUploads";
import { isSettled } from "@/lib/lifecycle";
import { Rail } from "./Rail";
import { DocumentScreen } from "./DocumentScreen";

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
 * Three columns: your documents, what the system says about one, and what the document itself says.
 * The URL holds the selection; the document list is polled until nothing in it can still change.
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

  const docs = documents.data?.docs ?? null;
  const current = selection.doc && docs ? (docs.find((d) => d.id === selection.doc) ?? null) : null;

  return (
    <div className="workspace" data-pane={current && selection.pane ? "open" : "closed"}>
      <Rail
        docs={docs}
        connection={documents.error}
        uploads={uploads.uploads}
        selected={selection.doc}
        onFiles={uploads.add}
        onDismiss={uploads.dismiss}
        onOpen={(id) => push({ doc: id, pane: null })}
        onRetry={retry}
      />
      {selection.doc === null ? (
        <main className="centre">
          <div className="welcome">
            <h1>{docs !== null && docs.length === 0 ? "Upload a document to start" : "Choose a document"}</h1>
            <p>
              Each document gets a summary and key facts. Every fact links to the page it came from, with the
              passage marked, so you can check it against the source.
            </p>
          </div>
        </main>
      ) : (
        <DocumentScreen
          key={selection.doc}
          id={selection.doc}
          doc={current}
          loading={docs === null}
          pane={selection.pane}
          push={push}
          replace={replace}
          onRetry={retry}
        />
      )}
    </div>
  );
}
