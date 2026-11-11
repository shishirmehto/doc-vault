// The one place wire shapes become domain types, and URL parameters become a Selection.
// Pure functions. client.ts calls one parser per response; a throw here becomes ApiError "unexpected".

import type * as W from "./wire.ts";
import type {
  AnswerPart, ApiError, AskProgress, AskStage, Chat, ChatId, Citation, CodeCount, CompareCell, ComparedDoc, Comparison,
  ComparisonId, Doc, DocEvent, DocumentId, DocumentMetrics, FactCite, Failure, Health, Insights, Lifecycle, Outcome, Page,
  PaneRequest, ProcessingMetrics, RefusedDocument, Selection, Spread, Step, Turn, TurnCite, TurnId, TurnOutcome, Usage,
} from "./domain.ts";
import type { SseMessage } from "./sse.ts";

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

export function asComparisonId(raw: string): ComparisonId | null {
  return UUID.test(raw) ? (raw as ComparisonId) : null;
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
        stage: required(w.failed_stage, "a failed stage"),
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
    documentType: w.document_type,
    layout: w.pages === null
      ? null
      : { pageCount: w.pages, unit: w.page_basis === "synthetic" ? "section" : "page", emptyPages: w.empty_pages },
    processingMs: w.processing_ms,
    createdAt: new Date(w.created_at),
  };
}

/** An event of GET /events/documents. Throws on an event or data the contract does not name. */
export function parseDocEvent(m: SseMessage): DocEvent {
  const data: unknown = JSON.parse(m.data);
  switch (m.event) {
    case "document":
      if (!isRecord(data)) throw new TypeError("a document event without a document");
      return { kind: "document", doc: parseDoc(data as W.DocumentOut) };
    case "deleted":
      if (!isRecord(data) || typeof data.id !== "string") throw new TypeError("a deleted event without an id");
      return { kind: "deleted", id: documentId(data.id) };
    case "resync":
      return { kind: "resync" };
    default:
      throw new TypeError(`unknown document event: ${m.event}`);
  }
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

export function parseChat(w: W.ChatOut): Chat {
  const [first, ...rest] = w.documents.map((d) => ({ id: documentId(d.id), filename: d.filename, deleted: d.deleted }));
  if (first === undefined) throw new TypeError("a chat with no documents");
  return { id: required(asChatId(w.id), "a chat id"), title: w.title, documents: [first, ...rest], createdAt: new Date(w.created_at) };
}

/** Newest first. */
export function parseChatPage(w: W.ChatPage): Chat[] {
  return w.items.map(parseChat);
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
    usage: usage(w.usage),
    latencyMs: w.latency_ms,
    suggestions: w.suggestions,
  };
}

const STAGES: readonly AskStage[] = ["condensing", "searching", "answering"];

/** An event of POST /chats/{id}/messages/stream: progress, or its end (the final turn, or the error envelope). */
export type AskEvent = AskProgress | { kind: "turn"; turn: Turn } | { kind: "error"; error: ApiError };

/**
 * The stream answered 200, so an `error` event is a refusal whose status is that 200; its code and
 * `retryable` say what failed. Throws on an event or data the contract does not name.
 */
