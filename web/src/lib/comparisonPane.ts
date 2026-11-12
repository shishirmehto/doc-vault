import type { Comparison, ComparisonPane, Result } from "../api/domain.ts";
import type { SourceView } from "../components/SourcePane.tsx";
import { NO_CELL, cannotOpen, explain } from "../copy.ts";
import { citedPageView } from "./citedPage.ts";
import { cellAt } from "./compare.ts";
import type { LookedUp } from "./docList.ts";

export function cellView(read: Result<Comparison> | null, pane: ComparisonPane, { doc, loading, failed }: LookedUp): SourceView {
  if (read === null) return { kind: "resolving" };
  if (!read.ok) return { kind: "unknown", message: explain(read.error) };
  const target = cellAt(read.value, pane.cite);
  if (!target) return { kind: "unknown", message: NO_CELL };
  if (target.doc.deleted) return { kind: "unknown", message: cannotOpen(target.doc) };
  if (doc === null) {
    if (loading) return { kind: "resolving" };
    return failed ? { kind: "failed", error: failed } : { kind: "unknown", message: cannotOpen(target.doc) };
  }
  return citedPageView(doc, pane.page, target.cell.source.page, { quote: target.cell.source.quote, label: target.field });
}
