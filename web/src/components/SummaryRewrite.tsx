"use client";

import { useEffect, useId, useRef, useState, type Ref } from "react";
import type { Summary, SummaryCite, SummaryId, SummaryLength, SummaryTone } from "@/api/domain";
import type { Rewrites } from "@/hooks/useRewrites";
import { useWorthRetrying } from "@/hooks/useWorthRetrying";
import { FOCUS_MAX, LENGTHS, TONES, normaliseFocus, type Run } from "@/lib/summary";
import {
  LOADING_REWRITES, LONG_DOCUMENT, MORE_REWRITES, NO_REWRITES, REWRITE_LEAD, REWRITTEN, SAVED_EARLIER, clockTime,
  explain, rewriteOptions, rewriting, rewrittenBy, sourceLabel, sourcesNote, wordTarget,
} from "@/copy";

/** After this long, the run says that a long document takes a while. */
const LONG_AFTER_MS = 5000;

/**
 * The document's summary written again at another length, about one topic, or for the reader's client: the options,
 * the request in flight, the summary shown with a page tab per source, and the summaries made before.
 */
export function SummaryRewrite({ rewrites, cite, onOpenSource, disclosure }: {
  rewrites: Rewrites;
  cite: SummaryCite | null;
  onOpenSource(summary: SummaryId, index: number): void;
  disclosure: Ref<HTMLElement>;
}) {
  const { run, shown, landed } = rewrites;
  const submit = useRef<HTMLButtonElement>(null);
  const retry = useWorthRetrying(run.kind === "failed" ? run.error : null);
  return (
    <details className="rewrite" open={rewrites.open} onToggle={(e) => rewrites.setOpen(e.currentTarget.open)}>
      <summary ref={disclosure}>Rewrite the summary</summary>
      <div className="rewrite-body">
        <p className="quiet">{REWRITE_LEAD}</p>
        <RewriteForm rewrites={rewrites} submit={submit} />
        <div className="rewrite-run" aria-live="polite">
          {run.kind === "running" ? (
            <Running key={run.startedAt} run={run} />
          ) : run.kind === "failed" ? (
            <p className="notice" data-tone="failed">
              {explain(run.error)}
              {retry && (
                <>
                  {" "}
                  {/* The run replaces this button, so focus goes to Rewrite, which holds it while the run is in flight. */}
                  <button type="button" className="text-button" onClick={() => {
                    submit.current?.focus();
                    void rewrites.start(run.options);
                  }}>Try again</button>
                </>
              )}
            </p>
          ) : (
            landed && <p className="visually-hidden">{landed.cached ? SAVED_EARLIER : REWRITTEN}</p>
          )}
        </div>
        {shown && <RewriteSheet summary={shown} cite={cite} onOpenSource={onOpenSource} />}
        <EarlierRewrites rewrites={rewrites} />
      </div>
    </details>
  );
}

function RewriteForm({ rewrites, submit }: { rewrites: Rewrites; submit: Ref<HTMLButtonElement> }) {
  const { draft, edit } = rewrites;
  const running = rewrites.run.kind === "running";
  const name = useId();
  return (
    <form className="rewrite-form" onSubmit={(e) => {
      e.preventDefault();
      void rewrites.start({ length: draft.length, focus: normaliseFocus(draft.focus), tone: draft.tone });
    }}>
      <fieldset>
        <legend>Length</legend>
        <div className="segments">
          {(Object.keys(LENGTHS) as SummaryLength[]).map((length) => (
            <label key={length}>
              <input type="radio" name={name} value={length} checked={draft.length === length} onChange={() => edit({ length })} />
              {LENGTHS[length].label}
            </label>
          ))}
        </div>
        <p className="quiet">{wordTarget(draft.length)}</p>
      </fieldset>
      <label className="find">
        <span>Focus (optional)</span>
        <input type="text" value={draft.focus} maxLength={FOCUS_MAX} autoComplete="off" placeholder="For example: deductibles"
          onChange={(e) => edit({ focus: e.currentTarget.value })} />
      </label>
      <div className="rewrite-send">
        <label className="find">
          <span>Written for</span>
          <select value={draft.tone} onChange={(e) => edit({ tone: e.currentTarget.value as SummaryTone })}>
            {(Object.keys(TONES) as SummaryTone[]).map((tone) => <option key={tone} value={tone}>{TONES[tone]}</option>)}
          </select>
        </label>
        {/* aria-disabled, not disabled: a disabled button drops the focus it holds, and the focus must not move. */}
        <button ref={submit} type="submit" className="button" aria-disabled={running}>{running ? "Rewriting" : "Rewrite"}</button>
      </div>
    </form>
  );
}

