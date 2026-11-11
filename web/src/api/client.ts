// One function per endpoint. Browser only: every call goes to same-origin /api/*, which
// next.config.ts rewrites to the API. Every call returns Result<T> and never throws.

import type {
  AskProgress, Chat, ChatId, Comparison, ComparisonId, Doc, DocumentId, DocumentMetrics, Health, Insights, MetricsWindow,
  NonEmpty, Page, ProcessingMetrics, Result, Summary, SummaryList, SummaryOptions, Turn, UploadReceipt,
} from "./domain.ts";
import * as P from "./parse.ts";
import { createSseReader } from "./sse.ts";
import type * as W from "./wire.ts";
import { LIST_LIMIT, type DocList } from "../lib/docList.ts";

const BASE = "/api";

export const api = {
  /**
   * The latest version of the newest LIST_LIMIT documents. Earlier versions, and documents older than these, are read
   * one at a time. Older documents need cursor paging, which pass 1 leaves out.
   */
  documents: (): Promise<Result<DocList>> => call("GET", `/documents?latest=true&limit=${LIST_LIMIT}`, P.parseDocumentPage),

  /** Every version of the document's family, oldest first. */
  versions: (id: DocumentId): Promise<Result<Doc[]>> => call("GET", `/documents/${id}/versions`, P.parseVersionList),

  /**
   * Removes the document, its pages and its insights. Chats keep their turns, whose citations carry their own
   * quotes. A 404 means it is already gone.
   */
  deleteDocument: (id: DocumentId): Promise<Result<null>> => call("DELETE", `/documents/${id}`, () => null),

  /** One document, for a link to one older than the newest 100 the list holds. */
  document: (id: DocumentId): Promise<Result<Doc>> => call("GET", `/documents/${id}`, P.parseDoc),

  /** Re-runs whichever stage failed: indexing for a failed document, analysis for failed insights. */
  retry: (id: DocumentId): Promise<Result<Doc>> => call("POST", `/documents/${id}/retry`, P.parseDoc),

  insights: (id: DocumentId): Promise<Result<Insights>> =>
    call("GET", `/documents/${id}/insights`, (w: W.InsightsOut) => P.parseInsights(w, id)),

  page: (id: DocumentId, n: number): Promise<Result<Page>> =>
    call("GET", `/documents/${id}/pages/${n}`, P.parsePage),

  health: (): Promise<Result<Health>> => call("GET", "/health", P.parseHealth, { dataStatuses: [503] }),

  /** 409 documents_not_ready names the documents that are not ready. */
  startChat: (ids: NonEmpty<DocumentId>): Promise<Result<Chat>> =>
    call("POST", "/chats", P.parseChat, { body: { document_ids: [...ids] } satisfies W.StartChatIn }),

  /** Its documents, with any deleted since it started marked. */
  chat: (id: ChatId): Promise<Result<Chat>> => call("GET", `/chats/${id}`, P.parseChat),

  /** The newest five, for the new-chat screen. */
  recentChats: (): Promise<Result<Chat[]>> => call("GET", "/chats?limit=5", P.parseChatPage),

  /**
   * Compares 2 to 5 ready documents, which become the columns in this order. A blank focus compares the fields a
   * broker compares. 409 documents_not_ready names the documents that are not ready; 429 shares the ask budget.
   */
  compare: (ids: readonly DocumentId[], focus: string | null): Promise<Result<Comparison>> =>
    call("POST", "/comparisons", P.parseComparison, {
      body: { document_ids: [...ids], focus: focus?.trim() || null } satisfies W.CompareIn,
    }),

  /** One stored comparison, with any document deleted since marked. */
  comparison: (id: ComparisonId): Promise<Result<Comparison>> => call("GET", `/comparisons/${id}`, P.parseComparison),

  /** The newest `limit`, newest first. */
  comparisons: (limit: number): Promise<Result<Comparison[]>> =>
    call("GET", `/comparisons?limit=${limit}`, P.parseComparisonPage),

  /**
   * Writes the document's summary again with these options. 201 is a new summary; 200 is the one stored for the same
   * options, served with no model call. 409 documents_not_ready until it is ready; 429 shares the ask budget.
   */
  summarize: (id: DocumentId, { length, focus, tone }: SummaryOptions): Promise<Result<Summary>> =>
    call("POST", `/documents/${id}/summaries`, P.parseSummary, { body: { length, focus, tone } satisfies W.SummaryIn }),

  /** The newest `limit` of the document's summaries, newest first. */
  summaries: (id: DocumentId, limit: number): Promise<Result<SummaryList>> =>
    call("GET", `/documents/${id}/summaries?limit=${limit}`, P.parseSummaryPage),

  documentMetrics: (): Promise<Result<DocumentMetrics>> => call("GET", "/metrics/documents", P.parseDocumentMetrics),

  processingMetrics: (window: MetricsWindow): Promise<Result<ProcessingMetrics>> =>
    call("GET", `/metrics/processing?window=${window}`, P.parseProcessingMetrics),

  /** Records exactly one turn on success. A 503 or a 429 records none. */
  ask: (chat: ChatId, question: string): Promise<Result<Turn>> =>
    call("POST", `/chats/${chat}/messages`, P.parseTurn, { body: { question } satisfies W.AskIn }),

  /**
   * The same ask, streamed: onProgress hears each stage, piece of provisional text and reset as it arrives,
   * and the result is the final turn. A refusal before the stream starts (404, 409, 422, 429) and an error
   * event are "refused". A body that ends without a turn or an error is "offline", like a lost response.
   */
  askStream: async (chat: ChatId, question: string, onProgress: (p: AskProgress) => void): Promise<Result<Turn>> => {
    let res: Response;
    try {
      res = await fetch(`${BASE}/chats/${chat}/messages/stream`, {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ question } satisfies W.AskIn),
      });
    } catch {
      return { ok: false, error: { kind: "offline" } };
    }
    if (res.status !== 200 || res.body === null) {
      return settle(res.status, await res.text(), res.headers.get("Retry-After"), P.parseTurn);
    }
    return readAskStream(res.body, onProgress);
  },

  /** Oldest first. 200 is the API's maximum. */
  history: (chat: ChatId): Promise<Result<Turn[]>> => call("GET", `/chats/${chat}/messages?limit=200`, P.parseHistory),

  /**
   * XHR, because fetch reports no upload progress. 202 is a new document; 200 means the same
   * bytes were uploaded before and nothing was reprocessed. 413, 415, 422 and 429 arrive as "refused".
   * With `replaces`, new bytes become the next version of that document's family whatever their name; 404 when it is
   * gone. Without it, new bytes under a stored document's name become its next version.
   */
  upload: (file: File, onProgress: (sent: number, total: number) => void, replaces: DocumentId | null = null): Promise<Result<UploadReceipt>> =>
    new Promise((resolve) => {
      const form = new FormData();
      form.append("file", file, file.name);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${BASE}/documents${replaces ? `?replaces=${replaces}` : ""}`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded, e.total);
      };
      xhr.onload = () =>
        resolve(settle(xhr.status, xhr.responseText, xhr.getResponseHeader("Retry-After"),
          (w: W.DocumentOut) => ({ doc: P.parseDoc(w), duplicate: xhr.status === 200 })));
      xhr.onerror = () => resolve({ ok: false, error: { kind: "offline" } });
      xhr.send(form);
    }),
} as const;

async function call<WireT, T>(
  method: "GET" | "POST" | "DELETE",
  path: string,
  parse: (w: WireT) => T,
  { body, dataStatuses = [] }: { body?: object; dataStatuses?: readonly number[] } = {},
): Promise<Result<T>> {
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      method,
      cache: "no-store",
      ...(body && { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    });
  } catch {
    return { ok: false, error: { kind: "offline" } };
  }
  return settle(res.status, await res.text(), res.headers.get("Retry-After"), parse, dataStatuses);
}

/**
 * Reads a streamed ask's body to its turn or error event. Bytes are decoded in stream mode, so a character
 * split across network chunks arrives whole.
 */
export async function readAskStream(body: ReadableStream<Uint8Array>, onProgress: (p: AskProgress) => void): Promise<Result<Turn>> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const sse = createSseReader();
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch {
      return { ok: false, error: { kind: "offline" } };
    }
    if (chunk.done) return { ok: false, error: { kind: "offline" } };
    for (const message of sse.push(decoder.decode(chunk.value, { stream: true }))) {
      let event: P.AskEvent;
      try {
        event = P.parseAskEvent(message);
      } catch (err) {
        void reader.cancel();
        return { ok: false, error: { kind: "unexpected", detail: err instanceof Error ? err.message : String(err) } };
      }
      if (event.kind === "turn" || event.kind === "error") {
        void reader.cancel();
        return event.kind === "turn" ? { ok: true, value: event.turn } : { ok: false, error: event.error };
      }
      onProgress(event);
    }
  }
}

/** Shared by fetch and XHR, and the only code that calls JSON.parse or a parser. */
export function settle<WireT, T>(
  status: number,
  text: string,
  retryAfter: string | null,
  parse: (w: WireT) => T,
  dataStatuses: readonly number[] = [],
): Result<T> {
  if (status === 0) return { ok: false, error: { kind: "offline" } };
  if ((status >= 200 && status < 300) || dataStatuses.includes(status)) {
    try {
      return { ok: true, value: parse((status === 204 ? null : JSON.parse(text)) as WireT) };
    } catch (err) {
      return { ok: false, error: { kind: "unexpected", detail: err instanceof Error ? err.message : String(err) } };
    }
  }
  let body: unknown = null;
  try {
    body = JSON.parse(text);
  } catch {
    // A proxy error page or an empty body: parseError reads null as "broken".
  }
  return { ok: false, error: P.parseError(status, body, retryAfter) };
}
