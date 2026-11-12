"use client";

import { Fragment, useRef, useState, type ReactNode } from "react";
import { api } from "@/api/client";
import type {
  ApiError, Doc, DocumentId, Insights, KeyFact, PaneRequest, Result, Selection, SummaryId, SummaryList,
} from "@/api/domain";
import { useFetched } from "@/hooks/useFetched";
import { useCompareRun } from "@/hooks/useCompareRun";
import { useRewrites } from "@/hooks/useRewrites";
import { useVersions } from "@/hooks/useVersions";
import type { CompareRequest } from "@/lib/compare";
import { familyOf, hasVersions } from "@/lib/versions";
import { citedFact, citedSource, summarySourceView } from "@/lib/summaryPane";
import { docMeta, explain, formatMs, statusLine, unitName, versionCompareBlocked, withVersion } from "@/copy";
import { DeleteDocument } from "./DeleteDocument";
import { MissingDocument } from "./Missing";
import { FailureNote } from "./Rail";
import { SourcePane, type SourceView } from "./SourcePane";
import { StageBar } from "./StageBar";
import { SummaryRewrite } from "./SummaryRewrite";
import { NewerVersionNote, UploadVersion, VersionLine, VersionList } from "./Versions";

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
  /** Compares two versions of this document and lands on the comparison. */
  onCompareVersions(request: CompareRequest): void;
  /** Uploads files as the next version of this document. */
  onUploadVersion(files: File[]): void;
};

/** The centre column for one document, and the source pane beside it. */
export function DocumentScreen({
  id, doc: current, loading, pane, push, replace, onRetry, onAsk, onDelete, onCompareVersions, onUploadVersion,
}: Props) {
  // A document that leaves the list, because a newer version arrived or because it was deleted, is read again on its
  // own. Until that read lands the page keeps what it showed, rather than a skeleton.
  const [last, setLast] = useState(current);
  if (current !== null && current !== last) setLast(current);
  const doc = current ?? (loading ? last : null);
  // A new key whenever the list shows the document or its insights moving on, so the panel refetches once.
  const insights = useFetched(`${id}:${doc?.lifecycle.status}:${doc?.insights}`, () => api.insights(id));
  const cite = citedSource(pane);
  const rewrites = useRewrites(id, cite, () => replace({ kind: "doc", doc: id, pane: null }));
  const versions = useVersions(doc);
  const [listOpen, setListOpen] = useState(false);
  const [askRefused, setAskRefused] = useState<ApiError | null>(null);
  const disclosure = useRef<HTMLElement>(null);
  const running = useCompareRun().phase === "running";

  if (!doc) {
    return (
      <main className="centre">
        {loading ? <HeaderSkeleton /> : <MissingDocument />}
      </main>
    );
  }

  const openFact = (index: number) => push({ kind: "doc", doc: id, pane: { kind: "cite", cite: { kind: "fact", index }, page: null } });
  const openSource = (summary: SummaryId, index: number) =>
    push({ kind: "doc", doc: id, pane: { kind: "cite", cite: { kind: "summary", summary, index }, page: null } });
  const openVersion = (version: DocumentId) => push({ kind: "doc", doc: version, pane: null });
  const family = versions.kind === "loaded" ? familyOf(doc, versions.docs) : null;
  const other = family?.compareWith ?? null;
  const compareWhy = other && versionCompareBlocked(doc, other, running);
  const compareVersions = () => {
    if (!other || compareWhy) return;
    const columns = [doc, other].sort((a, b) => a.version.number - b.version.number);
    onCompareVersions({ documents: columns.map((d) => ({ id: d.id, filename: withVersion(d.filename, d.version.number) })), focus: null });
  };
  const openRewrite = () => {
    rewrites.setOpen(true);
    disclosure.current?.focus({ preventScroll: true });
    disclosure.current?.scrollIntoView({ block: "start" });
  };
  const ready = doc.lifecycle.status === "ready";
  const readable = doc.layout !== null && doc.lifecycle.status !== "failed";
  const versionCount = family ? family.others.length + 1 : 0;

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
            {hasVersions(doc) && (
              <VersionLine doc={doc} versions={versions} family={family} onOpen={openVersion}>
                {other && (
                  <button type="button" className="text-button" aria-disabled={compareWhy ? true : undefined}
                    aria-describedby={compareWhy ? "compare-version-why" : undefined} onClick={compareVersions}>
                    Compare with version {other.version.number}
                  </button>
                )}
              </VersionLine>
            )}
            {compareWhy && <p className="quiet" id="compare-version-why">{compareWhy}</p>}
            {doc.version.number < doc.version.latest && (
              <NewerVersionNote doc={doc} newest={family?.newest ?? null} onOpen={openVersion} />
            )}
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
              {(ready || readable || versionCount > 1) && <div className="doc-actions-use">
                {ready && (
                  <>
                    <AskButton onAsk={() => onAsk(id)} onRefused={setAskRefused} />
                    <button type="button" className="button secondary" onClick={() => push({ kind: "compare", with: id })}>
                      Compare with...
                    </button>
                    <button type="button" className="button secondary" onClick={openRewrite}>Rewrite the summary</button>
                  </>
                )}
                {readable && (
                  <button type="button" className="button secondary" onClick={() => push({ kind: "doc", doc: id, pane: { kind: "page", page: 1 } })}>
                    Read the {unitName(doc.layout?.unit ?? "page", 2)}
                  </button>
                )}
                {versionCount > 1 && (
                  <button type="button" className="button secondary" aria-expanded={listOpen} aria-controls="version-list"
                    onClick={() => setListOpen(!listOpen)}>
                    Versions ({versionCount})
                  </button>
                )}
              </div>}
              <div className="doc-actions-file">
                <UploadVersion onFiles={onUploadVersion} />
                <DeleteDocument doc={doc} otherVersions={versionCount > 1} onDelete={() => onDelete(doc)} />
              </div>
            </div>
            {askRefused && <p className="row-message" data-tone="failed" role="alert">{explain(askRefused)}</p>}
            {listOpen && family && versionCount > 1 && (
              <VersionList id="version-list" doc={doc} family={family} onOpen={openVersion} />
            )}
          </header>
          <InsightsPanel doc={doc} result={insights.result} reload={insights.reload} activeFact={citedFact(pane)}
            onOpenFact={openFact} onRetry={() => onRetry(id)}
            rewrite={ready && <SummaryRewrite rewrites={rewrites} cite={cite} onOpenSource={openSource} disclosure={disclosure} />} />
        </article>
      </main>
      {pane && <SourcePane view={sourceView(doc, pane, insights.result, rewrites.paneList)}
        onPage={(page) => replace({ kind: "doc", doc: id, pane: pane.kind === "cite" ? { ...pane, page } : { kind: "page", page } })}
        onClose={() => push({ kind: "doc", doc: id, pane: null })} />}
    </>
  );
}

