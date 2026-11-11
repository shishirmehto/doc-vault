"use client";

import { useRef, useState, type DragEvent } from "react";
import type { ApiError, Doc, DocumentId, Failure } from "@/api/domain";
import { selectionQuery } from "@/api/parse";
import type { Upload } from "@/lib/uploads";
import { docMeta, explain, formatBytes, statusLine } from "@/copy";
import { StageBar } from "./StageBar";

type Props = {
  docs: readonly Doc[] | null;
  connection: ApiError | null;
  uploads: readonly Upload[];
  selected: DocumentId | null;
  onFiles(files: File[]): void;
  onDismiss(key: string): void;
  onOpen(id: DocumentId): void;
  onRetry(id: DocumentId): Promise<ApiError | null>;
};

export function Rail({ docs, connection, uploads, selected, onFiles, onDismiss, onOpen, onRetry }: Props) {
  const [dragging, setDragging] = useState(false);
  const empty = docs !== null && docs.length === 0 && uploads.length === 0;

  const drop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length > 0) onFiles([...e.dataTransfer.files]);
  };

  return (
    <aside
      className="rail"
      aria-label="Documents"
      data-dragging={dragging || undefined}
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={drop}
    >
      <DropZone empty={empty} dragging={dragging} onFiles={onFiles} />
      {connection && <p className="rail-note" data-tone="failed">{explain(connection)} Retrying.</p>}
      {docs === null && !connection && <RailSkeleton />}
      <ul className="rows">
        {uploads.map((u) => (
          <UploadRow key={u.key} upload={u} onDismiss={() => onDismiss(u.key)} onOpen={onOpen} />
        ))}
        {docs?.map((doc) => (
          <DocumentRow key={doc.id} doc={doc} current={doc.id === selected} onOpen={onOpen} onRetry={onRetry} />
        ))}
      </ul>
    </aside>
  );
}

function DropZone({ empty, dragging, onFiles }: { empty: boolean; dragging: boolean; onFiles(files: File[]): void }) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <div className="dropzone" data-empty={empty || undefined}>
      <p className="dropzone-lead">{dragging ? "Drop to upload" : "Drop PDF, Word or text files here"}</p>
      <button type="button" className="button" onClick={() => input.current?.click()}>
        Choose files
      </button>
      {empty && <p className="dropzone-hint">The repo&apos;s samples folder has three insurance documents to try.</p>}
      <input
        ref={input}
        type="file"
        multiple
        accept=".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain"
        hidden
        onChange={(e) => {
          const files = [...(e.currentTarget.files ?? [])];
          e.currentTarget.value = "";
          if (files.length > 0) onFiles(files);
        }}
      />
    </div>
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
            The file is {formatBytes(upload.sizeBytes)}, over the vault&apos;s {formatBytes(upload.limitBytes)} limit, so it was not sent.
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
