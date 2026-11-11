"use client";

import { useState } from "react";
import type { ApiError, Doc, DocumentId, Failure } from "@/api/domain";
import { selectionQuery } from "@/api/parse";
import { useFileDrop } from "@/hooks/useFileDrop";
import { FIND_AFTER, findDocs } from "@/lib/find";
import type { Upload } from "@/lib/uploads";
import { docMeta, explain, formatBytes, statusLine } from "@/copy";
import { DropZone } from "./DropZone";
import { FindField } from "./FindField";
import { StageBar } from "./StageBar";

type Props = {
  docs: readonly Doc[] | null;
  /** False when the vault holds more documents than the list fetched. */
  complete: boolean;
  connection: ApiError | null;
  uploads: readonly Upload[];
  selected: DocumentId | null;
  onFiles(files: File[]): void;
  onDismiss(key: string): void;
  onOpen(id: DocumentId): void;
  onRetry(id: DocumentId): Promise<ApiError | null>;
};

/**
 * The documents column. The whole rail takes dropped files. Below 760px it folds into a "Documents (n)"
 * button over the centre column: open while the vault is empty or uploads are listed, so the upload target
 * and their progress show, and closed once a document is opened from it.
 */
export function Rail({ docs, complete, connection, uploads, selected, onFiles, onDismiss, onOpen, onRetry }: Props) {
  const drop = useFileDrop(onFiles);
  const [query, setQuery] = useState("");
  const [choice, setChoice] = useState<boolean | null>(null);
  // A new upload hands the fold back to the default, which is open while uploads are listed.
  const [uploadCount, setUploadCount] = useState(uploads.length);
  if (uploads.length !== uploadCount) {
    setUploadCount(uploads.length);
    if (uploads.length > uploadCount) setChoice(null);
  }
  const open = choice ?? (docs !== null && (docs.length === 0 || uploads.length > 0));
  const finding = docs !== null && docs.length > FIND_AFTER;
  const shown = docs && finding ? findDocs(docs, query) : docs;
  const openDoc = (id: DocumentId) => {
    setChoice(false);
    onOpen(id);
  };

  return (
    <aside className="rail" aria-label="Documents" data-open={open} data-dragging={drop.dragging || undefined} {...drop.handlers}>
      <button type="button" className="rail-toggle" aria-expanded={open} aria-controls="rail-body" onClick={() => setChoice(!open)}>
        Documents{docs ? ` (${docs.length})` : ""}
      </button>
      {/* Outside the fold, so a phone shows it with the list folded away. */}
      {connection && <p className="rail-note rail-alert" data-tone="failed" role="alert">{explain(connection)} Retrying.</p>}
      <div className="rail-body" id="rail-body">
        <DropZone dragging={drop.dragging} onFiles={onFiles} />
        {docs === null && !connection && <RailSkeleton />}
        {finding && <FindField label="Find a document" value={query} onChange={setQuery} />}
        <ul className="rows">
          {uploads.map((u) => (
            <UploadRow key={u.key} upload={u} onDismiss={() => onDismiss(u.key)} onOpen={openDoc} />
          ))}
          {shown?.map((doc) => (
            <DocumentRow key={doc.id} doc={doc} current={doc.id === selected} onOpen={openDoc} onRetry={onRetry} />
          ))}
        </ul>
        {!complete && docs && (
          <p className="rail-note">The newest {docs.length} documents are listed. Older ones are in the vault but not shown here.</p>
        )}
        {finding && shown?.length === 0 && (
          <p className="rail-note">
            No document matches &ldquo;{query.trim()}&rdquo;.{" "}
            <button type="button" className="text-button" onClick={() => setQuery("")}>Clear</button>
          </p>
        )}
      </div>
    </aside>
  );
}

function DocLink({ id, onOpen, children }: { id: DocumentId; onOpen(id: DocumentId): void; children: string }) {
  return (
    <a
      className="row-name"
      href={selectionQuery({ kind: "doc", doc: id, pane: null })}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        onOpen(id);
      }}
    >
      {children}
    </a>
  );
}

