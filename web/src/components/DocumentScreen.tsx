"use client";

import { useState, type ReactNode } from "react";
import { api } from "@/api/client";
import type { ApiError, Doc, DocumentId, Insights, KeyFact, PaneRequest, Result, Selection } from "@/api/domain";
import { useFetched } from "@/hooks/useFetched";
import { docMeta, explain, formatMs, statusLine, unitName } from "@/copy";
import { DeleteDocument } from "./DeleteDocument";
import { FailureNote } from "./Rail";
import { SourcePane, type SourceView } from "./SourcePane";
import { StageBar } from "./StageBar";

type Props = {
  id: DocumentId;
  doc: Doc | null;
  loading: boolean;
  pane: PaneRequest | null;
  push(s: Selection): void;
  replace(s: Selection): void;
  onRetry(id: DocumentId): Promise<ApiError | null>;
  /** Starts a chat on this document and opens it. Resolves to the refusal, if any. */
  onAsk(id: DocumentId): Promise<ApiError | null>;
  /** Deletes it and leaves for the home screen. Resolves to the refusal, if any. */
  onDelete(doc: Doc): Promise<ApiError | null>;
};

/** The centre column for one document, and the source pane beside it. */
export function DocumentScreen({ id, doc, loading, pane, push, replace, onRetry, onAsk, onDelete }: Props) {
  // A new key whenever the list shows the document or its insights moving on, so the panel refetches once.
  const insights = useFetched(`${id}:${doc?.lifecycle.status}:${doc?.insights}`, () => api.insights(id));

  if (!doc) {
    return (
      <main className="centre">
        {loading ? <HeaderSkeleton /> : <p className="notice">This document isn&apos;t in the vault. It may have been deleted.</p>}
      </main>
    );
  }

  const openFact = (index: number) => push({ kind: "doc", doc: id, pane: { kind: "cite", cite: { kind: "fact", index }, page: null } });
  const activeFact = pane?.kind === "cite" ? pane.cite.index : null;

  return (
    <>
      <main className="centre">
        <article className="document">
          <header className="doc-header">
            <h1>{doc.filename}</h1>
            <p className="doc-meta">
              {docMeta(doc)}
              {doc.processingMs !== null && `, processed in ${formatMs(doc.processingMs)}`}
            </p>
            {doc.lifecycle.status !== "ready" && (
              <div className="doc-status">
                <span className="row-status" data-tone={statusLine(doc).tone}>{statusLine(doc).text}</span>
                <StageBar lifecycle={doc.lifecycle} />
              </div>
            )}
            {doc.lifecycle.status === "failed" && (
              <FailureNote failure={doc.lifecycle.failure} onRetry={() => onRetry(id)} />
            )}
            <div className="doc-actions">
              {doc.lifecycle.status === "ready" && <AskButton onAsk={() => onAsk(id)} />}
              {doc.layout && doc.lifecycle.status !== "failed" && (
                <button type="button" className="text-button" onClick={() => push({ kind: "doc", doc: id, pane: { kind: "page", page: 1 } })}>
                  Read the {unitName(doc.layout.unit, 2)}
                </button>
              )}
              <DeleteDocument doc={doc} onDelete={() => onDelete(doc)} />
            </div>
          </header>
          <InsightsPanel doc={doc} result={insights.result} reload={insights.reload} activeFact={activeFact}
            onOpenFact={openFact} onRetry={() => onRetry(id)} />
        </article>
      </main>
      {pane && <SourcePane view={sourceView(doc, pane, insights.result)}
        onPage={(page) => replace({ kind: "doc", doc: id, pane: pane.kind === "cite" ? { ...pane, page } : { kind: "page", page } })}
        onClose={() => push({ kind: "doc", doc: id, pane: null })} />}
    </>
  );
}

/** Resolves the URL's pane request against the loaded insights: which page, and which passage to mark. */
function sourceView(doc: Doc, pane: PaneRequest, insights: Result<Insights> | null): SourceView {
  if (pane.kind === "page") return { kind: "page", doc, page: pane.page, mark: null };
  if (insights === null) return { kind: "resolving" };
  const fact = insights.ok && insights.value.status === "ready" ? insights.value.keyFacts[pane.cite.index] : undefined;
  if (!fact) return { kind: "unknown", message: "This fact is not in the document's current summary." };
  const page = pane.page ?? fact.source.page;
  const mark = page === fact.source.page ? { quote: fact.source.quote, label: fact.label } : null;
  return { kind: "page", doc, page, mark, citedPage: fact.source.page };
}

