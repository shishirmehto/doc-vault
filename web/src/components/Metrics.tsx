"use client";

import type { ReactNode } from "react";
import { api } from "@/api/client";
import type {
  AiUsage, CacheFigures, CodeCount, Counts, DocumentMetrics, Health, MetricsWindow, ProcessingMetrics, Purpose, Result,
  Spread, Step, TurnOutcome,
} from "@/api/domain";
import { useFetched } from "@/hooks/useFetched";
import { useHealth } from "@/hooks/useHealth";
import {
  CACHE_OFF, CACHE_UNREACHABLE, NOTHING_CACHED, cacheSince, capFigures, explain, formatBytes, formatMs, formatUsd, hitRate,
  noCap,
} from "@/copy";

const WINDOWS: Record<MetricsWindow, string> = { "24h": "Last 24 hours", "7d": "Last 7 days" };

const STATUS: Record<keyof DocumentMetrics["byStatus"], string> = {
  queued: "Waiting to start",
  parsing: "Reading pages",
  embedding: "Indexing",
  ready: "Ready",
  failed: "Failed",
};

const STEP: Record<Step, string> = { parse: "Read pages", embed: "Index passages", analyze: "Write the summary" };

const PURPOSE: Record<Purpose, string> = {
  embed_chunks: "Index passages",
  embed_query: "Embed questions",
  insights: "Summaries and key facts",
  condense: "Rewrite follow-ups",
  answer: "Answers",
  compare: "Comparisons",
  summary: "Rewritten summaries",
};

const OUTCOME: Record<TurnOutcome, string> = {
  answered: "Answered",
  no_evidence: "Not found, no evidence",
  model_declined: "Not found, the model declined",
  unverified: "Not found, quotes unverified",
};

const SINCE = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

const n = (x: number) => x.toLocaleString("en-US");

/**
 * Each route loads and fails on its own, so one failing never hides the others. /health is read here as well as by the
 * page's shared poll, so the cap and the cache load, fail and refresh like the other halves.
 */
export function Metrics({ window, onWindow }: { window: MetricsWindow; onWindow(w: MetricsWindow): void }) {
  const documents = useFetched("metrics:documents", api.documentMetrics);
  const processing = useFetched(`metrics:processing:${window}`, () => api.processingMetrics(window));
  const status = useFetched("metrics:health", api.health);
  const health = useHealth();
  const fake = health?.ok === true && health.value.aiMode === "fake";

  return (
    <main className="centre">
      <section className="metrics" aria-labelledby="metrics-title">
        <header className="metrics-header">
          <h1 id="metrics-title">Metrics</h1>
          <button type="button" className="text-button" onClick={() => { documents.reload(); processing.reload(); status.reload(); }}>
            Refresh
          </button>
        </header>

        <h2>Documents</h2>
        <Half result={documents.result} retry={documents.reload}>{(m) => <DocumentFigures m={m} />}</Half>

        <div className="metrics-window">
          <h2>Processing</h2>
          <div className="segments" role="group" aria-label="Window">
            {(Object.keys(WINDOWS) as MetricsWindow[]).map((w) => (
              <button key={w} type="button" aria-pressed={w === window} onClick={() => onWindow(w)}>{WINDOWS[w]}</button>
            ))}
          </div>
        </div>
        <Half result={processing.result} retry={processing.reload}>{(m) => <ProcessingFigures m={m} fake={fake} />}</Half>

        <h2>Spending cap</h2>
        <Half result={status.result} retry={status.reload}>{(h) => <CapFigures health={h} />}</Half>

        <h2>Caches</h2>
        {/* The counts come with the processing figures. Without them, /health says whether the cache is off or down. */}
        <Half result={processing.result} retry={processing.reload}>
          {(m) =>
            m.cache ? (
              <CacheTable cache={m.cache} />
            ) : (
              <Half result={status.result} retry={status.reload}>
                {(h) => <p className="quiet">{h.cache === "off" ? CACHE_OFF : CACHE_UNREACHABLE}</p>}
              </Half>
            )}
        </Half>
      </section>
    </main>
  );
}

