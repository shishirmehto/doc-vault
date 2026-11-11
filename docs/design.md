# Design

This page explains how Doc Vault stores documents, processes them, and answers questions about them. Every number and name on it comes from the code at the commit that changed it last. `docs/lifecycle.md` covers the indexing state machine and its TLA+ model, and `docs/evals.md` covers answer quality.

## Data model

Postgres 16 with pgvector holds every table. `docvault/schema.sql` is the only schema file, and `db.migrate()` applies it at process start when its hash is new. Each table has one module that writes it. `metrics.py` reads every table and writes none.

| Table | Written by | Holds |
|---|---|---|
| `documents` | `documents.py` | One row per distinct file (unique `sha256`): filename, kind, size, `status`, the parse facts (`page_basis`, `page_count`, `chunk_count`, `empty_pages`), the error of a failed document, and `requeued_at`, which only `reconcile` reads. |
| `pages` | `documents.py` | The text of each page, or of each roughly 3,000-character section of a DOCX or TXT file. Citations and insights point here. |
| `chunks` | `documents.py` | Slices of about 1,800 characters with 250 characters of overlap. Each has a `page_map` of offsets to pages, an embedding tagged with `embedding_model`, and a generated `tsvector`. |
| `step_runs` | `db.pipeline_steps` | Append-only. One row per attempt of `parse`, `embed` or `analyze`, with its outcome and error code. |
| `document_insights` | `insights.py` | One row per document once insights exist or fail. No row means pending. |
| `chats`, `chat_documents` | `chat.py` | A chat and its fixed document scope, with a filename snapshot. |
| `chat_turns` | `chat.py` | One row per question: the standalone query, the outcome, the answer and a citation snapshot. |
| `llm_calls` | `ai.py` | Append-only. One row per provider attempt, failed ones included: purpose, model, prompt version, tokens, cost, latency, and the document or turn it is billed to. |

Uploaded files live on disk at a path derived from their SHA-256 (`context.BlobStore`), so the database stores no path and the same bytes are stored once.

## Request flows

**Upload.** `POST /documents` streams the file to disk while hashing it and stops at `MAX_UPLOAD_BYTES` (50 MB). A Content-Length over the limit gets 413 before the body is read. `pages.sniff_kind` reads the first bytes, not the extension. The insert uses `ON CONFLICT (sha256) DO NOTHING`, so bytes seen before return the existing document with 200 and cost nothing. A new document returns 202 and is enqueued on the `ingest` queue.

**Index.** `documents.run_index` takes a per-document advisory lock (`pg_try_advisory_lock`), so a duplicate delivery returns at once. Parsing extracts pages, cuts chunks and writes pages, chunks and parse facts in one transaction. It then enqueues insights and embeds pending chunks in batches of 128, one transaction per batch. A crash resumes at the first chunk without an embedding, and no parse or batch is paid for twice. A retryable failure raises for Celery to retry (five retries, 2 to 32 seconds apart). The last failure writes `failed` while the worker still holds the lock. `reconcile` runs every 60 seconds and re-enqueues stalled documents, at most 50 per tick.

**Insights.** `insights.run_analyze` runs on the `llm` queue beside embedding, under its own lock, so a slow summary never delays `ready`. It sends the pages that fit in 240,000 characters, in order, and records how many it read (`coverage`). A key fact is kept only when `pages.find_on_page` finds its quote on the page it cites. The rest are dropped and counted.

**Ask.** `chat.ask` runs these steps:

1. After the first question, the small model rewrites each question into a standalone query from the last four turns. The first question is used as asked.
2. Embed the query, then run one SQL statement over the chat's documents (see Retrieval).
3. Refuse with `no_evidence`, and spend no answer call, when no chunk passes the relevance gate.
4. Send eight sources to the main model and get back an answer with `[S#]` markers and a verbatim quote per marker.
5. Keep a citation only when `pages.locate_quote` finds its quote in the source it names. The citation's page is the page where the quote starts inside the chunk. With nothing verified, the turn is `unverified`.
6. Write one `chat_turns` row. Usage is read back from `llm_calls` by turn id.

No database transaction stays open across a model call.

## Retrieval

`chat.EXACT_SQL` scores every chunk of the chat's documents in one statement:

- Cosine distance to the query vector, for chunks whose `embedding_model` matches the current model. A model switch never compares vectors from two embedding spaces.
- `ts_rank_cd` against the query terms joined with OR. With AND, "Compare the general aggregate limit in the two documents." matched none of the two documents' eight chunks.
- Reciprocal rank fusion of the two rankings (k = 60), over the top 50 of each, plus each document's best two chunks by distance.

