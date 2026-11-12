"use client";

import { useRef, type ReactNode, type Ref } from "react";

const ACCEPT = ".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain";

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
      <FileInput ref={input} multiple onFiles={onFiles} />
    </div>
  );
}

export function FileInput({ ref, multiple = false, onFiles }: { ref: Ref<HTMLInputElement>; multiple?: boolean; onFiles(files: File[]): void }) {
  return (
    <input
      ref={ref}
      type="file"
      multiple={multiple}
      accept={ACCEPT}
      hidden
      onChange={(e) => {
        const files = [...(e.currentTarget.files ?? [])];
        e.currentTarget.value = "";
        if (files.length > 0) onFiles(files);
      }}
    />
  );
}