function Half<T>({ result, retry, children }: { result: Result<T> | null; retry(): void; children(value: T): ReactNode }) {
  if (result === null) {
    return <div aria-busy="true">{[64, 48, 56].map((w) => <span key={w} className="skeleton line" style={{ width: `${w}%` }} />)}</div>;
  }
  if (!result.ok) {
    return (
      <p className="notice" data-tone="failed" role="alert">
        {explain(result.error)}{" "}
        <button type="button" className="text-button" onClick={retry}>Try again</button>
      </p>
    );
  }
  return children(result.value);
}

function DocumentFigures({ m }: { m: DocumentMetrics }) {
  return (
    <>
      <p className="metrics-lead">
        {n(m.total)} documents, {n(m.pages)} pages, {n(m.chunks)} indexed passages, {formatBytes(m.bytes)} stored.
      </p>
      <div className="metrics-pair">
        <Figures caption="Documents by status">
          <thead><tr><th scope="col">Status</th><th scope="col">Documents</th></tr></thead>
          <tbody>
            {(Object.keys(STATUS) as (keyof typeof STATUS)[]).map((s) => (
              <tr key={s}><th scope="row">{STATUS[s]}</th><td>{n(m.byStatus[s])}</td></tr>
            ))}
          </tbody>
        </Figures>
        <CodeTable caption="Failed documents by reason" unit="Documents" rows={m.failures} empty="No document has failed." />
      </div>
    </>
  );
}

function CodeTable({ caption, unit, rows, empty }: { caption: string; unit: string; rows: readonly CodeCount[]; empty: string }) {
  return (
    <Figures caption={caption}>
      <thead><tr><th scope="col">Reason</th><th scope="col">{unit}</th></tr></thead>
      <tbody>
        {rows.length === 0 ? (
          <tr><td colSpan={2} className="quiet text">{empty}</td></tr>
        ) : (
          rows.map((r) => <tr key={r.code}><th scope="row"><code>{r.code}</code></th><td>{n(r.count)}</td></tr>)
        )}
      </tbody>
    </Figures>
  );
}

function ProcessingFigures({ m, fake }: { m: ProcessingMetrics; fake: boolean }) {
  const noCost = fake && m.ai.costUsd === 0 && m.ai.unpricedCalls === 0;
  return (
    <>
      <p className="metrics-lead">Since {SINCE.format(m.since)}: {n(m.uploaded)} uploaded, {n(m.indexed)} indexed.</p>

      <Figures caption="Timings">
        <thead><tr><th scope="col">Measure</th><th scope="col">p50</th><th scope="col">p95</th></tr></thead>
        <tbody>
          <tr><th scope="row">Upload to ready</th><SpreadCells spread={m.timeToReady} /></tr>
          <tr><th scope="row">Queue wait, upload to first page read</th><SpreadCells spread={m.queueWait} /></tr>
          <tr><th scope="row">Answer a question</th><SpreadCells spread={m.chat.latency} /></tr>
        </tbody>
      </Figures>

      <Figures caption="Steps, counted in runs: a retried document runs a step again">
        <thead>
          <tr>
            <th scope="col">Step</th><th scope="col">Runs</th><th scope="col">Failed runs</th><th scope="col">Retries</th>
            <th scope="col">p50</th><th scope="col">p95</th>
          </tr>
        </thead>
        <tbody>
          {m.steps.map((s) => (
            <tr key={s.step}>
              <th scope="row">{STEP[s.step]}</th>
              <td>{n(s.runs)}</td>
              <td>
                {n(s.failed)}
                {s.failures.length > 0 && <span className="quiet"> ({s.failures.map((f) => `${f.code} ${n(f.count)}`).join(", ")})</span>}
              </td>
              <td>{n(s.retries)}</td>
              <SpreadCells spread={s.duration} />
            </tr>
          ))}
        </tbody>
      </Figures>

      <p className="metrics-lead">
        {n(m.ai.calls)} AI calls, {n(m.ai.errors)} errors, {n(m.ai.inputTokens)} tokens in, {n(m.ai.outputTokens)} out. Cost:{" "}
        {noCost ? "fake provider, no cost" : costText(m.ai)}.
      </p>
      <Figures caption="AI calls by purpose and model">
        <thead>
          <tr>
            <th scope="col">Purpose</th><th scope="col" className="text">Model</th><th scope="col">Calls</th>
            <th scope="col">Errors</th><th scope="col">Tokens in</th><th scope="col">Tokens out</th>
            {!noCost && <th scope="col">Cost</th>}<th scope="col">p50</th><th scope="col">p95</th>
          </tr>
        </thead>
        <tbody>
          {m.ai.rows.length === 0 ? (
            <tr><td colSpan={noCost ? 8 : 9} className="quiet text">No AI calls in this window.</td></tr>
          ) : (
            m.ai.rows.map((r) => (
              <tr key={`${r.purpose} ${r.model}`}>
                <th scope="row">{PURPOSE[r.purpose]}</th>
                <td className="text"><code>{r.model}</code></td>
                <td>{n(r.calls)}</td>
                <td>{n(r.errors)}</td>
                <td>{n(r.inputTokens)}</td>
                <td>{n(r.outputTokens)}</td>
                {!noCost && <td>{costText(r)}</td>}
                <SpreadCells spread={r.latency} />
              </tr>
            ))
          )}
        </tbody>
      </Figures>

      <Figures caption={`Questions: ${n(m.chat.turns)} asked`}>
        <thead><tr><th scope="col">Outcome</th><th scope="col">Questions</th></tr></thead>
        <tbody>
          {(Object.keys(OUTCOME) as TurnOutcome[]).map((o) => (
            <tr key={o}><th scope="row">{OUTCOME[o]}</th><td>{n(m.chat.outcomes[o])}</td></tr>
          ))}
        </tbody>
      </Figures>
    </>
  );
}

