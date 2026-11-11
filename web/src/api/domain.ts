// The types components read. parse.ts builds them from the wire shapes; the wire flattens
// state into nullable fields, and these types put each field back in the one state that has it.

declare const brand: unique symbol;
type Brand<T, B extends string> = T & { readonly [brand]: B };

export type DocumentId = Brand<string, "DocumentId">;

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
  /** Null until parsing has run. */
  layout: Layout | null;
  processingMs: number | null;
  createdAt: Date;
};

export type UploadReceipt = { doc: Doc; duplicate: boolean };

/** A passage on a page. Key facts (and, in pass 2, chat citations) become this. */
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

export type Health = { aiMode: string; unreachable: readonly string[] };

/** Which key fact's passage the source pane marks. Pass 2 adds chat citations. */
export type CiteKey = { kind: "fact"; index: number };

/** What the source pane shows: a cited passage (on its page, or another page after paging), or a bare page. */
export type PaneRequest =
  | { kind: "cite"; cite: CiteKey; page: number | null }
  | { kind: "page"; page: number };

/** Everything the URL selects. The pane only exists inside a selected document. */
export type Selection = { doc: DocumentId | null; pane: PaneRequest | null };

export type ApiError =
  /** The backend's error envelope: 404, 409, 413, 415, 422 and 503 (with Retry-After). */
  | { kind: "refused"; status: number; code: string; message: string; retryable: boolean | null; retryAfterS: number | null }
  /** A non-2xx without the envelope: an unbuilt route's 500, FastAPI's own 422, or the proxy failing to reach the API. */
  | { kind: "broken"; status: number }
  /** A 2xx whose body did not match the contract. Regenerate schema.d.ts. */
  | { kind: "unexpected"; detail: string }
  /** No HTTP response at all. */
  | { kind: "offline" };

export type Result<T> = { ok: true; value: T } | { ok: false; error: ApiError };
