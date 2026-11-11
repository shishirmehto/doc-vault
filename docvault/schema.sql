-- The only schema declaration. db.migrate() applies this file at API and worker
-- start when its hash is not yet in schema_version (the one table db.py creates
-- itself). Every CREATE is IF NOT EXISTS, so an edited file adds new tables
-- and indexes to an existing database but does not alter a table that exists.
-- A changed column or CHECK goes in the upgrades section at the end as an
-- idempotent ALTER: the whole file runs again each time its hash changes.
--
-- Ownership: each table has one writing module. step_runs and llm_calls are
-- append-only logs; every metric is an aggregate over one table, never a
-- counter kept in sync with another table.
--
--   documents, pages, chunks             -> documents.py
--   step_runs (append-only)              -> db.pipeline_steps(), used by both pipelines
--   document_insights                    -> insights.py
--   chats, chat_documents, chat_turns    -> chat.py
--   llm_calls                            -> ai.py
--   (metrics.py reads all, writes none)

CREATE EXTENSION IF NOT EXISTS vector;

-- Closed value sets are text columns with a CHECK named <table>_<column>_values,
-- not Postgres enum types, so adding a value never needs ALTER TYPE. The lists
-- mirror the StrEnums in domain.py; tests/test_schema.py asserts they match.

-- Lifecycle of the searchable index. 'ready' means every chunk carries an
-- embedding from the current embedding model; insights are NOT part of it.
CREATE TABLE IF NOT EXISTS documents (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  sha256        bytea       NOT NULL UNIQUE,          -- dedupe key; blob path derives from it
  filename      text        NOT NULL,
  kind          text        NOT NULL,
  size_bytes    bigint      NOT NULL CHECK (size_bytes > 0),
  status        text        NOT NULL DEFAULT 'queued',
  -- written once by the parse checkpoint, immutable afterwards
  page_basis    text,
  page_count    int,
  chunk_count   int,
  empty_pages   int[],                                -- pages with no extractable text
  error_code    text,
  error_message text,
  error_retryable boolean,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  requeued_at   timestamptz,                          -- last re-enqueue by reconcile, its only reader and writer
  CONSTRAINT documents_kind_values CHECK (kind IN ('pdf', 'docx', 'txt')),
  CONSTRAINT documents_status_values CHECK (status IN ('queued', 'parsing', 'embedding', 'ready', 'failed')),
  CONSTRAINT documents_page_basis_values CHECK (page_basis IN ('physical', 'synthetic')),
  CONSTRAINT failed_iff_error CHECK ((status = 'failed') = (error_code IS NOT NULL)),
  CONSTRAINT error_fields_together CHECK (
    (error_code IS NULL) = (error_message IS NULL) AND (error_code IS NULL) = (error_retryable IS NULL)),
  CONSTRAINT parsed_fields_together CHECK (
    (page_count IS NULL) = (chunk_count IS NULL) AND (page_count IS NULL) = (page_basis IS NULL)),
  CONSTRAINT parsed_before_embedding CHECK (status IN ('queued', 'parsing', 'failed') OR page_count IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS documents_listing   ON documents (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS documents_in_flight ON documents (updated_at) WHERE status IN ('queued', 'parsing', 'embedding');

-- One row per page (or per labelled section when page_basis = 'synthetic').
-- Feeds insights (page-tagged text), insight quote checks and GET /documents/{id}/pages/{n}.
CREATE TABLE IF NOT EXISTS pages (
  document_id uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
  number      int  NOT NULL CHECK (number >= 1),
  text        text NOT NULL,
  PRIMARY KEY (document_id, number)
);

-- A chunk is a contiguous slice of the document's text stream. page_map lists
-- [offset_in_chunk, page_number] pairs, first pair always [0, first page], so
-- a quote found at offset o inside the chunk resolves to an exact page.
CREATE TABLE IF NOT EXISTS chunks (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id     uuid  NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
  ord             int   NOT NULL,
  text            text  NOT NULL,
  page_map        jsonb NOT NULL,
  -- No fixed dimension. Vectors of different sizes are never compared, because every
  -- read filters on embedding_model, which includes the dimension. The ANN path, when
  -- built, adds a partial HNSW expression index per embedding space:
  -- ((embedding::vector(N)) vector_cosine_ops) WHERE embedding_model = '<space>'.
  embedding       vector,
  embedding_model text,
  tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  UNIQUE (document_id, ord),
  CONSTRAINT embedding_with_model CHECK ((embedding IS NULL) = (embedding_model IS NULL))
);
CREATE INDEX IF NOT EXISTS chunks_doc     ON chunks (document_id);
CREATE INDEX IF NOT EXISTS chunks_pending ON chunks (document_id) WHERE embedding IS NULL;
CREATE INDEX IF NOT EXISTS chunks_tsv     ON chunks USING gin (tsv);

-- Append-only processing history. One row per attempt of a step. A row with
-- finished_at NULL is a running step or a crashed one; the next holder of the
-- document's lease closes it as 'abandoned'.
CREATE TABLE IF NOT EXISTS step_runs (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id uuid        NOT NULL,                   -- no FK: history outlives deletes
  step        text        NOT NULL,
  attempt     int         NOT NULL,                   -- Celery request.retries + 1
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  outcome     text,
  error_code  text,
  CONSTRAINT step_runs_step_values CHECK (step IN ('parse', 'embed', 'analyze')),
  CONSTRAINT step_runs_outcome_values CHECK (outcome IN ('ok', 'failed', 'abandoned')),
  CONSTRAINT finished_iff_outcome CHECK ((finished_at IS NULL) = (outcome IS NULL)),
  CONSTRAINT error_only_on_failure CHECK ((outcome = 'failed') = (error_code IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS step_runs_doc    ON step_runs (document_id, step, started_at DESC);
CREATE INDEX IF NOT EXISTS step_runs_window ON step_runs (started_at);

-- Absent row = insights pending. Written only by insights.py.
CREATE TABLE IF NOT EXISTS document_insights (
  document_id         uuid PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
  status              text NOT NULL CHECK (status IN ('ready', 'failed')),
  prompt_version      text NOT NULL,
  model               text NOT NULL,
  body                jsonb,       -- InsightsBody: title, document_type, summary, key_facts (verified), tags
  pages_read          int,         -- coverage: pages 1..pages_read went into the prompt
  dropped_unverified  int,         -- key facts whose quote was not found on the cited page
  error_code          text,
  error_message       text,
  error_retryable     boolean,
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ready_has_body CHECK ((status = 'ready') = (body IS NOT NULL)),
  CONSTRAINT failed_has_error CHECK ((status = 'failed') = (error_code IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS chats (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title      text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chats_listing ON chats (created_at DESC, id DESC);

-- The document scope is fixed when the chat starts. No FK to documents: a deleted
-- document stays in the scope, so its chats refuse new questions instead of quietly
-- answering from fewer documents. filename is a snapshot for that refusal.
CREATE TABLE IF NOT EXISTS chat_documents (
  chat_id     uuid NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
  document_id uuid NOT NULL,
  filename    text NOT NULL,
  PRIMARY KEY (chat_id, document_id)
);

-- One row per question/answer pair. A turn is written once, after the answer
-- or refusal exists; provider failures write no turn. citations is a snapshot
-- (document id, filename, page, quote) so history survives document deletes.
CREATE TABLE IF NOT EXISTS chat_turns (
  id               uuid PRIMARY KEY,                  -- generated before the AI calls, used as ledger attribution
  chat_id          uuid NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
  question         text NOT NULL,
  standalone_query text NOT NULL,                     -- equals question on the first turn
  outcome          text NOT NULL,
  answer           text NOT NULL,
  citations        jsonb NOT NULL DEFAULT '[]',
  started_at       timestamptz NOT NULL,
  finished_at      timestamptz NOT NULL,
  CONSTRAINT chat_turns_outcome_values CHECK (outcome IN ('answered', 'no_evidence', 'model_declined', 'unverified')),
  CONSTRAINT answered_iff_cited CHECK ((outcome = 'answered') = (jsonb_array_length(citations) > 0))
);
CREATE INDEX IF NOT EXISTS chat_turns_history ON chat_turns (chat_id, started_at, id);
CREATE INDEX IF NOT EXISTS chat_turns_window  ON chat_turns (started_at);

-- Every provider call, successful or not, one row. The only place tokens and
-- cost exist. No FKs: cost history outlives document and chat deletes.
CREATE TABLE IF NOT EXISTS llm_calls (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at             timestamptz NOT NULL DEFAULT now(),
  purpose        text        NOT NULL,
  provider       text        NOT NULL,                -- openai | fake
  model          text        NOT NULL,
  prompt_name    text,
  prompt_version text,
  document_id    uuid,
  turn_id        uuid,
  input_tokens   int  NOT NULL DEFAULT 0,
  output_tokens  int  NOT NULL DEFAULT 0,
  cost_usd       numeric(12, 6),                      -- NULL when the model has no price entry; never guessed
  latency_ms     int  NOT NULL,
  ok             boolean NOT NULL,
  error_code     text,
  CONSTRAINT llm_calls_purpose_values CHECK (purpose IN ('embed_chunks', 'embed_query', 'insights', 'condense', 'answer')),
  CONSTRAINT one_owner CHECK (num_nonnulls(document_id, turn_id) = 1),
  CONSTRAINT error_iff_not_ok CHECK (ok = (error_code IS NULL))
);
CREATE INDEX IF NOT EXISTS llm_calls_window ON llm_calls (at);
CREATE INDEX IF NOT EXISTS llm_calls_turn   ON llm_calls (turn_id) WHERE turn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS llm_calls_doc    ON llm_calls (document_id) WHERE document_id IS NOT NULL;

-- ------------------------------------------------------------------ upgrades
-- For databases created by an earlier version of this file. The whole file runs
-- again on every hash change, so each upgrade checks the catalog and acts once.

-- embedding was vector(1536) under an HNSW index. The ALTER alone keeps that index,
-- and the index then rejects every vector that is not 1536 long.
DO $$
BEGIN
  IF (SELECT atttypmod FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding') <> -1 THEN
    DROP INDEX IF EXISTS chunks_hnsw;
    ALTER TABLE chunks ALTER COLUMN embedding TYPE vector;
  END IF;
END $$;

ALTER TABLE document_insights ADD COLUMN IF NOT EXISTS error_message text;

ALTER TABLE chat_documents DROP CONSTRAINT IF EXISTS chat_documents_document_id_fkey;
ALTER TABLE chat_documents ADD COLUMN IF NOT EXISTS filename text;
UPDATE chat_documents cd SET filename = d.filename FROM documents d WHERE cd.filename IS NULL AND d.id = cd.document_id;
ALTER TABLE chat_documents ALTER COLUMN filename SET NOT NULL;

ALTER TABLE documents ADD COLUMN IF NOT EXISTS requeued_at timestamptz;
