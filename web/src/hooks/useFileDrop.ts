"use client";

import { useState, type DragEvent } from "react";

export function useFileDrop(onFiles: (files: File[]) => void) {
  const [dragging, setDragging] = useState(false);
  return {
    dragging,
    handlers: {
      onDragOver: (e: DragEvent) => {
        e.preventDefault();
        setDragging(true);
      },
      onDragLeave: (e: DragEvent) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false);
      },
      onDrop: (e: DragEvent) => {
        e.preventDefault();
        setDragging(false);
        if (e.dataTransfer.files.length > 0) onFiles([...e.dataTransfer.files]);
      },
    },
  };
}
