"use client";

import { useSyncExternalStore } from "react";
import { LiveFeed, type LiveStatus } from "@/lib/liveFeed";

export const liveFeed = new LiveFeed(() => new EventSource("/api/events/documents"));
if (typeof document !== "undefined") {
  liveFeed.setHidden(document.hidden);
  document.addEventListener("visibilitychange", () => liveFeed.setHidden(document.hidden));
}

export function useLiveStatus(): LiveStatus {
  return useSyncExternalStore(liveFeed.watch, liveFeed.status, () => "off");
}
