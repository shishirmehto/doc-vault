"use client";

/** "Find a document": filters a long list by filename or type as you type. */
export function FindField({ value, onChange }: { value: string; onChange(v: string): void }) {
  return (
    <label className="find">
      <span>Find a document</span>
      <input type="search" value={value} autoComplete="off" spellCheck={false} onChange={(e) => onChange(e.currentTarget.value)} />
    </label>
  );
}
