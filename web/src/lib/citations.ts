// A remark plugin for a grounded answer: each [n] in its text becomes a cite node, which remark-rehype turns into
// <button data-ref="n">. Markdown leaves "[1]" and "[1][2]" as text because an answer defines no link labels.

import type { Node, Parent, Root, RootContent, Text } from "mdast";
import remarkBreaks from "remark-breaks";

export interface Cite extends Node {
  type: "cite";
  data: { hName: "button"; hProperties: { dataRef: number } };
}

declare module "mdast" {
  interface PhrasingContentMap { cite: Cite }
  interface RootContentMap { cite: Cite }
}

const MARKER = /\[(\d+)\]/g;

/** The remark plugins an answer is rendered with. remark-breaks keeps a single newline as a line break, as answers
 * written before Markdown was allowed put one item per line. */
export const ANSWER_PLUGINS = [remarkBreaks, remarkCitations];

export function remarkCitations() {
  return (tree: Root) => split(tree);
}

function split(parent: Parent): void {
  parent.children = parent.children.flatMap<RootContent>((child) => {
    if (child.type === "text") return cut(child);
    if ("children" in child) split(child);
    return [child];
  });
}

/** The spaces before a marker go, so a chip sits on the word it follows and never wraps to a line of its own. */
function cut(text: Text): (Text | Cite)[] {
  const parts: (Text | Cite)[] = [];
  let at = 0;
  for (const marker of text.value.matchAll(MARKER)) {
    const before = text.value.slice(at, marker.index).trimEnd();
    if (before !== "") parts.push({ type: "text", value: before });
    parts.push({ type: "cite", data: { hName: "button", hProperties: { dataRef: Number(marker[1]) } } });
    at = marker.index + marker[0].length;
  }
  if (at === 0) return [text];
  if (at < text.value.length) parts.push({ type: "text", value: text.value.slice(at) });
  return parts;
}
