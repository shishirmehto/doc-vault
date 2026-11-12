"use client";

import { useEffect, useLayoutEffect, useRef } from "react";
import { api } from "@/api/client";
import type { Doc, Page } from "@/api/domain";
import { useFetched } from "@/hooks/useFetched";
import { locateQuote } from "@/lib/quote";
import { explain } from "@/copy";

/** A passage to mark. `flag` is the short text on the page-edge flag: a chat citation's number. */
export type Mark = { quote: string; label: string; flag?: string };

/**
 * What the pane shows, resolved from the URL: a document's page with an optional passage to mark, or
 * why it cannot (the fact or citation is gone, or its document is no longer in the vault).
 */
export type SourceView =
  | { kind: "resolving" }
  | { kind: "unknown"; message: string }
  | { kind: "page"; doc: Doc; page: number; mark: Mark | null; citedPage?: number };

type Props = { view: SourceView; onPage(page: number): void; onClose(): void };

/** The document's own words. Only a document page is paper; Esc and Close both close it. */
export function SourcePane({ view, onPage, onClose }: Props) {
  const close = useRef(onClose);
  useEffect(() => {
    close.current = onClose;
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || document.querySelector("dialog[open], :popover-open")) return;
      if (e.target instanceof Element && e.target.closest("input, textarea, select")) return;
      close.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const doc = view.kind === "page" ? view.doc : null;
  const count = doc?.layout?.pageCount ?? 0;
  const unit = doc?.layout?.unit === "section" ? "Section" : "Page";
  const page = view.kind === "page" ? view.page : null;

  return (
    <aside className="pane" aria-label="Source">
      <header className="pane-header">
        <span className="pane-file">{doc?.filename}</span>
        {page !== null && count > 0 && (
          <nav className="pager" aria-label="Pages">
            <button type="button" className="text-button" disabled={page <= 1} onClick={() => onPage(page - 1)}
              aria-label={`Previous ${unit.toLowerCase()}`}>Previous</button>
            <span className="pager-at">{unit} {page} of {count}</span>
            <button type="button" className="text-button" disabled={page >= count} onClick={() => onPage(page + 1)}
              aria-label={`Next ${unit.toLowerCase()}`}>Next</button>
          </nav>
        )}
        <button type="button" className="text-button" onClick={onClose}>Close</button>
      </header>
      <div className="pane-body">
        {view.kind === "resolving" && <SheetSkeleton />}
        {view.kind === "unknown" && <p className="notice">{view.message}</p>}
        {view.kind === "page" && (
          <>
            {view.mark === null && view.citedPage !== undefined && (
              <p className="pane-note">
                The passage is on {unit.toLowerCase()} {view.citedPage}.{" "}
                <button type="button" className="text-button" onClick={() => onPage(view.citedPage!)}>Go back to it</button>
              </p>
            )}
            <PageSheet key={`${view.doc.id}:${view.page}`} doc={view.doc} page={view.page} mark={view.mark} />
          </>
        )}
      </div>
    </aside>
  );
}

function PageSheet({ doc, page, mark }: { doc: Doc; page: number; mark: Mark | null }) {
  const { result, reload } = useFetched(`${doc.id}:${page}`, () => api.page(doc.id, page));
  if (result === null) return <SheetSkeleton />;
  if (!result.ok) {
    return (
      <div className="pane-error">
        <p className="notice" data-tone="failed">{explain(result.error)}</p>
        <button type="button" className="button" onClick={reload}>Try again</button>
      </div>
    );
  }
  if (doc.layout?.emptyPages.includes(page) || result.value.text.trim() === "") {
    return <div className="sheet"><p className="quiet">This {result.value.label.startsWith("section") ? "section" : "page"} has no text layer.</p></div>;
  }
  return <MarkedText page={result.value} mark={mark} />;
}

/** The page text with the quote marked: a canary passage and a canary flag on the page edge. */
function MarkedText({ page, mark }: { page: Page; mark: Mark | null }) {
  const sheet = useRef<HTMLDivElement>(null);
  const marked = useRef<HTMLElement>(null);
  const flag = useRef<HTMLSpanElement>(null);
  const match = mark ? locateQuote(page.text, mark.quote) : null;

  useLayoutEffect(() => {
    const el = marked.current;
    const box = sheet.current;
    const tab = flag.current;
    if (!el || !box || !tab) return;
    const place = () => {
      const rects = el.getClientRects();
      const first = rects[0];
      const last = rects[rects.length - 1];
      if (!first || !last) return;
      const top = box.getBoundingClientRect().top;
      tab.style.top = `${first.top - top}px`;
      tab.style.height = `${last.bottom - first.top}px`;
    };
    place();
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
    el.focus({ preventScroll: true });
    const observer = new ResizeObserver(place);
    observer.observe(box);
    return () => observer.disconnect();
    // A new quote on the same page moves the mark, so it re-runs the placement and the scroll.
  }, [page.text, mark?.quote]);

  return (
    <div className="sheet" ref={sheet}>
      {match?.kind === "missing" && (
        <div className="unlocated">
          <p className="quiet">This passage could not be located on this page.</p>
          <blockquote>{mark?.quote}</blockquote>
        </div>
      )}
      <p className="sheet-text">
        {match?.kind === "exact" ? (
          <>
            {page.text.slice(0, match.start)}
            <mark key={mark?.quote} ref={marked} tabIndex={-1} aria-label={`Passage for ${mark?.label}`}>
              {page.text.slice(match.start, match.end)}
            </mark>
            {page.text.slice(match.end)}
          </>
        ) : (
          page.text
        )}
      </p>
      {match?.kind === "exact" && <span className="flag" ref={flag} aria-hidden="true">{mark?.flag}</span>}
    </div>
  );
}

function SheetSkeleton() {
  return (
    <div className="sheet" aria-busy="true">
      {[88, 96, 70, 92, 60].map((w, i) => <span key={i} className="skeleton line" style={{ width: `${w}%` }} />)}
    </div>
  );
}
