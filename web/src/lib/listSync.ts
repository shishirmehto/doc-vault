// Keeps the list read from the server and the updates applied around that read consistent.

import type { DocList } from "./docList.ts";

/** A change to the list: a live event, or a response already in hand such as an upload's. */
export type Update = (list: DocList) => DocList;

/**
 * A list read is a snapshot taken at some moment while the request is in flight, so an update applied after the
 * request started may or may not be in it. Replaying all of them, in order, onto the snapshot is right either way:
 * each update carries the server's state at its moment, and the later ones follow it.
 */
export class ListSync {
  private list: DocList | null = null;
  private fetches = 0;
  /** The updates applied since the newest fetch started, while that fetch is in flight. */
  private since: Update[] | null = null;

  /** Null until the first fetch resolves. */
  get shown(): DocList | null {
    return this.list;
  }

  /** Returns the number to resolve or fail this fetch with. A fetch already in flight is dropped. */
  startFetch(): number {
    this.since = [];
    return ++this.fetches;
  }

  apply(update: Update): void {
    if (this.list !== null) this.list = update(this.list);
    this.since?.push(update);
  }

  /** False when a newer fetch started after this one, so its result is dropped. */
  resolve(fetch: number, list: DocList): boolean {
    if (fetch !== this.fetches || this.since === null) return false;
    this.list = this.since.reduce((l, update) => update(l), list);
    this.since = null;
    return true;
  }

  /** False when a newer fetch started after this one, so its failure is dropped too. */
  fail(fetch: number): boolean {
    if (fetch !== this.fetches || this.since === null) return false;
    this.since = null;
    return true;
  }
}
