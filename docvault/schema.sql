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
--   comparisons                          -> compare.py
--   document_summaries                   -> summaries.py
--   llm_calls                            -> ai.py
--   (metrics.py reads all, writes none)
--   *_notify triggers (live events)      -> NOTIFY documents, heard by events.py

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
  -- Versions: uploads of one file share family_id, the first version's id, and number 1, 2, 3 in upload order.
  -- UNIQUE (family_id, version) is the index documents_family_version in the upgrades section.
  family_id     uuid        NOT NULL,
  version       int         NOT NULL DEFAULT 1 CHECK (version >= 1),
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
  suggestions      jsonb NOT NULL DEFAULT '[]',     -- follow-up questions offered under the answer, at most 3
  started_at       timestamptz NOT NULL,
  finished_at      timestamptz NOT NULL,
  CONSTRAINT chat_turns_outcome_values CHECK (outcome IN ('answered', 'no_evidence', 'model_declined', 'unverified')),
  CONSTRAINT answered_iff_cited CHECK ((outcome = 'answered') = (jsonb_array_length(citations) > 0))
);
CREATE INDEX IF NOT EXISTS chat_turns_history ON chat_turns (chat_id, started_at, id);
CREATE INDEX IF NOT EXISTS chat_turns_window  ON chat_turns (started_at);

-- One row per comparison, written once after the model call; a provider failure writes none. documents is a
-- snapshot in column order (id, filename, page_basis, pages_read, page_count), so labels and coverage survive a
-- delete. rows are positional: cells[i] belongs to documents[i], and an empty cell is null. No FK to documents.
CREATE TABLE IF NOT EXISTS comparisons (
  id             uuid PRIMARY KEY,                    -- generated before the model call, used as ledger attribution
  focus          text,
  documents      jsonb NOT NULL,
  rows           jsonb NOT NULL,
  dropped_cells  int   NOT NULL,                      -- filled cells that grounding emptied
  model          text  NOT NULL,
  prompt_version text  NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS comparisons_listing ON comparisons (created_at DESC, id DESC);

-- One row per distinct summary request, written once after the model call; a provider failure writes none. A
-- document's content never changes (it is keyed by its hash), so a stored summary stays true and is served again
-- for the same options, model and prompt version; it goes when its document goes. focus_key is the normalised
-- focus, '' for none, so the unique key treats every request without a focus as one value.
CREATE TABLE IF NOT EXISTS document_summaries (
  id              uuid PRIMARY KEY,                   -- generated before the model call, used as ledger attribution
  document_id     uuid  NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
  length          text  NOT NULL,
  focus_key       text  NOT NULL,
  tone            text  NOT NULL,
  text            text  NOT NULL,
  sources         jsonb NOT NULL,                     -- [{page, quote}], each quote the source span on its page
  dropped_sources int   NOT NULL,                     -- sources the model cited whose quote was not on the cited page
  model           text  NOT NULL,
  prompt_version  text  NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT document_summaries_length_values CHECK (length IN ('brief', 'standard', 'detailed')),
  CONSTRAINT document_summaries_tone_values CHECK (tone IN ('plain', 'formal', 'client')),
  CONSTRAINT document_summaries_key UNIQUE (document_id, length, focus_key, tone, model, prompt_version)
);
CREATE INDEX IF NOT EXISTS document_summaries_listing ON document_summaries (document_id, created_at DESC, id DESC);

-- Every provider call, successful or not, one row. The only place tokens and
-- cost exist. No FKs: cost history outlives document, chat, comparison and summary deletes.
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
  comparison_id  uuid,
  summary_id     uuid,
  input_tokens   int  NOT NULL DEFAULT 0,
  output_tokens  int  NOT NULL DEFAULT 0,
  cost_usd       numeric(12, 6),                      -- NULL when the model has no price entry; never guessed
  latency_ms     int  NOT NULL,
  ok             boolean NOT NULL,
  error_code     text,
  request_id     text,                                -- X-Request-ID of the API request that caused the call
  CONSTRAINT llm_calls_purpose_values CHECK (
    purpose IN ('embed_chunks', 'embed_query', 'insights', 'condense', 'answer', 'compare', 'summary')),
  CONSTRAINT one_owner CHECK (num_nonnulls(document_id, turn_id, comparison_id, summary_id) = 1),
  CONSTRAINT error_iff_not_ok CHECK (ok = (error_code IS NULL))
);
CREATE INDEX IF NOT EXISTS llm_calls_window ON llm_calls (at);
CREATE INDEX IF NOT EXISTS llm_calls_turn   ON llm_calls (turn_id) WHERE turn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS llm_calls_doc    ON llm_calls (document_id) WHERE document_id IS NOT NULL;
-- llm_calls_comparison, llm_calls_summary and llm_calls_request are in the upgrades section, after the columns they index exist in
-- every database.

