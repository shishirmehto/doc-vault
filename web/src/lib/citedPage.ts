import type { Doc } from "../api/domain.ts";
import type { Mark, SourceView } from "../components/SourcePane.tsx";

export function citedPageView(doc: Doc, paged: number | null, citedPage: number, mark: Mark): SourceView {
  const page = paged ?? citedPage;
  return { kind: "page", doc, page, mark: page === citedPage ? mark : null, citedPage };
}
