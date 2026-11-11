// The types components read. parse.ts builds them from the wire shapes; the wire flattens
// state into nullable fields, and these types put each field back in the one state that has it.

declare const brand: unique symbol;
type Brand<T, B extends string> = T & { readonly [brand]: B };

export type DocumentId = Brand<string, "DocumentId">;
export type ChatId = Brand<string, "ChatId">;
export type TurnId = Brand<string, "TurnId">;

export type NonEmpty<T> = readonly [T, ...T[]];

export type DocKind = "pdf" | "docx" | "txt";

/** The backend's failure. `message` is written for people and is shown verbatim. */
export type Failure = { code: string; message: string; retryable: boolean };

/** One variant per document status. `stage` says which step failed: parsing never finished, or it did. */
export type Lifecycle =
  | { status: "queued" }
  | { status: "parsing" }
  | { status: "embedding"; embedded: number; total: number }
  | { status: "ready" }
  | { status: "failed"; stage: "parse" | "embed"; failure: Failure };

export type InsightsStatus = "pending" | "ready" | "failed" | "unavailable";

/** "section" for DOCX and TXT, whose pages are synthetic ~3,000-character sections. */
export type PageUnit = "page" | "section";

export type Layout = { pageCount: number; unit: PageUnit; emptyPages: readonly number[] };

export type Doc = {
  id: DocumentId;
  filename: string;
  kind: DocKind;
  sizeBytes: number;
  lifecycle: Lifecycle;
  insights: InsightsStatus;
  /** The insights' document type ("policy", "quote", "other"); null until insights are ready. */
  documentType: string | null;
  /** Null until parsing has run. */
  layout: Layout | null;
  processingMs: number | null;
  createdAt: Date;
};

export type UploadReceipt = { doc: Doc; duplicate: boolean };

/** A passage on a page. Key facts and chat citations both become this. */
export type SourceRef = { documentId: DocumentId; page: number; quote: string };

export type Page = { documentId: DocumentId; number: number; label: string; text: string };

export type KeyFact = { label: string; value: string; source: SourceRef; pageLabel: string };

export type Insights =
  | { status: "pending" }
  | {
      status: "ready";
      title: string;
      documentType: string;
      summary: string;
      keyFacts: readonly KeyFact[];
      /** Facts the model cited whose quote was not on the cited page. The backend drops them. */
      droppedKeyFacts: number;
      tags: readonly string[];
      coverage: { pagesRead: number; pageCount: number };
      writtenBy: { model: string; promptVersion: string };
    }
  | { status: "failed"; failure: Failure }
  | { status: "unavailable"; reason: string };

/** maxUploadBytes: the API answers 413 for anything larger, so the browser refuses it before sending. */
export type Health = { aiMode: "live" | "fake" | "mixed"; unreachable: readonly ("db" | "redis")[]; maxUploadBytes: number };

// ------------------------------------------------------------------ chat

/** A chat answers from a fixed set of documents, chosen when it starts. A deleted one keeps its filename. */
export type ChatDocument = { id: DocumentId; filename: string; deleted: boolean };
export type Chat = { id: ChatId; title: string | null; documents: NonEmpty<ChatDocument>; createdAt: Date };

/** `ref` is the number the answer's [n] markers use. pageLabel is the backend's ("p. 2" or "section 2"). */
export type Citation = { ref: number; source: SourceRef; filename: string; pageLabel: string };

/** The answer text cut at its [n] markers. A marker with no citation stays text. */
export type AnswerPart = { kind: "text"; text: string } | { kind: "cite"; citation: Citation };

export type NotFoundReason = "no_evidence" | "model_declined" | "unverified";

export type Outcome =
  | { kind: "answered"; parts: readonly AnswerPart[]; citations: readonly Citation[] }
  /** A deliberate refusal, not an error. `message` is the backend's own sentence. */
  | { kind: "not_found"; reason: NotFoundReason; message: string };

/** costUsd is null when a call used a model with no price. */
export type Usage = { calls: number; inputTokens: number; outputTokens: number; costUsd: number | null };

export type Turn = {
  id: TurnId;
  question: string;
  /** The standalone question a follow-up was read as. Null when it is the question itself. */
  readAs: string | null;
  outcome: Outcome;
  usage: Usage;
  latencyMs: number;
  /** Up to three follow-up questions to offer under the turn, each ending in "?". */
  suggestions: readonly string[];
};

