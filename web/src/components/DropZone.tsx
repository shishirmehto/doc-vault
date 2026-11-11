"use client";

import { useRef, type ReactNode } from "react";

const ACCEPT = ".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain";

/** The upload target: what to drop, and a button that opens the file picker. The caller handles the drop. */
export function DropZone({ dragging, onFiles, large = false, children }: {
  dragging: boolean;
  onFiles(files: File[]): void;
  large?: boolean;
  children?: ReactNode;
}) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <div className="dropzone" data-large={large || undefined} data-dragging={dragging || undefined}>
      <p className="dropzone-lead">{dragging ? "Drop to upload" : "Drop PDF, Word or text files here"}</p>
      <button type="button" className="button" onClick={() => input.current?.click()}>
        Choose files
      </button>
      {children}
      <input
        ref={input}
        type="file"
        multiple
        accept={ACCEPT}
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
