// One function per endpoint. Browser only: every call goes to same-origin /api/*, which
// next.config.ts rewrites to the API. Every call returns Result<T> and never throws. Each request sends a fresh
// X-Request-ID, and a failure carries the id the API echoed, so a reader can quote it.

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
  documents: (): Promise<Result<DocList>> => call("GET", `/documents?latest=true&limit=${LIST_LIMIT}`, P.parseDocumentPage),

  versions: (id: DocumentId): Promise<Result<Doc[]>> => call("GET", `/documents/${id}/versions`, P.parseVersionList),

  deleteDocument: (id: DocumentId): Promise<Result<null>> => call("DELETE", `/documents/${id}`, () => null),

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

  compare: (ids: readonly DocumentId[], focus: string | null): Promise<Result<Comparison>> =>
    call("POST", "/comparisons", P.parseComparison, {
      body: { document_ids: [...ids], focus: focus?.trim() || null } satisfies W.CompareIn,
    }),

  comparison: (id: ComparisonId): Promise<Result<Comparison>> => call("GET", `/comparisons/${id}`, P.parseComparison),

  comparisons: (limit: number): Promise<Result<Comparison[]>> =>
    call("GET", `/comparisons?limit=${limit}`, P.parseComparisonPage),

  summarize: (id: DocumentId, { length, focus, tone }: SummaryOptions): Promise<Result<Summary>> =>
    call("POST", `/documents/${id}/summaries`, P.parseSummary, { body: { length, focus, tone } satisfies W.SummaryIn }),

  summaries: (id: DocumentId, limit: number): Promise<Result<SummaryList>> =>
    call("GET", `/documents/${id}/summaries?limit=${limit}`, P.parseSummaryPage),

  documentMetrics: (): Promise<Result<DocumentMetrics>> => call("GET", "/metrics/documents", P.parseDocumentMetrics),

  processingMetrics: (window: MetricsWindow): Promise<Result<ProcessingMetrics>> =>
    call("GET", `/metrics/processing?window=${window}`, P.parseProcessingMetrics),

  /** `onAccepted` hears the 200 that begins the stream: from then on the server answers whatever happens to it. */
  askStream: async (chat: ChatId, question: string, onProgress: (p: AskProgress) => void,
    onAccepted: () => void = () => {}): Promise<Result<Turn>> => {
    const id = newRequestId();
    let res: Response;
    try {
      res = await fetch(`${BASE}/chats/${chat}/messages/stream`, {
        method: "POST",
        cache: "no-store",
        headers: { ...idHeader(id), "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ question } satisfies W.AskIn),
      });
    } catch {
      return { ok: false, error: { kind: "offline" } };
    }
    const headers = heard((name) => res.headers.get(name), id);
    if (res.status !== 200 || res.body === null) {
      const text = await bodyText(res);
      return text === null ? { ok: false, error: { kind: "offline" } } : settle(res.status, text, headers, P.parseTurn);
    }
    onAccepted();
    return readAskStream(res.body, onProgress, headers.requestId);
  },

  /** Oldest first. 200 is the API's maximum. */
  history: (chat: ChatId): Promise<Result<Turn[]>> => call("GET", `/chats/${chat}/messages?limit=200`, P.parseHistory),

  /**
   * XHR, because fetch reports no upload progress. 202 is a new document; 200 means the same
   * bytes were uploaded before and nothing was reprocessed. 413, 415, 422 and 429 arrive as "refused".
   */
  upload: (file: File, onProgress: (sent: number, total: number) => void, replaces: DocumentId | null = null): Promise<Result<UploadReceipt>> =>
    new Promise((resolve) => {
      const form = new FormData();
      form.append("file", file, file.name);
      const id = newRequestId();
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${BASE}/documents${replaces ? `?replaces=${replaces}` : ""}`);
      Object.entries(idHeader(id)).forEach(([name, value]) => xhr.setRequestHeader(name, value));
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded, e.total);
      };
      xhr.onload = () =>
        resolve(settle(xhr.status, xhr.responseText, heard((name) => xhr.getResponseHeader(name), id),
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
  const id = newRequestId();
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      method,
      cache: "no-store",
      headers: { ...idHeader(id), ...(body && { "Content-Type": "application/json" }) },
      ...(body && { body: JSON.stringify(body) }),
    });
  } catch {
    return { ok: false, error: { kind: "offline" } };
  }
  const text = await bodyText(res);
  if (text === null) return { ok: false, error: { kind: "offline" } };
  return settle(res.status, text, heard((name) => res.headers.get(name), id), parse, dataStatuses);
}

/** Null when the connection drops after the headers, before the body ends. */
async function bodyText(res: Response): Promise<string | null> {
  try {
    return await res.text();
  } catch {
    return null;
  }
}

/** A page served over plain HTTP from another host has no randomUUID. The API then makes the id, and echoes it. */
function newRequestId(): string | null {
  return typeof crypto.randomUUID === "function" ? crypto.randomUUID() : null;
}

function idHeader(id: string | null): Record<string, string> {
  return id === null ? {} : { "X-Request-ID": id };
}

/** What settle reads from a response's headers. */
export type ReplyHeaders = { retryAfter: string | null; requestId: string | null };

/** The id the API echoed, else the one the request sent. */
function heard(header: (name: string) => string | null, sent: string | null): ReplyHeaders {
  return { retryAfter: header("Retry-After"), requestId: header("X-Request-ID") ?? sent };
}

/**
 * `requestId` is the 200's, which an error event carries.
 */
export async function readAskStream(
  body: ReadableStream<Uint8Array>,
  onProgress: (p: AskProgress) => void,
  requestId: string | null,
): Promise<Result<Turn>> {
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
        event = P.parseAskEvent(message, requestId);
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
  headers: ReplyHeaders,
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
  return { ok: false, error: P.parseError(status, body, headers.retryAfter, headers.requestId) };
}
