"use client";

import { useSyncExternalStore } from "react";
import { LiveFeed, type LiveStatus } from "@/lib/liveFeed";

/** The page's one stream of document changes. It connects on the first subscribe, which useDocuments makes. */
export const liveFeed = new LiveFeed(() => new EventSource("/api/events/documents"));
if (typeof document !== "undefined") {
  liveFeed.setHidden(document.hidden);
  document.addEventListener("visibilitychange", () => liveFeed.setHidden(document.hidden));
}

/** Reading the status never opens the stream, so it stays "off" on a page that lists no documents. */
export function useLiveStatus(): LiveStatus {
  return useSyncExternalStore(liveFeed.watch, liveFeed.status, () => "off");
}
