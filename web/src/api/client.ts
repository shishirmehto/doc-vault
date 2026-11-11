// One function per endpoint. Browser only: every call goes to same-origin /api/*, which
// next.config.ts rewrites to the API. Every call returns Result<T> and never throws.

import type { Doc, DocumentId, Health, Insights, Page, Result, UploadReceipt } from "./domain.ts";
import * as P from "./parse.ts";
import type * as W from "./wire.ts";

const BASE = "/api";

export const api = {
  /** The newest 100. Older documents need cursor paging, which pass 1 leaves out. */
  documents: (): Promise<Result<{ docs: Doc[]; complete: boolean }>> =>
    call("GET", "/documents?limit=100", P.parseDocumentPage),

  /** Re-runs whichever stage failed: indexing for a failed document, analysis for failed insights. */
  retry: (id: DocumentId): Promise<Result<Doc>> => call("POST", `/documents/${id}/retry`, P.parseDoc),

  insights: (id: DocumentId): Promise<Result<Insights>> =>
    call("GET", `/documents/${id}/insights`, (w: W.InsightsOut) => P.parseInsights(w, id)),

  page: (id: DocumentId, n: number): Promise<Result<Page>> =>
    call("GET", `/documents/${id}/pages/${n}`, P.parsePage),

  health: (): Promise<Result<Health>> => call("GET", "/health", P.parseHealth, [503]),

  /**
   * XHR, because fetch reports no upload progress. 202 is a new document; 200 means the same
   * bytes were uploaded before and nothing was reprocessed. 413, 415 and 422 arrive as "refused".
   */
  upload: (file: File, onProgress: (sent: number, total: number) => void): Promise<Result<UploadReceipt>> =>
    new Promise((resolve) => {
      const form = new FormData();
      form.append("file", file, file.name);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${BASE}/documents`);
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
  method: "GET" | "POST",
  path: string,
  parse: (w: WireT) => T,
  dataStatuses: readonly number[] = [],
): Promise<Result<T>> {
  let res: Response;
  try {
    res = await fetch(BASE + path, { method, cache: "no-store" });
  } catch {
    return { ok: false, error: { kind: "offline" } };
  }
  return settle(res.status, await res.text(), res.headers.get("Retry-After"), parse, dataStatuses);
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
      return { ok: true, value: parse(JSON.parse(text) as WireT) };
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
