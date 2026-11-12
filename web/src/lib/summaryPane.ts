import type { Doc, PaneRequest, Result, SummaryCite, SummaryList } from "../api/domain.ts";
import type { SourceView } from "../components/SourcePane.tsx";
import { NO_SUCH_SOURCE, explain, rewriteNotListed } from "../copy.ts";
import { citedPageView } from "./citedPage.ts";

export function citedFact(pane: PaneRequest | null): number | null {
  return pane?.kind === "cite" && pane.cite.kind === "fact" ? pane.cite.index : null;
}

export function citedSource(pane: PaneRequest | null): SummaryCite | null {
  return pane?.kind === "cite" && pane.cite.kind === "summary" ? pane.cite : null;
}

export function summarySourceView(list: Result<SummaryList> | null, cite: SummaryCite, page: number | null, doc: Doc): SourceView {
  if (list === null) return { kind: "resolving" };
  if (!list.ok) return { kind: "unknown", message: explain(list.error) };
  const summary = list.value.summaries.find((s) => s.id === cite.summary);
  if (!summary) return { kind: "unknown", message: rewriteNotListed(list.value.complete) };
  const source = summary.sources[cite.index];
  if (!source) return { kind: "unknown", message: NO_SUCH_SOURCE };
  return citedPageView(doc, page, source.page, { quote: source.quote, label: "Rewritten summary" });
}
