"use client";

/** Filters a long list by filename or type as you type. */
export function FindField({ label, value, onChange }: { label: string; value: string; onChange(v: string): void }) {
  return (
    <label className="find">
      <span>{label}</span>
      <input type="search" value={value} autoComplete="off" spellCheck={false} onChange={(e) => onChange(e.currentTarget.value)} />
    </label>
  );
}