`chat.select_sources` gives each document its best two chunks first, then fills to eight by fused score, so a comparison sees every document. The gate passes when the best similarity reaches the embedder's `min_similarity`, or when one chunk contains every query term. The second arm keeps exact identifiers such as a form number from being refused by a semantic threshold. The fake embedder's threshold (0.184) comes from the eval set. The OpenAI threshold (0.25) is uncalibrated, and `docs/evals.md` gives the command that calibrates it.

The scan is exact, not approximate, because a chat's scope is a few documents and a few thousand chunks at most. There is no vector index.

## Prompts

`docvault/prompts.py` holds every prompt, each with a name and version that every `llm_calls` row records. Each prompt carries a comment on why it is worded the way it is. In short:

- **condense** (small model) keeps the user's wording, amounts and defined terms, because the rewrite drives both the embedding and the full-text search. It must not answer, because the answer would be searched instead of the question.
- **answer** (main model) treats source text as data, never instructions. It asks for a verbatim quote of at most 40 words per marker, because `chat.ground` drops every quote it cannot find. It asks for figures exactly as written and a refusal (`supported: false`) over a guess, because a wrong limit costs a broker more than a follow-up question. It answers the covered part of a question and names the rest.
- **insights** (main model) writes the summary for a broker deciding whether to open the document. It asks for the `<page n>` of each key fact and an exact quote, because `insights.verify` checks the quote on that page.
- **repair** shows the model its invalid JSON and the validator's errors once, under the original prompt's name, so the ledger counts repairs against the prompt that needed them.

All three calls use strict JSON-schema output (`response_format`). The schemas are the `*Draft` models in `prompts.py`.

## Assumptions

- The users are insurance brokers and their clients, and the documents are policies, quotes and endorsements in English. The full-text configuration is `english`.
- Files are PDF, DOCX or TXT, at most 50 MB and 2,000 pages. Scanned PDFs fail with `no_extractable_text`, because OCR is out of scope.
- DOCX and TXT have no reliable page numbers, so they are cut into sections and cited as "section n", never "p. n".
- One vault, one tenant, no authentication. Rate limits are per client address.
- A chat's documents are fixed when it starts. A deleted document makes its chats refuse new questions (409 `documents_deleted`) instead of answering from fewer documents.
- Without a provider key, the stack runs on a deterministic fake provider, so the whole system runs and is tested offline.

## Scale

### What is measured

`scripts/scale_probe.py` seeds synthetic rows into a scratch database on the compose Postgres and reports the median of five runs of each read. Machine: the development Mac, Postgres 16 in Docker.

| Read | 5,000 documents | 50,000 documents |
|---|---|---|
| `GET /metrics/documents` | 2.1 ms | 15.3 ms |
| `GET /metrics/processing?window=24h` | 8.8 ms | 57.3 ms |
| `GET /metrics/processing?window=7d` | 21.6 ms | 217.6 ms |
| `GET /documents` (first page of 20) | 0.7 ms | 1.2 ms |
| `reconcile` rule 1 pick (500 and 5,000 stale documents) | 0.2 ms | 1.5 ms |
| `chat.retrieve` over 3 documents (30 chunks) | 1.6 ms | 1.0 ms |

The 50,000-document set has 135,000 `step_runs`, 150,000 `llm_calls` and 50,000 `chat_turns` rows, spread over seven days. Reruns differ by up to about 20%. The metrics reads grow with the rows inside the window. Retrieval cost depends on the chat's scope, not on the vault's size. On the fake provider, a sample document reaches `ready` in 62 to 118 ms (`scripts/demo.sh` output).

### What is reasoned

- Ingest throughput is set by worker concurrency (4 in compose) and the provider's rate limits. Embedding sends 128 chunks per call, and insights make one call per document. More `worker` replicas scale ingest, and each document's lock keeps duplicates harmless.
- `reconcile` adds at most 50 duplicate index messages a minute, however long the backlog is. It used to add one per stale document per minute.
- Duplicate uploads cost nothing: the hash makes them one document.
- Spend is visible per purpose and model in `GET /metrics/processing`, and per turn in each answer's `usage`.

### What changes past that

- Past a few million `step_runs` or `llm_calls` rows, the windowed metrics become materialized views that beat refreshes, or the tables are partitioned by month.
- Search across the whole vault, rather than inside a chat's scope, needs an HNSW index per embedding space: a partial expression index on `embedding::vector(N)` for each `embedding_model`.
- With more than one API process, the rate-limit buckets move to Redis, because each process now keeps its own.
- File storage moves to S3. `BlobStore` is the only code that touches files.
- `reconcile` rule 2 (re-embed after an embedding-model change) enqueues every outdated document in one tick. Past a few thousand documents it needs the same batch as rule 1.
- Insights read a prefix of a long document. Past the 240,000-character budget, a map-reduce over the remaining pages is the next step.
