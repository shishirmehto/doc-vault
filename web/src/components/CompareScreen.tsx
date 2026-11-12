"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { ApiError, ComparisonId, Doc, DocumentId } from "@/api/domain";
import { useFetched } from "@/hooks/useFetched";
import { useListedDoc } from "@/hooks/useListedDoc";
import { useLongRun } from "@/hooks/useLongRun";
import { compareRuns, useCompareRun } from "@/hooks/useCompareRun";
import { useWorthRetrying } from "@/hooks/useWorthRetrying";
import { COMPARE_MAX, FOCUS_MAX, columns, compareRequest, withDoc, type CompareRequest, type Run } from "@/lib/compare";
import {
  COMPARE_FULL, LONG_DOCUMENT, NO_COMPARISONS, NOTHING_TO_COMPARE, compareBlocked, compareFailed, comparedNames, comparing,
  differCount, documentNames, explain, focusLine, formatWhen, notReadyNames,
} from "@/copy";
import { InAppLink } from "./InAppLink";
import { ListPending, Picker } from "./Picker";

const RECENT = 10;

export function CompareScreen({ docs: live, failed, initial, onCompare, onOpen }: {
  docs: readonly Doc[] | null;
  failed: ApiError | null;
  initial: DocumentId | null;
  onCompare(request: CompareRequest): void;
  onOpen(id: ComparisonId): void;
}) {
  const latest = useCompareRun();
  // A success moves this screen to the comparison. That URL change lands a moment after the run is forgotten, so the
  // run stays on screen until then rather than flashing the picker.
  const [run, setRun] = useState(latest);
  if (latest !== run && !(latest.phase === "idle" && run.phase === "running")) setRun(latest);
  const [chosen, setChosen] = useState<ReadonlySet<DocumentId>>(() => new Set(initial ? [initial] : []));
  const [focus, setFocus] = useState("");

  const { doc } = useListedDoc(initial, live);
  const docs = live && withDoc(live, doc);

  const picked = columns(chosen, docs ?? []);
  const blocked = compareBlocked(picked.length, focus);
  const submit = () => {
    if (blocked === null) onCompare(compareRequest(picked, focus));
  };
  const change = (request: CompareRequest) => {
    setChosen(new Set(request.documents.map((d) => d.id)));
    setFocus(request.focus ?? "");
    compareRuns.dismiss();
  };

  return (
    <main className="centre with-composer">
      <div className="centre-scroll">
        <section className="new-chat" aria-labelledby="compare-title">
          <h1 id="compare-title">Compare documents</h1>
          <p className="lead">
            Pick 2 to 5 ready documents. Doc Vault lines up the fields a broker compares, such as limits, deductibles,
            premium and the policy period, and marks the ones that differ. Every value opens its page.
          </p>
          {run.phase === "running" ? (
            <RunStatus key={run.startedAt} run={run} />
          ) : run.phase === "failed" ? (
            <RunFailed run={run} docs={docs ?? []} onRetry={() => onCompare(run.request)} onChange={() => change(run.request)} />
          ) : docs === null ? (
            <ListPending failed={failed} />
          ) : docs.length === 0 ? (
            <p className="quiet">{NOTHING_TO_COMPARE}</p>
          ) : (
            <Picker docs={docs} chosen={chosen} onChange={setChosen} findLabel="Find a document to compare" busy={false}
              limit={{ kind: "hard", max: COMPARE_MAX, note: COMPARE_FULL }} noneReady="Comparisons open up once two documents are ready." />
          )}
        </section>
        <RecentComparisons onOpen={onOpen} />
      </div>
      {run.phase === "idle" && docs !== null && docs.length > 0 && (
        <form className="centre-footer" onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}>
          {blocked && <p className="quiet">{blocked}</p>}
          <div className="compare-row">
            <label className="find">
              <span>Focus (optional)</span>
              <input type="text" value={focus} maxLength={FOCUS_MAX} autoComplete="off" placeholder="For example, deductibles and limits"
                onChange={(e) => setFocus(e.currentTarget.value)} />
            </label>
            <button type="submit" className="button" disabled={blocked !== null}>Compare</button>
          </div>
        </form>
      )}
    </main>
  );
}

function RunStatus({ run }: { run: Extract<Run, { phase: "running" }> }) {
  const long = useLongRun(run.startedAt);
  const box = useRef<HTMLDivElement>(null);
  // The control that started the run is gone or disabled, so focus would fall to the page.
  useEffect(() => {
    box.current?.scrollIntoView({ block: "nearest" });
    box.current?.focus({ preventScroll: true });
  }, []);
  return (
    <div className="run" aria-busy="true" ref={box} tabIndex={-1}>
      <div role="status">
        <p className="quiet">{comparing(documentNames(run.request.documents))}</p>
        {long && <p className="quiet">{LONG_DOCUMENT}</p>}
      </div>
      <div className="progress indeterminate"><span /></div>
    </div>
  );
}

function RunFailed({ run, docs, onRetry, onChange }: {
  run: Extract<Run, { phase: "failed" }>;
  docs: readonly Doc[];
  onRetry(): void;
  onChange(): void;
}) {
  const [first, ...rest] = compareFailed(run.error, notReadyNames(run.error, docs));
  const retry = useWorthRetrying(run.error);
  const actions = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    actions.current?.scrollIntoView({ block: "nearest" });
    actions.current?.querySelector("button")?.focus({ preventScroll: true });
  }, []);
  return (
    <div className="ask-failed">
      <p role="alert">{first}</p>
      {rest.map((line) => <p key={line}>{line}</p>)}
      <p className="actions" ref={actions}>
        {retry && <button type="button" className="button" onClick={onRetry}>Try again</button>}
        <button type="button" className="text-button" onClick={onChange}>Change the documents</button>
      </p>
    </div>
  );
}

function RecentComparisons({ onOpen }: { onOpen(id: ComparisonId): void }) {
  const { result, reload } = useFetched("recent-comparisons", () => api.comparisons(RECENT));
  if (result === null) return null;
  return (
    <section className="recent" aria-labelledby="recent-comparisons-title">
      <h2 id="recent-comparisons-title">Recent comparisons</h2>
      {!result.ok ? (
        <p className="quiet">
          {explain(result.error)}{" "}
          <button type="button" className="text-button" onClick={reload}>Try again</button>
        </p>
      ) : result.value.length === 0 ? (
        <p className="quiet">{NO_COMPARISONS}</p>
      ) : (
        <ul>
          {result.value.map((c) => (
            <li key={c.id}>
              <div>
                <InAppLink to={{ kind: "comparison", comparison: c.id, pane: null, only: false }} onFollow={() => onOpen(c.id)}>
                  {comparedNames(c.documents)}
                </InAppLink>
                <p className="recent-meta">{c.focus === null ? "" : `${focusLine(c.focus)}. `}{differCount(c.rows)}.</p>
              </div>
              <time dateTime={c.createdAt.toISOString()}>{formatWhen(c.createdAt)}</time>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
