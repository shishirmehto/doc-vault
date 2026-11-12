"use client";

import type { ReactNode } from "react";

const MAX_QUESTION = 2000;

export function Composer({ value, onChange, onSend, ready, placeholder, label, extra }: {
  value: string;
  onChange(v: string): void;
  onSend(): void;
  ready: boolean;
  placeholder: string;
  label: string;
  extra?: ReactNode;
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
