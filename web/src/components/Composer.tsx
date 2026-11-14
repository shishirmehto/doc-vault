"use client";

import type { ReactNode } from "react";
import { recalled } from "@/lib/recall";

const MAX_QUESTION = 2000;

export function Composer({ value, onChange, onSend, ready, placeholder, label, extra, recall }: {
  value: string;
  onChange(v: string): void;
  onSend(): void;
  ready: boolean;
  placeholder: string;
  label: string;
  extra?: ReactNode;
  /** Questions to recall, oldest first, each text once. */
  recall?: readonly string[];
}) {
  const empty = value.trim() === "";
  return (
    <form className="composer" onSubmit={(e) => {
      e.preventDefault();
      onSend();
    }}>
      <textarea value={value} rows={2} maxLength={MAX_QUESTION} placeholder={placeholder} aria-label="Question"
        onChange={(e) => onChange(e.currentTarget.value)}
        onKeyDown={(e) => {
          const to = e.key === "ArrowUp" ? "older" : e.key === "ArrowDown" ? "newer" : null;
          if (to && recall && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey && !e.nativeEvent.isComposing) {
            const { selectionStart: start, selectionEnd: end } = e.currentTarget;
            const next = recalled(recall, value, to, { start, end });
            if (next !== null) {
              e.preventDefault();
              onChange(next);
            }
          }
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            onSend();
          }
        }} />
      <div className="composer-actions">
        <button type="submit" className="button" disabled={!ready || empty}>{label}</button>
        {extra}
      </div>
    </form>
  );
}
