"use client";

import { startTransition, useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "@/api/client";
import type { ApiError, Doc, DocumentId } from "@/api/domain";
import { parseSelection } from "@/api/parse";
import { useSelection } from "@/hooks/useSelection";
import { useDocuments } from "@/hooks/useDocuments";
import { liveFeed } from "@/hooks/useLiveFeed";
import { useUploads } from "@/hooks/useUploads";
import { useListedDoc } from "@/hooks/useListedDoc";
import { useHealth } from "@/hooks/useHealth";
import { applyDocEvent, upsertDoc } from "@/lib/docList";
import { threads } from "@/hooks/useThread";
import { compareRuns } from "@/hooks/useCompareRun";
import type { CompareRequest } from "@/lib/compare";
import { Rail } from "./Rail";
import { ChatScreen } from "./ChatScreen";
import { CompareScreen } from "./CompareScreen";
import { ComparisonScreen } from "./ComparisonScreen";
import { DocumentScreen } from "./DocumentScreen";
import { NewChat } from "./NewChat";
import { Metrics } from "./Metrics";
import { MissingChat, MissingComparison, MissingDocument } from "./Missing";

/**
 * Three columns: your documents; a chat, a comparison, or what the system says about one document; and what
 * the document itself says. The URL holds the selection; the document list follows the live feed of document
 * changes.
 */
export function Workspace() {
  const { selection, push, replace } = useSelection();
  const documents = useDocuments();
  const { patch, refresh } = documents;
  const uploads = useUploads((doc) => patch((list) => upsertDoc(list, doc)));
  const health = useHealth();
  const uploadLimit = health?.ok ? health.value.maxUploadBytes : null;

  const retry = useCallback(
    async (id: DocumentId): Promise<ApiError | null> => {
      const result = await api.retry(id);
      if (result.ok) {
        patch((list) => upsertDoc(list, result.value));
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

  // A run outlives the screen that started it. On success the compare screen gives way to the comparison, and
  // replace means Back returns to where Compare was clicked; a reader who has moved on stays where they are.
  const compare = useCallback(
    (request: CompareRequest) =>
      void compareRuns.start(request, (comparison) => {
        if (parseSelection(new URLSearchParams(window.location.search)).kind !== "compare") return;
        replace({ kind: "comparison", comparison: comparison.id, pane: null, only: false });
      }),
    [replace],
  );
  const compareAndShow = (request: CompareRequest) => {
    compare(request);
    push({ kind: "compare", with: null });
  };

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
      // Next applies the URL in a transition, so the
      // list changes in one too; otherwise one frame shows the document page with its document gone.
      replace({ kind: "none" });
      startTransition(() => {
        patch((list) => applyDocEvent(list, { kind: "deleted", id: doc.id }));
        setDeleted(doc.filename);
      });
      return null;
    },
    [patch, replace],
  );

  const docs = documents.data?.docs ?? null;
  const complete = documents.data?.complete ?? true;
  const selectedDoc = selection.kind === "doc" ? selection.doc : null;
  const { doc: current, loading, lookUp, reload } = useListedDoc(selectedDoc, docs);
  useEffect(() => {
    if (lookUp === null) return;
    return liveFeed.subscribe((event) => {
      if (event.kind === "down") return;
      if (event.kind === "document" && event.doc.id !== lookUp) return;
      if (event.kind === "deleted" && event.id !== lookUp) return;
      reload();
    });
  }, [lookUp, reload]);
  const paneOpen = selection.kind === "chat" || selection.kind === "comparison"
    ? selection.pane !== null
    : current !== null && selection.kind === "doc" && selection.pane !== null;

  const centre = (): ReactNode => {
    switch (selection.kind) {
      case "none":
        return (
          <NewChat docs={docs} failed={documents.error} deleted={deleted} onDismiss={() => setDeleted(null)}
            onFiles={(files) => uploads.add(files, uploadLimit)} onOpen={(chat) => push({ kind: "chat", chat, pane: null })}
            onCompare={compareAndShow} />
        );
      case "missing":
        return <main className="centre">{{ doc: <MissingDocument />, chat: <MissingChat />, comparison: <MissingComparison /> }[selection.what]}</main>;
      case "metrics":
        return <Metrics window={selection.window} onWindow={(window) => replace({ kind: "metrics", window })} />;
      case "chat":
        return <ChatScreen key={selection.chat} id={selection.chat} docs={docs} pane={selection.pane} push={push} replace={replace} />;
      case "compare":
        return (
          <CompareScreen key={selection.with} docs={docs} failed={documents.error} initial={selection.with} onCompare={compare}
            onOpen={(comparison) => push({ kind: "comparison", comparison, pane: null, only: false })} />
        );
      case "comparison":
        return (
          <ComparisonScreen key={selection.comparison} id={selection.comparison} docs={docs} pane={selection.pane}
            only={selection.only} push={push} replace={replace} />
        );
      case "doc":
        return (
          <DocumentScreen
            key={selection.doc}
            id={selection.doc}
            doc={current}
            loading={loading}
            pane={selection.pane}
            push={push}
            replace={replace}
            onRetry={retry}
            onAsk={askAbout}
            onDelete={remove}
            onCompareVersions={compareAndShow}
            onUploadVersion={(files) => uploads.add(files, uploadLimit, selection.doc)}
          />
        );
      default: {
        const unknown: never = selection;
        return unknown;
      }
    }
  };

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
      {centre()}
    </div>
  );
}
