// The one place wire shapes become domain types, and URL parameters become a Selection.
// Pure functions. client.ts calls one parser per response; a throw here becomes ApiError "unexpected".

import type * as W from "./wire.ts";
import type {
  AnswerPart, ApiError, Chat, ChatId, Citation, Doc, DocumentId, FactCite, Failure, Health, Insights, Lifecycle, Outcome,
  Page, PaneRequest, Selection, Turn, TurnCite, TurnId,
} from "./domain.ts";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The only constructor of a DocumentId. */
export function asDocumentId(raw: string): DocumentId | null {
  return UUID.test(raw) ? (raw as DocumentId) : null;
}

export function asChatId(raw: string): ChatId | null {
  return UUID.test(raw) ? (raw as ChatId) : null;
}

function asTurnId(raw: string): TurnId | null {
  return UUID.test(raw) ? (raw as TurnId) : null;
}

function documentId(raw: string): DocumentId {
  const id = asDocumentId(raw);
  if (id === null) throw new TypeError(`not a document id: ${raw}`);
  return id;
}

function required<T>(value: T | null, what: string): T {
  if (value === null) throw new TypeError(`not ${what}`);
  return value;
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

// ------------------------------------------------------------------ chat

/** ChatOut.documents is typed as string maps, so each entry's id and filename are checked here. */
export function parseChat(w: W.ChatOut): Chat {
  const [first, ...rest] = w.documents.map((d) => ({
    id: documentId(d.id ?? ""),
    filename: required(d.filename ?? null, "a filename"),
  }));
  if (first === undefined) throw new TypeError("a chat with no documents");
  return { id: required(asChatId(w.id), "a chat id"), documents: [first, ...rest] };
}

export function parseTurn(w: W.TurnOut): Turn {
  const citations: Citation[] = w.citations.map((c) => ({
    ref: c.ref,
    filename: c.filename,
    pageLabel: c.page_label,
    source: { documentId: documentId(c.document_id), page: c.page, quote: c.quote },
  }));
  return {
    id: required(asTurnId(w.id), "a turn id"),
    question: w.question,
    readAs: sameWords(w.interpreted_as, w.question) ? null : w.interpreted_as,
    outcome: outcome(w, citations),
    usage: {
      calls: w.usage.calls,
      inputTokens: w.usage.input_tokens,
      outputTokens: w.usage.output_tokens,
      costUsd: w.usage.cost_usd,
    },
    latencyMs: w.latency_ms,
  };
}

export function parseHistory(w: W.HistoryOut): Turn[] {
  return w.turns.map(parseTurn);
}

function outcome(w: W.TurnOut, citations: readonly Citation[]): Outcome {
  switch (w.status) {
    case "answered":
      return { kind: "answered", parts: answerParts(w.answer, citations), citations };
    case "not_found":
      return { kind: "not_found", reason: required(w.reason, "a not-found reason"), message: w.answer };
    default: {
      const unknown: never = w.status;
      throw new TypeError(`unknown turn status: ${String(unknown)}`);
    }
  }
}

/**
 * Cuts the answer at its [n] markers. The spaces before a marker go, so the chip sits on the word it
 * follows. A marker with no citation of that number stays text, so a turn with no citations is all text.
 */
function answerParts(answer: string, citations: readonly Citation[]): AnswerPart[] {
  const byRef = new Map(citations.map((c) => [c.ref, c]));
  const parts: AnswerPart[] = [];
  let at = 0;
  for (const marker of answer.matchAll(/\[(\d+)\]/g)) {
    const citation = byRef.get(Number(marker[1]));
    if (citation === undefined) continue;
    const text = answer.slice(at, marker.index).trimEnd();
    if (text !== "") parts.push({ kind: "text", text });
    parts.push({ kind: "cite", citation });
    at = marker.index + marker[0].length;
  }
  if (at < answer.length) parts.push({ kind: "text", text: answer.slice(at) });
  return parts;
}

function sameWords(a: string, b: string): boolean {
  const words = (s: string) => s.trim().replace(/\s+/g, " ").toLowerCase();
  return words(a) === words(b);
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
  const details = isRecord(envelope.details) ? envelope.details : {};
  return {
    kind: "refused",
    status,
    code: envelope.code,
    message: envelope.message,
    retryable: typeof envelope.retryable === "boolean" ? envelope.retryable : null,
    retryAfterS: Number.isFinite(seconds) ? seconds : null,
    documents: ["not_ready", "missing", "deleted"].flatMap((key) => documentIds(details[key])),
  };
}

/** details.not_ready and details.deleted list {id, ...} objects; details.missing lists bare ids. */
function documentIds(list: unknown): DocumentId[] {
  if (!Array.isArray(list)) return [];
  return list.flatMap((item: unknown) => {
    const raw = typeof item === "string" ? item : isRecord(item) && typeof item.id === "string" ? item.id : "";
    const id = asDocumentId(raw);
    return id === null ? [] : [id];
  });
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// ------------------------------------------------------------------ the URL

function positiveInt(raw: string | null): number | null {
  return raw !== null && /^[1-9]\d{0,5}$/.test(raw) ? Number(raw) : null;
}

function factCite(raw: string | null): FactCite | null {
  const m = raw === null ? null : /^fact\.(\d{1,4})$/.exec(raw);
  return m ? { kind: "fact", index: Number(m[1]) } : null;
}

function turnCite(raw: string | null): TurnCite | null {
  const m = raw === null ? null : /^turn\.([0-9a-f-]{36})\.([1-9]\d{0,3})$/i.exec(raw);
  const turn = m ? asTurnId(m[1]!) : null;
  return m && turn ? { turn, ref: Number(m[2]) } : null;
}

/**
 * `?chat=<uuid>&cite=turn.<turn uuid>.<n>&p=<n>`, or `?doc=<uuid>&cite=fact.<i>&p=<n>`.
 * Anything malformed is dropped, never an error page.
 */
export function parseSelection(params: URLSearchParams): Selection {
  const page = positiveInt(params.get("p"));
  const chat = asChatId(params.get("chat") ?? "");
  if (chat !== null) {
    const cite = turnCite(params.get("cite"));
    return { kind: "chat", chat, pane: cite ? { cite, page } : null };
  }
  const doc = asDocumentId(params.get("doc") ?? "");
  if (doc === null) return { kind: "none" };
  const cite = factCite(params.get("cite"));
  const pane: PaneRequest | null = cite ? { kind: "cite", cite, page } : page ? { kind: "page", page } : null;
  return { kind: "doc", doc, pane };
}

/** The inverse of parseSelection, so every link and history entry is built one way. */
export function selectionQuery(s: Selection): string {
  const params = new URLSearchParams();
  switch (s.kind) {
    case "none":
      break;
    case "doc":
      params.set("doc", s.doc);
      if (s.pane?.kind === "cite") params.set("cite", `fact.${s.pane.cite.index}`);
      if (s.pane?.page) params.set("p", String(s.pane.page));
      break;
    case "chat":
      params.set("chat", s.chat);
      if (s.pane) params.set("cite", `turn.${s.pane.cite.turn}.${s.pane.cite.ref}`);
      if (s.pane?.page) params.set("p", String(s.pane.page));
      break;
    default: {
      const unknown: never = s;
      return unknown;
    }
  }
  const q = params.toString();
  return q ? `?${q}` : "";
}
