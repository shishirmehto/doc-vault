import { createContext, useContext } from "react";
import Markdown, { type Components, type ExtraProps } from "react-markdown";
import type { Citation } from "@/api/domain";
import { remarkCitations } from "@/lib/citations";

type Cite = { citations: readonly Citation[]; active: number | null; onCite(citation: Citation): void };

// The chip reads its citation from context, so COMPONENTS stays one object: a button component made per render
// would be a new component type each time, and React would remount the chip that was just clicked and drop its focus.
const Cited = createContext<Cite | null>(null);
const PLUGINS = [remarkCitations];
// An image has no children for unwrapDisallowed to keep, so it is allowed and drawn as its alt text, never an <img>.
const ALLOWED = ["p", "ul", "ol", "li", "strong", "em", "br", "button", "img"];
const COMPONENTS: Components = { button: Chip, img: ({ alt }) => alt ?? null };

/**
 * An answer's Markdown as paragraphs, lists and emphasis. Anything else (a heading, a link, an image, code) shows
 * as its text, and raw HTML stays escaped. Each [n] is a chip that opens citation n; with no `cite`, or no citation
 * n, it stays "[n]".
 */
export function AnswerText({ text, cite }: { text: string; cite?: Cite }) {
  return (
    <Cited value={cite ?? null}>
      <Markdown remarkPlugins={PLUGINS} allowedElements={ALLOWED} unwrapDisallowed components={COMPONENTS}>
        {text}
      </Markdown>
    </Cited>
  );
}

function Chip({ node }: ExtraProps) {
  const cite = useContext(Cited);
  const ref = Number(node?.properties.dataRef);
  const citation = cite?.citations.find((c) => c.ref === ref);
  if (!cite || !citation) return `[${ref}]`;
  return (
    <button type="button" className="cite" aria-pressed={cite.active === ref}
      aria-label={`Source ${ref}: ${citation.filename}, ${citation.pageLabel}`}
      title={`${citation.filename}, ${citation.pageLabel}`} onClick={() => cite.onCite(citation)}>
      {ref}
    </button>
  );
}
