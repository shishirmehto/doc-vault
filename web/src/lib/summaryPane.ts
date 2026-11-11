// What the document's source pane shows for a rewritten summary's source, apart from the screen so it is tested on Node.

import type { Doc, PaneRequest, Result, SummaryCite, SummaryList } from "../api/domain.ts";
import type { SourceView } from "../components/SourcePane.tsx";
import { NO_SUCH_SOURCE, explain, rewriteNotListed } from "../copy.ts";

/** The key fact the pane opens, by its index; null for a summary's source or a bare page. */
export function citedFact(pane: PaneRequest | null): number | null {
  return pane?.kind === "cite" && pane.cite.kind === "fact" ? pane.cite.index : null;
}

/** The summary source the pane opens; null for a key fact or a bare page. */
export function citedSource(pane: PaneRequest | null): SummaryCite | null {
  return pane?.kind === "cite" && pane.cite.kind === "summary" ? pane.cite : null;
}

/**
 * The URL names a summary and one of its sources; the document's list of summaries resolves it to a page and a
 * passage. `list` is null while it loads. `page` is set after paging away from the cited page.
 */
export function summarySourceView(list: Result<SummaryList> | null, cite: SummaryCite, page: number | null, doc: Doc): SourceView {
  if (list === null) return { kind: "resolving" };
  if (!list.ok) return { kind: "unknown", message: explain(list.error) };
  const summary = list.value.summaries.find((s) => s.id === cite.summary);
  if (!summary) return { kind: "unknown", message: rewriteNotListed(list.value.complete) };
  const source = summary.sources[cite.index];
  if (!source) return { kind: "unknown", message: NO_SUCH_SOURCE };
  const shown = page ?? source.page;
  const mark = shown === source.page ? { quote: source.quote, label: "Rewritten summary" } : null;
  return { kind: "page", doc, page: shown, mark, citedPage: source.page };
}
