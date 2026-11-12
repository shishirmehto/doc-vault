import type { DocEvent } from "../api/domain.ts";
import { parseDocEvent } from "../api/parse.ts";

export type FeedEvent = DocEvent | { kind: "open" } | { kind: "down" };

export type LiveStatus = "off" | "live" | "paused";

export type FeedSource = {
  readonly readyState: number;
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  close(): void;
};

// EventSource.CLOSED. Node, which runs the tests, has no EventSource to read it from.
const CLOSED = 2;
const FIRST_RETRY_MS = 2000;
const LAST_RETRY_MS = 30_000;
const PAUSED_AFTER_MS = 3000;
// "message" is an event with no name. The contract has none, so one reads as resync, like data that fails to parse.
const MESSAGES = ["document", "deleted", "resync", "message"] as const;

function parsed(event: string, data: string): DocEvent {
  try {
    return parseDocEvent({ event, data });
  } catch {
    return { kind: "resync" };
  }
}

export class LiveFeed {
  private readonly connect: () => FeedSource;
  private readonly listeners = new Set<(event: FeedEvent) => void>();
  private readonly watchers = new Set<() => void>();
  private source: FeedSource | null = null;
  private isOpen = false;
  private retryMs = FIRST_RETRY_MS;
  private retry: ReturnType<typeof setTimeout> | null = null;
  private pausing: ReturnType<typeof setTimeout> | null = null;
  private current: LiveStatus = "off";
  private hidden = false;

  constructor(connect: () => FeedSource) {
    this.connect = connect;
  }

  get open(): boolean {
    return this.isOpen;
  }

  status = (): LiveStatus => this.current;

  subscribe = (listener: (event: FeedEvent) => void): (() => void) => {
    this.listeners.add(listener);
    if (this.listeners.size === 1 && !this.hidden) this.connectNow();
    return () => {
      if (!this.listeners.delete(listener) || this.listeners.size > 0) return;
      this.stop();
      this.setStatus("off");
    };
  };

  /**
   * Over HTTP/1.1
   * a browser allows six connections to one host across all its tabs, so streams held by background tabs would
   * leave the visible one none for its requests.
   */
  setHidden = (hidden: boolean): void => {
    if (hidden === this.hidden) return;
    this.hidden = hidden;
    if (this.listeners.size === 0) return;
    if (!hidden) {
      this.connectNow();
      return;
    }
    const wasOpen = this.isOpen;
    this.stop();
    if (wasOpen) this.emit({ kind: "down" });
  };

  watch = (watcher: () => void): (() => void) => {
    this.watchers.add(watcher);
    return () => {
      this.watchers.delete(watcher);
    };
  };

  private connectNow(): void {
    this.setStatus("live");
    this.startPausing();
    this.start();
  }

  private stop(): void {
    this.source?.close();
    this.source = null;
    this.isOpen = false;
    this.retryMs = FIRST_RETRY_MS;
    if (this.retry !== null) clearTimeout(this.retry);
    if (this.pausing !== null) clearTimeout(this.pausing);
    this.retry = this.pausing = null;
  }

  private start(): void {
    const source = this.connect();
    this.source = source;
    const current = () => this.source === source;
    source.addEventListener("open", () => {
      if (!current()) return;
      this.retryMs = FIRST_RETRY_MS;
      this.isOpen = true;
      if (this.pausing !== null) clearTimeout(this.pausing);
      this.pausing = null;
      this.setStatus("live");
      this.emit({ kind: "open" });
    });
    source.addEventListener("error", () => {
      if (!current()) return;
      if (this.isOpen) {
        this.isOpen = false;
        this.startPausing();
        this.emit({ kind: "down" });
      }
      // CONNECTING means the browser retries by itself. CLOSED (a non-200, such as the 503 past 100 streams, or a
      // proxy error while the API restarts) means it never will.
      if (source.readyState !== CLOSED) return;
      source.close();
      this.source = null;
      this.retry = setTimeout(() => {
        this.retry = null;
        this.start();
      }, this.retryMs);
      this.retryMs = Math.min(this.retryMs * 2, LAST_RETRY_MS);
    });
    for (const name of MESSAGES) {
      source.addEventListener(name, (event) => {
        if (current()) this.emit(parsed(name, event.data));
      });
    }
  }

  private startPausing(): void {
    this.pausing = setTimeout(() => {
      this.pausing = null;
      this.setStatus("paused");
    }, PAUSED_AFTER_MS);
  }

  private setStatus(status: LiveStatus): void {
    if (status === this.current) return;
    this.current = status;
    this.watchers.forEach((w) => w());
  }

  private emit(event: FeedEvent): void {
    [...this.listeners].forEach((l) => l(event));
  }
}
