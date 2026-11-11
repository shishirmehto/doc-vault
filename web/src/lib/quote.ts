// A mirror of docvault/pages.py `locate_quote`, with the same folding, so the span the page
// sheet marks is the span the backend verified. Offsets index the original text in UTF-16 units.

export type QuoteMatch = { kind: "exact"; start: number; end: number } | { kind: "missing" };

/** pages.py `_FOLD`: typographic quotes and dashes to ASCII. */
const FOLD: Readonly<Record<string, string>> = {
  "‘": "'", "’": "'", "‚": "'", "′": "'",
  "“": '"', "”": '"', "„": '"', "″": '"',
  "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
};

/** Exactly the characters for which Python's str.isspace is true. JS \s differs (no U+001C-U+001F or U+0085; adds U+FEFF). */
const SPACE = /[\t\n\v\f\r\u001c-\u001f \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u;

/** pages.py `_normalise`: fold, lower-case, collapse each whitespace run to one space. */
function normalise(text: string): { folded: string; origin: number[]; width: number[] } {
  let folded = "";
  const origin: number[] = [];
  const width: number[] = [];
  let i = 0;
  for (const ch of text) {
    if (SPACE.test(ch)) {
      if (folded !== "" && !folded.endsWith(" ")) {
        folded += " ";
        origin.push(i);
        width.push(ch.length);
      }
    } else {
      for (const out of (FOLD[ch] ?? ch).toLowerCase()) {
        folded += out;
        for (let k = 0; k < out.length; k++) {
          origin.push(i);
          width.push(ch.length);
        }
      }
    }
    i += ch.length;
  }
  return { folded, origin, width };
}

export function locateQuote(text: string, quote: string): QuoteMatch {
  const needle = normalise(quote).folded.trim();
  if (needle === "") return { kind: "missing" };
  const { folded, origin, width } = normalise(text);
  const at = folded.indexOf(needle);
  if (at < 0) return { kind: "missing" };
  const last = at + needle.length - 1;
  return { kind: "exact", start: origin[at]!, end: origin[last]! + width[last]! };
}
