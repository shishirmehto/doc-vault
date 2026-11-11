// The one place wire shapes become domain types, and URL parameters become a Selection.
// Pure functions. client.ts calls one parser per response; a throw here becomes ApiError "unexpected".

import type * as W from "./wire.ts";
import type {
  ApiError, CiteKey, Doc, DocumentId, Failure, Health, Insights, Lifecycle, Page, PaneRequest, Selection,
} from "./domain.ts";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The only constructor of a DocumentId. */
export function asDocumentId(raw: string): DocumentId | null {
  return UUID.test(raw) ? (raw as DocumentId) : null;
}

function documentId(raw: string): DocumentId {
  const id = asDocumentId(raw);
  if (id === null) throw new TypeError(`not a document id: ${raw}`);
  return id;
}

function failure(w: W.ErrorBody): Failure {
  return { code: w.code, message: w.message, retryable: w.retryable ?? false };
}

function lifecycle(w: W.DocumentOut): Lifecycle {
  switch (w.status) {
    case "queued":
    case "parsing":
    case "ready":
      return { status: w.status };
    case "embedding":
      return { status: "embedding", embedded: w.progress?.embedded_chunks ?? 0, total: w.progress?.total_chunks ?? 0 };
    case "failed":
      return {
        status: "failed",
        stage: w.pages === null ? "parse" : "embed",
        failure: w.error ? failure(w.error) : { code: "internal_error", message: "Processing failed.", retryable: true },
      };
    default: {
      const unknown: never = w.status;
      throw new TypeError(`unknown document status: ${String(unknown)}`);
    }
  }
}

export function parseDoc(w: W.DocumentOut): Doc {
  return {
    id: documentId(w.id),
    filename: w.filename,
    kind: w.kind,
    sizeBytes: w.size_bytes,
    lifecycle: lifecycle(w),
    insights: w.insights_status,
    layout: w.pages === null
      ? null
      : { pageCount: w.pages, unit: w.page_basis === "synthetic" ? "section" : "page", emptyPages: w.empty_pages },
    processingMs: w.processing_ms,
    createdAt: new Date(w.created_at),
  };
}

/** `complete` is false when older documents exist past this page. */
export function parseDocumentPage(w: W.DocumentPage): { docs: Doc[]; complete: boolean } {
  return { docs: w.items.map(parseDoc), complete: w.next_cursor === null };
}

export function parsePage(w: W.PageOut): Page {
  return { documentId: documentId(w.document_id), number: w.number, label: w.label, text: w.text };
}

export function parseInsights(w: W.InsightsOut, id: DocumentId): Insights {
  switch (w.status) {
    case "pending":
      return { status: "pending" };
    case "failed":
      return { status: "failed", failure: failure(w.error) };
    case "unavailable":
      return { status: "unavailable", reason: w.reason };
    case "ready":
      return {
        status: "ready",
        title: w.title,
        documentType: w.document_type,
        summary: w.summary,
        keyFacts: w.key_facts.map((f) => ({
          label: f.label,
          value: f.value,
          pageLabel: f.page_label,
          source: { documentId: id, page: f.page, quote: f.quote },
        })),
        droppedKeyFacts: w.dropped_key_facts,
        tags: w.tags,
        coverage: { pagesRead: w.coverage.pages_read, pageCount: w.coverage.page_count },
        writtenBy: { model: w.model, promptVersion: w.prompt_version },
      };
    default: {
      const unknown: never = w;
      throw new TypeError(`unknown insights status: ${JSON.stringify(unknown)}`);
    }
  }
}

/** Read on 200 and on 503: a 503 body names the dependency that is down. */
export function parseHealth(w: W.HealthOut): Health {
  return {
    aiMode: w.ai_mode ?? "unknown",
    unreachable: ["db", "redis"].filter((k) => w[k] !== undefined && w[k] !== "ok"),
  };
}

/** Any non-2xx. `body` is the parsed JSON, or null when the body was not JSON. */
export function parseError(status: number, body: unknown, retryAfter: string | null): ApiError {
  const envelope = isRecord(body) && isRecord(body.error) ? body.error : null;
  if (envelope === null || typeof envelope.code !== "string" || typeof envelope.message !== "string") {
    return { kind: "broken", status };
  }
  const seconds = retryAfter === null ? NaN : Number(retryAfter);
  return {
    kind: "refused",
    status,
    code: envelope.code,
    message: envelope.message,
    retryable: typeof envelope.retryable === "boolean" ? envelope.retryable : null,
    retryAfterS: Number.isFinite(seconds) ? seconds : null,
  };
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// ------------------------------------------------------------------ the URL

function positiveInt(raw: string | null): number | null {
  return raw !== null && /^[1-9]\d{0,5}$/.test(raw) ? Number(raw) : null;
}

function citeKey(raw: string | null): CiteKey | null {
  const m = raw === null ? null : /^fact\.(\d{1,4})$/.exec(raw);
  return m ? { kind: "fact", index: Number(m[1]) } : null;
}

/** `?doc=<uuid>&cite=fact.<i>&p=<n>`. Anything malformed is dropped, never an error page. */
export function parseSelection(params: URLSearchParams): Selection {
  const doc = asDocumentId(params.get("doc") ?? "");
  if (doc === null) return { doc: null, pane: null };
  const cite = citeKey(params.get("cite"));
  const page = positiveInt(params.get("p"));
  const pane: PaneRequest | null = cite ? { kind: "cite", cite, page } : page ? { kind: "page", page } : null;
  return { doc, pane };
}

/** The inverse of parseSelection, so every link and history entry is built one way. */
export function selectionQuery(s: Selection): string {
  const params = new URLSearchParams();
  if (s.doc) {
    params.set("doc", s.doc);
    if (s.pane?.kind === "cite") params.set("cite", `fact.${s.pane.cite.index}`);
    const page = s.pane?.page ?? null;
    if (page !== null) params.set("p", String(page));
  }
  const q = params.toString();
  return q ? `?${q}` : "";
}
