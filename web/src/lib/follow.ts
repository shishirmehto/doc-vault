/** The column's scroll position and size, in pixels. */
export type ScrollAt = { top: number; height: number; client: number };

/**
 * A streamed answer is followed until the reader moves the column up, by wheel, keys, scrollbar or touch alike. A
 * move down, the column's own jump to the end, or the browser pulling the column up when the text shrinks (a retry
 * clears it) leaves following on.
 */
export function stillFollowing(following: boolean, before: number, now: ScrollAt): boolean {
  return following && (now.top >= before || now.height - now.top - now.client <= 1);
}