-- ------------------------------------------------------------------ live events
-- GET /events/documents (events.py) listens on channel 'documents'. Each payload is the id of a document whose
-- GET /documents/{id} body may have changed. Postgres delivers a NOTIFY when its transaction commits and never
-- after a rollback, and delivers identical payloads from one transaction once, so a checkpoint that writes many
-- rows of a document sends its id once.

-- latest_version is read across a family, so a version inserted or deleted changes every other member's body too.
-- An UPDATE never moves a document between families, so it names only its own row.
CREATE OR REPLACE FUNCTION notify_document() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    PERFORM pg_notify('documents', NEW.id::text);
  ELSIF TG_OP = 'INSERT' THEN
    PERFORM pg_notify('documents', NEW.id::text);
    PERFORM pg_notify('documents', f.id::text) FROM documents f WHERE f.family_id = NEW.family_id AND f.id <> NEW.id;
  ELSE
    PERFORM pg_notify('documents', OLD.id::text);
    PERFORM pg_notify('documents', f.id::text) FROM documents f WHERE f.family_id = OLD.family_id;
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION notify_document_ref() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    PERFORM pg_notify('documents', OLD.document_id::text);
  ELSE
    PERFORM pg_notify('documents', NEW.document_id::text);
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION notify_changed_chunks() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('documents', document_id::text) FROM (SELECT DISTINCT document_id FROM changed) AS docs;
  RETURN NULL;
END $$;

CREATE OR REPLACE TRIGGER documents_notify AFTER INSERT OR UPDATE OR DELETE ON documents
  FOR EACH ROW EXECUTE FUNCTION notify_document();

CREATE OR REPLACE TRIGGER document_insights_notify AFTER INSERT OR UPDATE OR DELETE ON document_insights
  FOR EACH ROW EXECUTE FUNCTION notify_document_ref();

-- Embedding progress. A trigger with a transition table takes one event and no column list, so UPDATE only.
-- Chunk inserts and cascade deletes always share a transaction with a documents write, which notifies.
CREATE OR REPLACE TRIGGER chunks_notify AFTER UPDATE ON chunks REFERENCING NEW TABLE AS changed
  FOR EACH STATEMENT EXECUTE FUNCTION notify_changed_chunks();

-- processing_ms reads the closed embed row, and run_index commits 'ready' before that row closes. Without this
-- the last event for a document could carry processing_ms null for good.
CREATE OR REPLACE TRIGGER step_runs_notify AFTER UPDATE ON step_runs
  FOR EACH ROW WHEN (NEW.step = 'embed' AND NEW.outcome = 'ok') EXECUTE FUNCTION notify_document_ref();

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

-- Versions. Every document from before them is its own family, version 1.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS family_id uuid;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS version int NOT NULL DEFAULT 1 CHECK (version >= 1);
UPDATE documents SET family_id = id WHERE family_id IS NULL;
ALTER TABLE documents ALTER COLUMN family_id SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS documents_family_version ON documents (family_id, version);
CREATE INDEX IF NOT EXISTS documents_filename ON documents (lower(filename));   -- an upload's family by its name

ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS suggestions jsonb NOT NULL DEFAULT '[]';

-- Comparisons and summaries are billed to their own owners. Each guard tests for the newest owner and purpose,
-- so a database at any earlier version upgrades in one step, and the constraints are replaced only while they lack
-- them, so a later hash change does not re-validate every ledger row.
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS comparison_id uuid;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS summary_id uuid;
CREATE INDEX IF NOT EXISTS llm_calls_comparison ON llm_calls (comparison_id) WHERE comparison_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS llm_calls_summary ON llm_calls (summary_id) WHERE summary_id IS NOT NULL;
DO $$
BEGIN
  IF (SELECT pg_get_constraintdef(oid) FROM pg_constraint
      WHERE conrelid = 'llm_calls'::regclass AND conname = 'one_owner') NOT LIKE '%summary_id%' THEN
    ALTER TABLE llm_calls DROP CONSTRAINT one_owner,
      ADD CONSTRAINT one_owner CHECK (num_nonnulls(document_id, turn_id, comparison_id, summary_id) = 1);
  END IF;
  IF (SELECT pg_get_constraintdef(oid) FROM pg_constraint
      WHERE conrelid = 'llm_calls'::regclass AND conname = 'llm_calls_purpose_values') NOT LIKE '%''summary''%' THEN
    ALTER TABLE llm_calls DROP CONSTRAINT llm_calls_purpose_values, ADD CONSTRAINT llm_calls_purpose_values CHECK (
      purpose IN ('embed_chunks', 'embed_query', 'insights', 'condense', 'answer', 'compare', 'summary'));
  END IF;
END $$;

-- The request id from the API's log line, so a request's model calls can be found from it.
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS request_id text;
CREATE INDEX IF NOT EXISTS llm_calls_request ON llm_calls (request_id) WHERE request_id IS NOT NULL;