/** The step a streamed answer is on. "condensing" reads a follow-up against the turns before it. */
export type AskStage = "condensing" | "searching" | "answering";

/**
 * One event of the streamed ask before its end. `text` is provisional answer text with the source markers
 * removed; `reset` discards the text so far. The stream ends with a Turn or an ApiError instead.
 */
export type AskProgress = { kind: "stage"; stage: AskStage } | { kind: "text"; text: string } | { kind: "reset" };

// ------------------------------------------------------------------ the URL

/** A key fact's passage, by its index in the document's insights. */
export type FactCite = { kind: "fact"; index: number };
/** A chat citation, by the turn that made it and its [n]. */
export type TurnCite = { turn: TurnId; ref: number };

/** The document view's pane: a fact's passage (on its page, or another page after paging), or a bare page. */
export type PaneRequest =
  | { kind: "cite"; cite: FactCite; page: number | null }
  | { kind: "page"; page: number };

/** The chat's pane: always a citation, so it always knows its document. `page` is set after paging. */
export type ChatPane = { cite: TurnCite; page: number | null };

export type MetricsWindow = "24h" | "7d";

/** Everything the URL selects. Nothing selected is the screen that starts a chat. */
export type Selection =
  | { kind: "none" }
  | { kind: "metrics"; window: MetricsWindow }
  | { kind: "doc"; doc: DocumentId; pane: PaneRequest | null }
  | { kind: "chat"; chat: ChatId; pane: ChatPane | null }
  /** The URL names a document or chat by something that cannot be an id. */
  | { kind: "missing"; what: "doc" | "chat" };

export type ApiError =
  /** The backend's error envelope: 404, 409, 413, 415, 422, and 429 and 503 with Retry-After. */
  | {
      kind: "refused";
      status: number;
      code: string;
      message: string;
      retryable: boolean | null;
      retryAfterS: number | null;
      /** The documents a refusal names: not ready (409), missing (404) or deleted from a chat (409). */
      documents: readonly RefusedDocument[];
    }
  /** A non-2xx without the envelope: an unhandled 500, or the proxy failing to reach the API. */
  | { kind: "broken"; status: number }
  /** A 2xx whose body did not match the contract. Regenerate schema.d.ts. */
  | { kind: "unexpected"; detail: string }
  /** No HTTP response at all. */
  | { kind: "offline" };

/** A 404 names a missing document by id only; a 409 also gives its filename. */
export type RefusedDocument = { id: DocumentId; filename: string | null };

export type Result<T> = { ok: true; value: T } | { ok: false; error: ApiError };

// ------------------------------------------------------------------ metrics

/** Milliseconds. Null when the window holds no sample. */
export type Spread = { p50Ms: number; p95Ms: number } | null;

export type CodeCount = { code: string; count: number };

export type DocumentMetrics = {
  total: number;
  byStatus: Record<Lifecycle["status"], number>;
  /** Failed documents by error code, most first. */
  failures: readonly CodeCount[];
  pages: number;
  chunks: number;
  bytes: number;
};

export type Step = "parse" | "embed" | "analyze";
export type Purpose = "embed_chunks" | "embed_query" | "insights" | "condense" | "answer";
export type TurnOutcome = "answered" | NotFoundReason;

/** Runs, not documents: a retried document counts once per run. */
export type StepMetrics = {
  step: Step;
  runs: number;
  failed: number;
  retries: number;
  duration: Spread;
  /** Failed runs by error code, most first. */
  failures: readonly CodeCount[];
};

/** costUsd counts priced calls only; unpricedCalls used a model with no price. */
export type AiUsage = {
  calls: number;
  errors: number;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  unpricedCalls: number;
};

export type ProcessingMetrics = {
  window: string;
  since: Date;
  uploaded: number;
  indexed: number;
  timeToReady: Spread;
  queueWait: Spread;
  steps: readonly StepMetrics[];
  ai: AiUsage & { rows: readonly (AiUsage & { purpose: Purpose; model: string; latency: Spread })[] };
  chat: { turns: number; outcomes: Record<TurnOutcome, number>; latency: Spread };
};