function CapFigures({ health }: { health: Health }) {
  if (health.aiBudget === null) return <p className="quiet">{noCap(health)}</p>;
  const { rows, stopped } = capFigures(health.aiBudget, new Date());
  return (
    <>
      {stopped && <p className="notice" data-tone="failed">{stopped}</p>}
      <Figures caption="Today's AI spending">
        <tbody>
          {rows.map(([label, value]) => <tr key={label}><th scope="row">{label}</th><td>{value}</td></tr>)}
        </tbody>
      </Figures>
    </>
  );
}

const CACHES: Record<keyof Omit<CacheFigures, "countedSince">, string> = { embedding: "Embeddings", answer: "First answers" };

function CacheTable({ cache }: { cache: CacheFigures }) {
  if (cache.countedSince === null) return <p className="quiet">{NOTHING_CACHED}</p>;
  return (
    <>
      <p className="metrics-lead">{cacheSince(cache.countedSince)}</p>
      <Figures caption="Lookups by cache">
        <thead>
          <tr><th scope="col">Cache</th><th scope="col">Hits</th><th scope="col">Misses</th><th scope="col">Hit rate</th></tr>
        </thead>
        <tbody>
          {(Object.keys(CACHES) as (keyof typeof CACHES)[]).map((c) => (
            <tr key={c}>
              <th scope="row">{CACHES[c]}</th>
              <td>{n(cache[c].hits)}</td>
              <td>{n(cache[c].misses)}</td>
              <td><HitRate counts={cache[c]} /></td>
            </tr>
          ))}
        </tbody>
      </Figures>
    </>
  );
}

/**
 * The dash is named for screen readers by role and label, not by a visually hidden span: absolutely placed in a cell at
 * the right edge of a narrow window, the span's text runs past the page and makes it scroll sideways.
 */
function HitRate({ counts }: { counts: Counts }) {
  return hitRate(counts) ?? <span role="img" aria-label="no lookups">–</span>;
}

/** A table that scrolls sideways inside its own box when it is wider than the column. */
function Figures({ caption, children }: { caption: string; children: ReactNode }) {
  return (
    <div className="table-box" role="region" aria-label={caption} tabIndex={0} data-scroll-x>
      <table className="figures">
        <caption>{caption}</caption>
        {children}
      </table>
    </div>
  );
}

function SpreadCells({ spread }: { spread: Spread }) {
  return spread === null ? (
    <><td className="quiet">no samples</td><td className="quiet">no samples</td></>
  ) : (
    <><td>{formatMs(spread.p50Ms)}</td><td>{formatMs(spread.p95Ms)}</td></>
  );
}

/** Priced calls only, so unpriced ones are named beside the figure rather than counted as free. */
function costText(u: AiUsage): string {
  const unpriced = u.unpricedCalls === 0 ? "" : `, plus ${n(u.unpricedCalls)} unpriced ${u.unpricedCalls === 1 ? "call" : "calls"}`;
  return `${formatUsd(u.costUsd)}${unpriced}`;
}
