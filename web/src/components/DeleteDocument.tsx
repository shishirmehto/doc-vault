"use client";

import { useRef, useState } from "react";
import type { ApiError, Doc } from "@/api/domain";
import { explain, OTHER_VERSIONS_STAY } from "@/copy";

/** "Delete", then a modal that names the file and what happens to the chats that used it and to its other versions. */
export function DeleteDocument({ doc, otherVersions, onDelete }: {
  doc: Doc;
  otherVersions: boolean;
  onDelete(): Promise<ApiError | null>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const open = () => {
    setError(null);
    dialog.current?.showModal();
    cancel.current?.focus();
  };
  const confirm = async () => {
    setBusy(true);
    setError(null);
    const refused = await onDelete();
    if (refused) {
      setError(refused);
      setBusy(false);
    }
  };

  return (
    <>
      <button type="button" className="text-button quiet-action" onClick={open}>Delete</button>
      <dialog ref={dialog} className="confirm" aria-labelledby={`delete-${doc.id}`} aria-describedby={`delete-what-${doc.id}`}
        onCancel={(e) => { if (busy) e.preventDefault(); }}>
        <h2 id={`delete-${doc.id}`}>Delete {doc.filename}?</h2>
        <p id={`delete-what-${doc.id}`}>
          The file, its pages and its summary leave the vault. Chats that used it keep their answers and citations, but
          take no more questions.{otherVersions && ` ${OTHER_VERSIONS_STAY}`}
        </p>
        {error && <p className="notice" data-tone="failed" role="alert">{explain(error)}</p>}
        <p className="actions">
          <button type="button" className="button danger" disabled={busy} onClick={() => void confirm()}>
            {busy ? "Deleting" : "Delete"}
          </button>
          <button ref={cancel} type="button" className="text-button" disabled={busy} onClick={() => dialog.current?.close()}>
            Cancel
          </button>
        </p>
      </dialog>
    </>
  );
}