export function parseAskEvent(m: SseMessage): AskEvent {
  const data: unknown = JSON.parse(m.data);
  switch (m.event) {
    case "stage": {
      const stage = isRecord(data) ? STAGES.find((s) => s === data.stage) : undefined;
      return { kind: "stage", stage: required(stage ?? null, "a stage") };
    }
    case "delta":
      if (!isRecord(data) || typeof data.text !== "string") throw new TypeError("a delta without text");
      return { kind: "text", text: data.text };
    case "reset":
      return { kind: "reset" };
    case "turn":
      return { kind: "turn", turn: parseTurn(data as W.TurnOut) };
    case "error":
      return { kind: "error", error: parseError(200, data, null) };
    default:
      throw new TypeError(`unknown stream event: ${m.event}`);
  }
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

function usage(w: W.TurnOut["usage"]): Usage {
  return { calls: w.calls, inputTokens: w.input_tokens, outputTokens: w.output_tokens, costUsd: w.cost_usd };
}

// ------------------------------------------------------------------ comparison

/**
 * Throws when a row does not line up with the columns: a different cell count, a cell for another document, or a
 * cell that is neither empty (every field null) nor whole (a value with its page, page label and quote).
 */
export function parseComparison(w: W.ComparisonOut): Comparison {
  const documents: ComparedDoc[] = w.documents.map((d) => ({
    id: documentId(d.id), filename: d.filename, deleted: d.deleted, pagesRead: d.pages_read, pageCount: d.page_count,
  }));
  return {
    id: required(asComparisonId(w.id), "a comparison id"),
    documents,
    focus: w.focus,
    rows: w.rows.map((row) => {
      if (row.cells.length !== documents.length) {
        throw new TypeError(`the row ${row.field} has ${row.cells.length} cells for ${documents.length} documents`);
      }
      return { field: row.field, differs: row.differs, cells: row.cells.map((cell, i) => compareCell(cell, documents[i]!)) };
    }),
    droppedCells: w.dropped_cells,
    model: w.model,
    promptVersion: w.prompt_version,
    usage: usage(w.usage),
    createdAt: new Date(w.created_at),
  };
}

function compareCell(w: W.ComparisonOut["rows"][number]["cells"][number], doc: ComparedDoc): CompareCell | null {
  if (w.document_id !== doc.id) throw new TypeError(`a cell for ${w.document_id} in the column of ${doc.id}`);
  if (w.value === null && w.page === null && w.page_label === null && w.quote === null) return null;
  return {
    value: required(w.value, "a cell value"),
    pageLabel: required(w.page_label, "a cell's page label"),
    source: { documentId: doc.id, page: required(w.page, "a cell's page"), quote: required(w.quote, "a cell's quote") },
  };
}

/** Newest first. */
export function parseComparisonPage(w: W.ComparisonPage): Comparison[] {
  return w.items.map(parseComparison);
}

function sameWords(a: string, b: string): boolean {
  const words = (s: string) => s.trim().replace(/\s+/g, " ").toLowerCase();
  return words(a) === words(b);
}

/** Read on 200 and on 503: a 503 body names the dependency that is down. */
export function parseHealth(w: W.HealthOut): Health {
  return {
    aiMode: w.ai_mode,
    unreachable: (["db", "redis"] as const).filter((k) => w[k] !== "ok"),
    maxUploadBytes: w.max_upload_bytes,
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
    documents: ["not_ready", "missing", "deleted"].flatMap((key) => refusedDocuments(details[key])),
  };
}

/** details.not_ready and details.deleted list {id, filename, status}; details.missing lists bare ids. */
function refusedDocuments(list: unknown): RefusedDocument[] {
  if (!Array.isArray(list)) return [];
  return list.flatMap((item: unknown) => {
    const entry: Record<string, unknown> = typeof item === "string" ? { id: item } : isRecord(item) ? item : {};
    const id = typeof entry.id === "string" ? asDocumentId(entry.id) : null;
    return id === null ? [] : [{ id, filename: typeof entry.filename === "string" ? entry.filename : null }];
  });
}

// ------------------------------------------------------------------ metrics

function spread(w: W.PercentilesOut): Spread {
  if (w.p50 === null && w.p95 === null) return null;
  return { p50Ms: required(w.p50, "a p50"), p95Ms: required(w.p95, "a p95") };
}

/** Most first, then by code, so the table order does not depend on the wire's key order. */
function codeCounts(w: { [code: string]: number }): CodeCount[] {
  return Object.entries(w)
    .map(([code, count]) => ({ code, count }))
    .sort((a, b) => b.count - a.count || a.code.localeCompare(b.code));
}

export function parseDocumentMetrics(w: W.DocumentMetricsOut): DocumentMetrics {
  const status = (s: Lifecycle["status"]) => w.by_status[s] ?? 0;
  return {
    total: w.total,
    byStatus: { queued: status("queued"), parsing: status("parsing"), embedding: status("embedding"), ready: status("ready"), failed: status("failed") },
    failures: codeCounts(w.failures_by_code),
    pages: w.pages,
    chunks: w.chunks,
    bytes: w.bytes,
  };
}

const STEPS: readonly Step[] = ["parse", "embed", "analyze"];
const OUTCOMES: readonly TurnOutcome[] = ["answered", "no_evidence", "model_declined", "unverified"];

export function parseProcessingMetrics(w: W.ProcessingMetricsOut): ProcessingMetrics {
  const usage = (u: W.ProcessingMetricsOut["ai"] | W.ProcessingMetricsOut["ai"]["by_purpose_and_model"][number]) => ({
    calls: u.calls,
    errors: u.errors,
    inputTokens: u.input_tokens,
    outputTokens: u.output_tokens,
    costUsd: u.cost_usd,
    unpricedCalls: u.unpriced_calls,
  });
  return {
    window: w.window,
    since: new Date(w.since),
    uploaded: w.uploaded,
    indexed: w.indexed,
    timeToReady: spread(w.time_to_ready_ms),
    queueWait: spread(w.queue_wait_ms),
    steps: STEPS.map((step) => {
      const s = required(w.steps[step] ?? null, `the ${step} step`);
      return { step, runs: s.runs, failed: s.failed, retries: s.retries, duration: spread(s.duration_ms), failures: codeCounts(s.failures_by_code) };
    }),
    ai: {
      ...usage(w.ai),
      rows: w.ai.by_purpose_and_model.map((r) => ({ ...usage(r), purpose: r.purpose, model: r.model, latency: spread(r.latency_ms) })),
    },
    chat: {
      turns: w.chat.turns,
      outcomes: Object.fromEntries(OUTCOMES.map((o) => [o, w.chat.outcomes[o] ?? 0])) as Record<TurnOutcome, number>,
      latency: spread(w.chat.latency_ms),
    },
  };
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
 * `?chat=<uuid>&cite=turn.<turn uuid>.<n>&p=<n>`, `?doc=<uuid>&cite=fact.<i>&p=<n>`, or `?view=metrics&window=7d`.
 * Anything malformed is dropped, never an error page.
 */
export function parseSelection(params: URLSearchParams): Selection {
  if (params.get("view") === "metrics") return { kind: "metrics", window: params.get("window") === "7d" ? "7d" : "24h" };
  const page = positiveInt(params.get("p"));
  const chat = asChatId(params.get("chat") ?? "");
  if (chat === null && params.has("chat")) return { kind: "missing", what: "chat" };
  if (chat !== null) {
    const cite = turnCite(params.get("cite"));
    return { kind: "chat", chat, pane: cite ? { cite, page } : null };
  }
  const doc = asDocumentId(params.get("doc") ?? "");
  if (doc === null) return params.has("doc") ? { kind: "missing", what: "doc" } : { kind: "none" };
  const cite = factCite(params.get("cite"));
  const pane: PaneRequest | null = cite ? { kind: "cite", cite, page } : page ? { kind: "page", page } : null;
  return { kind: "doc", doc, pane };
}

/** The inverse of parseSelection, so every link and history entry is built one way. */
export function selectionQuery(s: Selection): string {
  const params = new URLSearchParams();
  switch (s.kind) {
    case "none":
    case "missing":
      break;
    case "metrics":
      params.set("view", "metrics");
      if (s.window !== "24h") params.set("window", s.window);
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