function UploadRow({ upload, onDismiss, onOpen }: { upload: Upload; onDismiss(): void; onOpen(id: DocumentId): void }) {
  switch (upload.phase) {
    case "waiting":
      return (
        <li className="row">
          <span className="row-name">{upload.filename}</span>
          <span className="row-status" data-tone="quiet">Waiting to upload</span>
        </li>
      );
    case "sending": {
      const pct = upload.total > 0 ? Math.round((upload.sent / upload.total) * 100) : 0;
      return (
        <li className="row">
          <span className="row-name">{upload.filename}</span>
          <span className="row-status" data-tone="working">Uploading {pct}%</span>
          <div className="progress" role="progressbar" aria-label={`Uploading ${upload.filename}`} aria-valuenow={pct}>
            <span style={{ width: `${pct}%` }} />
          </div>
        </li>
      );
    }
    case "rejected":
      return (
        <li className="row">
          <span className="row-name">{upload.filename}</span>
          <span className="row-status" data-tone="failed">Not uploaded</span>
          <p className="row-message">{explain(upload.error)}</p>
          <button type="button" className="text-button" onClick={onDismiss}>Dismiss</button>
        </li>
      );
    case "too_large":
      return (
        <li className="row">
          <span className="row-name">{upload.filename}</span>
          <span className="row-status" data-tone="failed">Not uploaded</span>
          <p className="row-message">
            The file is larger than the vault&apos;s {formatBytes(upload.limitBytes)} limit, so it was not sent.
          </p>
          <button type="button" className="text-button" onClick={onDismiss}>Dismiss</button>
        </li>
      );
    case "duplicate":
      return (
        <li className="row">
          <DocLink id={upload.doc.id} onOpen={onOpen}>{upload.filename}</DocLink>
          <span className="row-status" data-tone="quiet">Already in the vault. Nothing was reprocessed.</span>
          <button type="button" className="text-button" onClick={onDismiss}>Dismiss</button>
        </li>
      );
  }
}

function DocumentRow({ doc, current, onOpen, onRetry }: {
  doc: Doc;
  current: boolean;
  onOpen(id: DocumentId): void;
  onRetry(id: DocumentId): Promise<ApiError | null>;
}) {
  const status = statusLine(doc);
  return (
    <li className="row" aria-current={current || undefined}>
      <DocLink id={doc.id} onOpen={onOpen}>{doc.filename}</DocLink>
      <span className="row-meta">{docMeta(doc)}</span>
      <span className="row-status" data-tone={status.tone} aria-live="polite">{status.text}</span>
      <StageBar lifecycle={doc.lifecycle} />
      {doc.lifecycle.status === "failed" && (
        <FailureNote failure={doc.lifecycle.failure} onRetry={() => onRetry(doc.id)} />
      )}
    </li>
  );
}

/** The backend's reason, verbatim. A non-retryable failure still allows a quiet "Retry anyway". */
export function FailureNote({ failure, onRetry }: { failure: Failure; onRetry(): Promise<ApiError | null> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const retry = async () => {
    setBusy(true);
    setError(await onRetry());
    setBusy(false);
  };
  return (
    <div className="failure">
      <p className="row-message">{failure.message}</p>
      {error && <p className="row-message" data-tone="failed">{explain(error)}</p>}
      <button
        type="button"
        className={failure.retryable ? "button" : "text-button"}
        disabled={busy}
        onClick={() => void retry()}
      >
        {busy ? "Retrying" : failure.retryable ? "Retry" : "Retry anyway"}
      </button>
    </div>
  );
}

function RailSkeleton() {
  return (
    <ul className="rows" aria-hidden="true">
      {[0, 1, 2].map((i) => (
        <li key={i} className="row skeleton-row">
          <span className="skeleton" style={{ width: "70%" }} />
          <span className="skeleton" style={{ width: "40%" }} />
        </li>
      ))}
    </ul>
  );
}
