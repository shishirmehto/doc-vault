"use client";

import { useState } from "react";
import type { Doc, DocumentId } from "@/api/domain";
import { FIND_AFTER, allChosen, chooseAll, findDocs } from "@/lib/find";
import { laterVersion } from "@/lib/versions";
import { docMeta, statusLine } from "@/copy";
import { FindField } from "./FindField";
import { VersionMark } from "./VersionMark";

/**
 * How many documents can be ticked. A chat offers "Select all ready" and stops it at `max`; a comparison disables the
 * unticked rows once `max` are ticked, and says why in `note`.
 */
export type PickLimit = { kind: "select-all"; max: number } | { kind: "hard"; max: number; note: string };

/** The ready documents to tick, with a find field over a long list. Only ready documents can be ticked. */
export function Picker({ docs, chosen, onChange, findLabel, limit, noneReady, busy }: {
  docs: readonly Doc[];
  chosen: ReadonlySet<DocumentId>;
  onChange(next: ReadonlySet<DocumentId>): void;
  /** Each screen names its own field, so no two fields on screen share a name. */
  findLabel: string;
  limit: PickLimit;
  /** Shown while no document is ready. */
  noneReady: string;
  busy: boolean;
}) {
  const [query, setQuery] = useState("");
  const [capped, setCapped] = useState(false);

  const ready = docs.filter((d) => d.lifecycle.status === "ready");
  const ticked = ready.filter((d) => chosen.has(d.id)).length;
  const finding = docs.length > FIND_AFTER;
  const shown = finding ? findDocs(docs, query) : docs;
  const shownReady = shown.filter((d) => d.lifecycle.status === "ready").map((d) => d.id);
  const hiddenChosen = ready.filter((d) => chosen.has(d.id) && !shown.includes(d)).length;
  const full = limit.kind === "hard" && ticked >= limit.max;

  const toggle = (id: DocumentId) => {
    const next = new Set(chosen);
    if (!next.delete(id)) next.add(id);
    onChange(next);
  };

  return (
    <fieldset className="picker">
      <legend>Documents</legend>
      <div className="picker-tools">
        {finding && <FindField label={findLabel} value={query} onChange={setQuery} />}
        <p className="pick-count" aria-live="polite">
          {ticked === 0 ? "None chosen" : `${ticked} chosen`}
          {hiddenChosen > 0 && `, ${hiddenChosen} not shown by the search`}
        </p>
        {limit.kind === "select-all" && ready.length > 1 && (
          <button type="button" className="text-button" disabled={busy || shownReady.length === 0} onClick={() => {
            const next = chooseAll(chosen, shownReady, limit.max);
            onChange(next.chosen);
            setCapped(next.capped);
          }}>
            {allChosen(chosen, shownReady, limit.max) ? "Unselect all ready" : "Select all ready"}
          </button>
        )}
      </div>
      {limit.kind === "select-all" && capped && (
        <p className="quiet">A chat answers from at most {limit.max} documents, so only the first {limit.max} are chosen.</p>
      )}
      {full && <p className="quiet" role="status">{limit.note}</p>}
      {ready.length === 0 && <p className="quiet">{noneReady}</p>}
      {finding && shown.length === 0 && (
        <p className="quiet">
          No document matches &ldquo;{query.trim()}&rdquo;.{" "}
          <button type="button" className="text-button" onClick={() => setQuery("")}>Clear</button>
        </p>
      )}
      <ul>
        {shown.map((d) => {
          const isReady = d.lifecycle.status === "ready";
          const isChosen = isReady && chosen.has(d.id);
          const open = isReady && (isChosen || !full);
          return (
            <li key={d.id}>
              <label className="pick" data-disabled={!open || undefined}>
                <input type="checkbox" checked={isChosen} disabled={!open || busy}
                  aria-labelledby={`pick-${d.id}`} aria-describedby={`pick-meta-${d.id}`} onChange={() => toggle(d.id)} />
                <span className="pick-name" id={`pick-${d.id}`}>{d.filename}</span>
                <span className="pick-meta" id={`pick-meta-${d.id}`}>
                  <VersionMark number={laterVersion(d)} />{isReady ? docMeta(d) : statusLine(d).text}
                </span>
              </label>
            </li>
          );
        })}
      </ul>
    </fieldset>
  );
}
