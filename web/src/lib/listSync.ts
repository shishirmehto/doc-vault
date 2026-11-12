import type { DocList } from "./docList.ts";

/** A change to the list: a live event, or a response already in hand such as an upload's. */
export type Update<T = DocList> = (list: T) => T;

/**
 * A list read, or a document family's (useVersions), is a snapshot taken at some moment while the request is in flight, so an update applied after the
 * request started may or may not be in it. Replaying all of them, in order, onto the snapshot converges either way
 * for stream events: each carries the server's state at its moment, and every later commit sends a later event.
 * An upload or retry response can be older than an event that arrived before it; the next event for that
 * document corrects it.
 */
export class ListSync<T = DocList> {
  private list: T | null = null;
  private fetches = 0;
  private since: Update<T>[] | null = null;

  get shown(): T | null {
    return this.list;
  }

  startFetch(): number {
    this.since = [];
    return ++this.fetches;
  }

  apply(update: Update<T>): void {
    if (this.list !== null) this.list = update(this.list);
    this.since?.push(update);
  }

  resolve(fetch: number, list: T): boolean {
    if (fetch !== this.fetches || this.since === null) return false;
    this.list = this.since.reduce((l, update) => update(l), list);
    this.since = null;
    return true;
  }

  fail(fetch: number): boolean {
    if (fetch !== this.fetches || this.since === null) return false;
    this.since = null;
    return true;
  }
}