function AskButton({ onAsk }: { onAsk(): Promise<ApiError | null> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const ask = async () => {
    setBusy(true);
    const refused = await onAsk();
    setError(refused);
    setBusy(false);
  };
  return (
    <>
      <button type="button" className="button" disabled={busy} onClick={() => void ask()}>
        {busy ? "Starting a chat" : "Ask about this document"}
      </button>
      {error && <p className="row-message" data-tone="failed">{explain(error)}</p>}
    </>
  );
}

function InsightsPanel({ doc, result, reload, activeFact, onOpenFact, onRetry }: {
  doc: Doc;
  result: Result<Insights> | null;
  reload(): void;
  activeFact: number | null;
  onOpenFact(index: number): void;
  onRetry(): Promise<ApiError | null>;
}) {
  if (result === null) return <SummarySkeleton label="Loading the summary" />;
  if (!result.ok) {
    return (
      <section className="insights">
        <p className="notice" data-tone="failed">{explain(result.error)}</p>
        <button type="button" className="button" onClick={reload}>Try again</button>
      </section>
    );
  }
  const insights = result.value;
  switch (insights.status) {
    case "pending":
      return doc.lifecycle.status === "ready"
        ? <SummarySkeleton label="Writing the summary" />
        : <p className="notice">The summary appears when indexing finishes.</p>;
    case "unavailable":
      return <p className="notice">{insights.reason}</p>;
    case "failed":
      return (
        <section className="insights">
          <h2>The summary failed</h2>
          <FailureNote failure={insights.failure} onRetry={onRetry} />
        </section>
      );
    case "ready":
      return (
        <section className="insights">
          <h2>{insights.title}</h2>
          <p className="doc-type">
            {sentenceCase(insights.documentType)}
            {insights.tags.length > 0 && (
              <>
                {". "}
                <span className="tags">{insights.tags.map((t) => <span key={t} className="tag">{t}</span>)}</span>
              </>
            )}
          </p>
          <CoverageNotice coverage={insights.coverage} doc={doc} />
          <p className="summary">{insights.summary}</p>
          <h3>Key facts</h3>
          <KeyFacts facts={insights.keyFacts} active={activeFact} onOpen={onOpenFact} />
          {insights.droppedKeyFacts > 0 && (
            <p className="quiet">
              {insights.droppedKeyFacts === 1
                ? "One more fact was left out because its quote was not on the page it cited."
                : `${insights.droppedKeyFacts} more facts were left out because their quotes were not on the pages they cited.`}
            </p>
          )}
          <p className="written-by">Written by {insights.writtenBy.model} with prompt {insights.writtenBy.promptVersion}.</p>
        </section>
      );
    default: {
      const unknown: never = insights;
      return unknown;
    }
  }
}

/** Shown only when the model read part of the document. */
function CoverageNotice({ coverage, doc }: { coverage: { pagesRead: number; pageCount: number }; doc: Doc }) {
  if (coverage.pagesRead >= coverage.pageCount) return null;
  const unit = unitName(doc.layout?.unit ?? "page", coverage.pageCount);
  return (
    <p className="coverage">
      Based on the first {coverage.pagesRead} of {coverage.pageCount} {unit}. Facts from later {unit} are not here.
    </p>
  );
}

function KeyFacts({ facts, active, onOpen }: { facts: readonly KeyFact[]; active: number | null; onOpen(i: number): void }) {
  if (facts.length === 0) return <p className="quiet">No key facts were found in this document.</p>;
  return (
    <dl className="facts">
      {facts.map((fact, i) => (
        <div key={i} className="fact" data-active={active === i || undefined}>
          <dt>{fact.label}</dt>
          <dd>
            <span className="fact-value">{fact.value}</span>
            <button type="button" className="page-tab" aria-pressed={active === i}
              aria-label={`${fact.label}: show the passage on ${fact.pageLabel}`} onClick={() => onOpen(i)}>
              {fact.pageLabel}
            </button>
          </dd>
        </div>
      ))}
    </dl>
  );
}

function sentenceCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function SummarySkeleton({ label }: { label: string }): ReactNode {
  return (
    <section className="insights" aria-busy="true">
      <p className="quiet">{label}</p>
      {[92, 100, 76].map((w) => <span key={w} className="skeleton line" style={{ width: `${w}%` }} />)}
    </section>
  );
}

function HeaderSkeleton() {
  return (
    <div className="document" aria-busy="true">
      <span className="skeleton heading" style={{ width: "50%" }} />
      <span className="skeleton line" style={{ width: "30%" }} />
    </div>
  );
}