/**
 * Resolves the URL's pane request against the loaded insights, or the document's rewritten summaries: which page,
 * and which passage to mark.
 */
function sourceView(doc: Doc, pane: PaneRequest, insights: Result<Insights> | null, summaries: Result<SummaryList> | null): SourceView {
  if (pane.kind === "page") return { kind: "page", doc, page: pane.page, mark: null };
  if (pane.cite.kind === "summary") return summarySourceView(summaries, pane.cite, pane.page, doc);
  if (insights === null) return { kind: "resolving" };
  const fact = insights.ok && insights.value.status === "ready" ? insights.value.keyFacts[pane.cite.index] : undefined;
  if (!fact) return { kind: "unknown", message: "This fact is not in the document's current summary." };
  const page = pane.page ?? fact.source.page;
  const mark = page === fact.source.page ? { quote: fact.source.quote, label: fact.label } : null;
  return { kind: "page", doc, page, mark, citedPage: fact.source.page };
}

function AskButton({ onAsk, onRefused }: { onAsk(): Promise<ApiError | null>; onRefused(error: ApiError | null): void }) {
  const [busy, setBusy] = useState(false);
  const ask = async () => {
    setBusy(true);
    onRefused(await onAsk());
    setBusy(false);
  };
  return (
    <button type="button" className="button" disabled={busy} onClick={() => void ask()}>
      {busy ? "Starting a chat" : "Ask about this document"}
    </button>
  );
}

/**
 * `rewrite` sits under the summary, or under the note that it is loading, being written or failed. Every branch is
 * the one section with `rewrite` keyed, so the disclosure keeps its node, and the focus in it, as the insights land.
 */
function InsightsPanel({ doc, result, reload, activeFact, onOpenFact, onRetry, rewrite }: {
  doc: Doc;
  result: Result<Insights> | null;
  reload(): void;
  activeFact: number | null;
  onOpenFact(index: number): void;
  onRetry(): Promise<ApiError | null>;
  rewrite: ReactNode;
}) {
  const slot = <Fragment key="rewrite">{rewrite}</Fragment>;
  if (result === null) {
    return <section className="insights"><SummarySkeleton label="Loading the summary" />{slot}</section>;
  }
  if (!result.ok) {
    return (
      <section className="insights">
        <p className="notice" data-tone="failed">{explain(result.error)}</p>
        <button type="button" className="button" onClick={reload}>Try again</button>
        {slot}
      </section>
    );
  }
  const insights = result.value;
  switch (insights.status) {
    case "pending":
      return (
        <section className="insights">
          {doc.lifecycle.status === "ready"
            ? <SummarySkeleton label="Writing the summary" />
            : <p className="notice">The summary appears when indexing finishes.</p>}
          {slot}
        </section>
      );
    case "unavailable":
      return <section className="insights"><p className="notice">{insights.reason}</p>{slot}</section>;
    case "failed":
      return (
        <section className="insights">
          <h2>The summary failed</h2>
          <FailureNote failure={insights.failure} onRetry={onRetry} />
          {slot}
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
          {slot}
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

/** Busy on its own, so the disclosure beside it is not. */
function SummarySkeleton({ label }: { label: string }): ReactNode {
  return (
    <div aria-busy="true">
      <p className="quiet">{label}</p>
      {[92, 100, 76].map((w) => <span key={w} className="skeleton line" style={{ width: `${w}%` }} />)}
    </div>
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
