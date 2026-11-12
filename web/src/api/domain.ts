// The types components read. parse.ts builds them from the wire shapes; the wire flattens
// state into nullable fields, and these types put each field back in the one state that has it.

declare const brand: unique symbol;
type Brand<T, B extends string> = T & { readonly [brand]: B };

export type DocumentId = Brand<string, "DocumentId">;
export type FamilyId = Brand<string, "FamilyId">;
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

export type DocVersion = { family: FamilyId; number: number; latest: number };

export type Doc = {
  id: DocumentId;
  filename: string;
  kind: DocKind;
  sizeBytes: number;
  lifecycle: Lifecycle;
  insights: InsightsStatus;
  documentType: string | null;
  /** Null until parsing has run. */
  layout: Layout | null;
  processingMs: number | null;
  createdAt: Date;
  version: DocVersion;
};

export type UploadReceipt = { doc: Doc; duplicate: boolean };

export type DocEvent = { kind: "document"; doc: Doc } | { kind: "deleted"; id: DocumentId } | { kind: "resync" };

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

/** Today's AI spend against the daily cap, in US dollars. The day is UTC's. */
export type AiBudget = { limitUsd: number; spentTodayUsd: number };

/** off: no CACHE_URL is set. unreachable: every request goes to the model, and still works. */
export type CacheState = "ok" | "unreachable" | "off";

export type Health = {
  aiMode: "live" | "fake" | "mixed";
  unreachable: readonly ("db" | "redis")[];
  /** The API answers 413 for anything larger, so the browser refuses it before sending. */
  maxUploadBytes: number;
  /** Null when no daily cap is set, and while the database is unreachable. */
  aiBudget: AiBudget | null;
  cache: CacheState;
};

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
  suggestions: readonly string[];
  /** Stored for the same first question on the same documents and served with no model call; usage reads 0 calls. */
  cached: boolean;
};

export type AskStage = "condensing" | "searching" | "answering";

export type AskProgress = { kind: "stage"; stage: AskStage } | { kind: "text"; text: string } | { kind: "reset" };

export type ComparisonId = Brand<string, "ComparisonId">;

export type ComparedDoc = {
  id: DocumentId;
  filename: string;
  version: number | null;
  deleted: boolean;
  pagesRead: number;
  pageCount: number;
};

export type CompareCell = { source: SourceRef; value: string; pageLabel: string };

/** `cells[i]` belongs to the comparison's `documents[i]`; null where that document does not state the field. */
export type CompareRow = { field: string; cells: readonly (CompareCell | null)[]; differs: boolean };

export type Comparison = {
  id: ComparisonId;
  documents: readonly ComparedDoc[];
  focus: string | null;
  rows: readonly CompareRow[];
  droppedCells: number;
  model: string;
  promptVersion: string;
  usage: Usage;
  createdAt: Date;
};

export type SummaryId = Brand<string, "SummaryId">;

export type SummaryLength = "brief" | "standard" | "detailed";
export type SummaryTone = "plain" | "formal" | "client";

/** `focus` as the backend compares it: trimmed, lowercase, single spaces; null for none. */
export type SummaryOptions = { length: SummaryLength; focus: string | null; tone: SummaryTone };

export type SummarySource = { page: number; pageLabel: string; quote: string };

export type Summary = {
  id: SummaryId;
  documentId: DocumentId;
  options: SummaryOptions;
  text: string;
  sources: readonly SummarySource[];
  droppedSources: number;
  writtenBy: { model: string; promptVersion: string };
  cached: boolean;
  createdAt: Date;
};

export type SummaryList = { summaries: readonly Summary[]; complete: boolean };

// ------------------------------------------------------------------ the URL

/** A key fact's passage, by its index in the document's insights. */
export type FactCite = { kind: "fact"; index: number };
export type SummaryCite = { kind: "summary"; summary: SummaryId; index: number };
/** A chat citation, by the turn that made it and its [n]. */
export type TurnCite = { turn: TurnId; ref: number };

export type PaneRequest =
  | { kind: "cite"; cite: FactCite | SummaryCite; page: number | null }
  | { kind: "page"; page: number };

/** The chat's pane: always a citation, so it always knows its document. `page` is set after paging. */
export type ChatPane = { cite: TurnCite; page: number | null };

export type CellCite = { row: number; col: number };

export type ComparisonPane = { cite: CellCite; page: number | null };

export type MetricsWindow = "24h" | "7d";

/** Everything the URL selects. Nothing selected is the screen that starts a chat. */
export type Selection =
  | { kind: "none" }
  | { kind: "metrics"; window: MetricsWindow }
  | { kind: "doc"; doc: DocumentId; pane: PaneRequest | null }
  | { kind: "chat"; chat: ChatId; pane: ChatPane | null }
  | { kind: "compare"; with: DocumentId | null }
  | { kind: "comparison"; comparison: ComparisonId; pane: ComparisonPane | null; only: boolean }
  | { kind: "missing"; what: "doc" | "chat" | "comparison" };

/** Today's AI spending cap is used up: the cap, the spend, and the next 00:00 UTC, when asking works again. */
export type Quota = { limitUsd: number; spentTodayUsd: number; resetsAt: Date };

/**
 * `requestId` is the X-Request-ID the API echoed, else the one the request sent, so a reader can quote it and an
 * operator can find its log lines. Null when neither is known.
 */
export type ApiError =
  /** The backend's error envelope: 404, 409, 413, 415, 422, 429 and 503 with Retry-After, and 500 internal_error. */
  | {
      kind: "refused";
      status: number;
      code: string;
      message: string;
      retryable: boolean | null;
      retryAfterS: number | null;
      /** The documents a refusal names: not ready (409), missing (404) or deleted from a chat (409). */
      documents: readonly RefusedDocument[];
      /** Set only for quota_exceeded whose details parse. */
      quota: Quota | null;
      requestId: string | null;
    }
  /** A non-2xx without the envelope: the proxy failing to reach the API, or an error page in front of it. */
  | { kind: "broken"; status: number; requestId: string | null }
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
export type Purpose = "embed_chunks" | "embed_query" | "insights" | "condense" | "answer" | "compare" | "summary";
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

export type Counts = { hits: number; misses: number };

/** Counted since `countedSince` (null before the first lookup), across windows and API restarts. */
export type CacheFigures = { embedding: Counts; answer: Counts; countedSince: Date | null };

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
  /** Embeddings are counted per text, answers per chat's first question. Null when the cache is off or unreachable. */
  cache: CacheFigures | null;
};
