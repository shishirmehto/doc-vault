// What the source pane shows for a comparison's cell, apart from the screen so it is tested on Node.

import type { Comparison, ComparisonPane, Doc, Result } from "../api/domain.ts";
import type { SourceView } from "../components/SourcePane.tsx";
import { NO_CELL, cannotOpen, explain } from "../copy.ts";
import { cellAt } from "./compare.ts";

/**
 * The URL names a cell by row and column; the comparison and the document resolve it to a page and a passage.
 * `read` is null while the comparison loads. `doc` is the cell's document: null while `loading`, or when it is gone.
 */
export function cellView(read: Result<Comparison> | null, pane: ComparisonPane, doc: Doc | null, loading: boolean): SourceView {
  if (read === null) return { kind: "resolving" };
  if (!read.ok) return { kind: "unknown", message: explain(read.error) };
  const target = cellAt(read.value, pane.cite);
  if (!target) return { kind: "unknown", message: NO_CELL };
  if (target.doc.deleted) return { kind: "unknown", message: cannotOpen(target.doc) };
  if (doc === null) return loading ? { kind: "resolving" } : { kind: "unknown", message: cannotOpen(target.doc) };
  const cited = target.cell.source.page;
  const page = pane.page ?? cited;
  return { kind: "page", doc, page, mark: page === cited ? { quote: target.cell.source.quote, label: target.field } : null, citedPage: cited };
}
