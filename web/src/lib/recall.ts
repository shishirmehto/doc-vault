export type Caret = { start: number; end: number };

/**
 * What Up ("older") or Down ("newer") does in the question box. Null: the key stays the textarea's cursor key.
 * `asked` is the chat's questions, oldest first, each text once. Recall keeps no state: the box's place in the
 * list is where its text is, so there is nothing to reset when the box is sent, edited or cleared.
 *   older: an empty box takes the newest question. A box that shows a past question unedited, with the caret on
 *          its first line, takes the one before it. At the oldest: null.
 *   newer: a box that shows a past question unedited, with the caret on its last line, takes the next one. Past
 *          the newest: "" (the box empties).
 *   Anything else is a draft: null. A typed draft is never replaced.
 * A line ends at "\n": a long question the box wraps is one line, so Up from its end recalls. A selection is never
 * recall. The caller leaves out modifier keys and IME composition.
 */
export function recalled(asked: readonly string[], value: string, to: "older" | "newer", caret: Caret): string | null {
  if (caret.start !== caret.end) return null;
  if (value === "") return to === "older" ? (asked.at(-1) ?? null) : null;
  const at = asked.lastIndexOf(value);
  if (at === -1) return null;
  if (to === "older") return at === 0 || value.slice(0, caret.start).includes("\n") ? null : (asked[at - 1] ?? null);
  return value.slice(caret.end).includes("\n") ? null : (asked[at + 1] ?? "");
}