function Running({ run }: { run: Extract<Run, { kind: "running" }> }) {
  const [long, setLong] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setLong(true), Math.max(0, run.startedAt + LONG_AFTER_MS - Date.now()));
    return () => clearTimeout(timer);
  }, [run.startedAt]);
  return (
    <div className="run">
      <p className="quiet">{rewriting(run.options)}</p>
      {long && <p className="quiet">{LONG_DOCUMENT}</p>}
      <div className="progress indeterminate"><span /></div>
    </div>
  );
}

/** On the white paper of the key-facts schedule: a second copy of the summary, for another reader. */
function RewriteSheet({ summary, cite, onOpenSource }: {
  summary: Summary;
  cite: SummaryCite | null;
  onOpenSource(summary: SummaryId, index: number): void;
}) {
  const count = summary.sources.length;
  const note = sourcesNote(summary);
  return (
    <article className="rewrite-sheet" aria-labelledby={`rewrite-${summary.id}`}>
      <h3 id={`rewrite-${summary.id}`} className="rewrite-options">{rewriteOptions(summary.options)}</h3>
      <p className="summary">{summary.text}</p>
      {summary.cached && <p className="quiet">{SAVED_EARLIER}</p>}
      {count > 0 && (
        <p className="rewrite-sources">
          <span className="quiet">Sources</span>
          {summary.sources.map((source, i) => (
            <button key={i} type="button" className="page-tab" aria-pressed={cite?.summary === summary.id && cite.index === i}
              aria-label={sourceLabel(i, count, source.pageLabel)} onClick={() => onOpenSource(summary.id, i)}>
              {source.pageLabel}
            </button>
          ))}
        </p>
      )}
      {note && <p className="quiet">{note}</p>}
      <p className="written-by">{rewrittenBy(summary, new Date())}</p>
    </article>
  );
}

function EarlierRewrites({ rewrites }: { rewrites: Rewrites }) {
  const { list, shown } = rewrites;
  const heading = useRef<HTMLHeadingElement>(null);
  const now = new Date();
  return (
    <section className="rewrites" aria-labelledby="earlier-rewrites">
      <h3 id="earlier-rewrites" ref={heading} tabIndex={-1}>Earlier rewrites</h3>
      {list === null ? (
        <p className="quiet">{LOADING_REWRITES}</p>
      ) : !list.ok ? (
        <p className="notice" data-tone="failed">
          {explain(list.error)}{" "}
          {/* The reload replaces this button, so focus goes to the heading, which stays. */}
          <button type="button" className="text-button" onClick={() => {
            heading.current?.focus();
            rewrites.reload();
          }}>Try again</button>
        </p>
      ) : list.value.summaries.length === 0 ? (
        <p className="quiet">{NO_REWRITES}</p>
      ) : (
        <>
          <ul>
            {list.value.summaries.map((s) => (
              <li key={s.id}>
                <button type="button" className="rewrite-item" aria-current={s.id === shown?.id ? "true" : undefined}
                  onClick={() => rewrites.choose(s.id)}>
                  <span>{rewriteOptions(s.options)}</span>
                  <time dateTime={s.createdAt.toISOString()}>{clockTime(s.createdAt, now)}</time>
                </button>
              </li>
            ))}
          </ul>
          {!list.value.complete && <p className="quiet">{MORE_REWRITES}</p>}
        </>
      )}
    </section>
  );
}
