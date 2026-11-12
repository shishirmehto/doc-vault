# Design

This page explains how Doc Vault stores documents, processes them, and answers questions about them. Every number and name on it comes from the code at the commit that changed it last. `docs/lifecycle.md` covers the indexing state machine and its TLA+ model, and `docs/evals.md` covers answer quality.

## Data model

Postgres 16 with pgvector holds every table. `docvault/schema.sql` is the only schema file, and `db.migrate()` applies it at process start when its hash is new. Each table has one module that writes it. `metrics.py` reads every table and writes none.

| Table | Written by | Holds |
|---|---|---|
| `documents` | `documents.py` | One row per distinct file (unique `sha256`): filename, kind, size, `status`, the parse facts (`page_basis`, `page_count`, `chunk_count`, `empty_pages`), the error of a failed document, and `requeued_at`, which only `reconcile` reads. `family_id` and `version` place it among the versions of one document, unique together. A first version's `family_id` is its own id. |
| `pages` | `documents.py` | The text of each page, or of each roughly 3,000-character section of a DOCX or TXT file. Citations and insights point here. |
| `chunks` | `documents.py` | Slices of about 1,800 characters with 250 characters of overlap. Each has a `page_map` of offsets to pages, an embedding tagged with `embedding_model`, and a generated `tsvector`. |
| `step_runs` | `db.pipeline_steps` | Append-only. One row per attempt of `parse`, `embed` or `analyze`, with its outcome and error code. |
| `document_insights` | `insights.py` | One row per document once insights exist or fail. No row means pending. |
| `chats`, `chat_documents` | `chat.py` | A chat and its fixed document scope, with a filename snapshot. |
| `chat_turns` | `chat.py` | One row per question: the standalone query, the outcome, the answer, a citation snapshot, up to three follow-up suggestions, and `cached`, true when the answer came from the answer cache with no model call. |
| `comparisons` | `compare.py` | One row per comparison: the focus, a snapshot of its documents in column order (filename, version, page basis, pages read), the rows as jsonb with each cell's value, page and verified quote, and how many cells grounding emptied. |
| `document_summaries` | `summaries.py` | One row per distinct summary request: the length, the normalised focus (`''` for none), the tone, the text, the sources with their pages and verified quotes, how many sources grounding dropped, and the model and prompt version. Unique on document, length, focus, tone, model and prompt version. Deleted with its document. |
| `llm_calls` | `ai.py` | Append-only. One row per provider attempt, failed ones included: purpose, model, prompt version, tokens, cost, latency, the document, turn, comparison or summary it is billed to (`one_owner` allows exactly one), and the `request_id` of the API request that caused it. |

Uploaded files live on disk at a path derived from their SHA-256 (`context.BlobStore`), so the database stores no path and the same bytes are stored once. The cache Redis holds embeddings and answers that the app can compute again, and its own hit and miss counts (see Caches).

`llm_calls.request_id` and `chat_turns.cached` are added by `ADD COLUMN IF NOT EXISTS` in the upgrades section of `schema.sql`, with `cached` defaulting to false. An existing volume gains both on the next start, and a stored turn reads as not cached.

## Request flows

**Upload.** `POST /documents` streams the file to disk while hashing it and stops at `MAX_UPLOAD_BYTES` (50 MB). A Content-Length over the limit gets 413 before the body is read. `pages.sniff_kind` reads the first bytes, not the extension. The insert does nothing on a conflict over `sha256`, so bytes seen before return the existing document with 200 and cost nothing. A new document returns 202 and is enqueued on the `ingest` queue.

**Versions.** The same insert places the new document in a family. One statement (`documents._INSERT_VERSION_SQL`) picks the family, given by `replaces=<id>` or else the family of the newest document whose `lower(filename)` matches, and takes the family's highest version plus 1. With no family it starts one at version 1, whose id is the new row's own id. Its `ON CONFLICT DO NOTHING` names no index, so it covers both unique keys: `sha256` and `documents_family_version` on `(family_id, version)`. When no row comes back, `_insert_version` reads by hash. A match means the bytes are stored, so the upload returns Existing. No match means a concurrent upload committed that version number first. The next attempt is a new statement with a new snapshot, so it sees that version and takes the number after it. Each retry follows another upload's commit, and after 10 attempts the upload fails with a retryable `internal_error`. A `replaces` id is checked before the file is read, so an unknown one stores nothing.

The migration in `schema.sql` adds `family_id` as nullable, sets it to the row's id where it is null, then makes it `NOT NULL`. `version` is added with default 1. On a fresh volume `CREATE TABLE` has both columns, and on an existing one the upgrade statements make each stored document its own family at version 1. Both are idempotent, so applying the file again changes nothing.

`latest_version` is read, never stored: `max(version)` over the family, in the same statement as the document. `GET /documents?latest=true` keeps the rows with no higher version in their family. Deleting a version deletes one row, so the others keep their numbers, and the family's next upload takes the highest remaining number plus 1. Chats store document ids, so a chat keeps the version it was started with. Summaries cascade with their own version.

**Index.** `documents.run_index` takes a per-document advisory lock (`pg_try_advisory_lock`), so a duplicate delivery returns at once. Parsing extracts pages, cuts chunks and writes pages, chunks and parse facts in one transaction. It then enqueues insights and embeds pending chunks in batches of 128, one transaction per batch. A crash resumes at the first chunk without an embedding, and no parse or batch is paid for twice. A retryable failure raises for Celery to retry (five retries, 2 to 32 seconds apart). The last failure writes `failed` while the worker still holds the lock. `reconcile` runs every 60 seconds and re-enqueues stalled documents, at most 50 per tick.

**Insights.** `insights.run_analyze` runs on the `llm` queue beside embedding, under its own lock, so a slow summary never delays `ready`. It sends the pages that fit in 240,000 characters, in order, and records how many it read (`coverage`). A key fact is kept only when `pages.find_on_page` finds its quote on the page it cites. The rest are dropped and counted. Over the spending cap the insights call raises `quota_exceeded` with `retryable: false`, so Celery does not retry it and the insights are stored as failed, for `POST /documents/{id}/retry` to run again after the cap resets.

**Ask.** `chat.ask` runs these steps:

1. Check that the chat exists and none of its documents is deleted, and read its scope and last four turns, in one connection.
2. For a chat's first question only, look the answer up in the answer cache under `chat.answer_key` (see Caches). A hit writes a `chat_turns` row marked `cached = true` with the stored answer, citations and suggestions, and returns it. It makes no model call of any kind, the query embedding included.
3. Check the spending cap with `AI.check_budget` (see Spending cap). Over the cap, raise 429 `quota_exceeded` before any model call, even one the gate would have saved.
4. After the first question, the small model rewrites each question into a standalone query from the last four turns. The first question is used as asked.
5. Embed the query, then run one SQL statement over the chat's documents (see Retrieval).
6. Refuse with `no_evidence`, and spend no answer call, when no chunk passes the relevance gate.
7. Send eight sources to the main model and get back an answer with `[S#]` markers and a verbatim quote per marker.
8. Keep a citation only when `pages.locate_quote` finds its quote in the source it names. The citation's page is the page where the quote starts inside the chunk. With nothing verified, the turn is `unverified`.
9. Keep up to three of the model's `follow_ups` as suggestions (`chat.suggestions`): questions that end in "?", are at most 160 characters and differ from the question just asked. A turn the gate refused had no model call, so `chat.fact_suggestions` asks "What is the <label>?" for the documents' verified key-fact labels of two words or more, taken round by round across the documents.
10. Write one `chat_turns` row. Usage is read back from `llm_calls` by turn id.
11. When the turn is a first question and `answered`, store it in the answer cache.

No database transaction stays open across a model call.

**Ask, streamed.** `POST /chats/{id}/messages/stream` runs the same `chat.ask`. The only difference is three optional sinks, which the plain route does not pass:

- `on_stage` hears `condensing` (only when there is history), `searching` and `answering` as each step starts. The first one comes after the chat, its scope, the answer cache and the spending cap are checked. An answer from the answer cache sends no stage. It sends its whole text to `on_text` once, with the `[n]` markers and the spaces before them removed.
- `on_text` gets provisional answer text. The gateway streams the answer call's raw JSON into a `TextSink`. `answer_text.AnswerField` decodes the top-level `answer` string as it arrives, and `answer_text.MarkerFilter` removes `[S#]` markers, holding back a partial one until it resolves. `follow_ups` comes after `evidence` in the draft, so the answer streams first.
- `on_reset` fires when a retry or the repair turn starts after text was sent, because that text is void.

`api.AskInThread` runs `ask` in a worker thread that puts server-sent event frames on an unbounded queue. The route blocks until the first item arrives. If that item is an error (`chat_not_found`, `documents_deleted`, `quota_exceeded`), it answers with the usual status and envelope. Otherwise it streams the frames, then one `turn` or `error` event. A client that disconnects stops reading the queue, but the thread runs on, stores the turn and bills every attempt, so the history shows the answer. The provisional text is never trusted: the `turn` event carries the grounded answer, which replaces it.

The response has `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no`. `no-transform` matters for the web UI: its Next.js rewrite proxy gzips `text/event-stream` and so delivered every event at once when the answer ended. The proxy's compression skips a response marked `no-transform`.

**Compare.** `compare.create` lines up 2 to 5 ready documents field by field:

1. In one transaction, `chat.ready_documents` checks the documents exist and are ready, with the 404 and 409 that `chat.start` gives. The same transaction reads each document's pages and verified key facts, then commits.
2. `prompts.compare` labels the documents `D1` to `D5` in request order. Each gets an even share of the insights budget (240,000 characters), filled by the page-prefix rule that insights use, and each lists its key facts before its pages. The prompt returns how many pages of each it read.
3. One main-model call returns rows, each with one cell per document: value, page and quote. It is billed to the comparison id, which exists before the call. The call has one attempt of at most 90 s, because the request is synchronous and the web proxy gives up at 120 s.
4. `compare.ground` places each cell by its document id, ignoring case and spaces, and keeps it only when `pages.find_on_page` finds its quote on the page it cites. It stores the source span, not the model's copy. Any other filled cell becomes empty and counts in `dropped_cells`: a quote not on its page, a missing page or quote, or a cell naming no document or one already placed. A row left with no filled cell is removed. Rows are read until 30 are kept, and the rest are not read.
5. One `comparisons` row is written and read back with its usage from `llm_calls`.

`Row.differs` in `domain.py` decides a difference when the comparison is read, from the values: casefolded, with thousands separators and zero cents removed, whitespace collapsed and trailing punctuation stripped. Currency symbols and codes become one code per currency, kept apart from the figure. Two distinct figures differ, two different currencies differ (a figure with no currency matches either), and an empty cell beside a filled one differs. The model is never asked. A comparison is a snapshot: its documents' filenames, page bases and coverage are stored with it, so a deleted document keeps its column and page labels, and `deleted` comes from the `documents` table at read time.

**Summaries.** `summaries.create` writes a document's summary again at a length, about a focus, in a tone:

1. `SummaryOptions.of` normalises the focus: trimmed, casefolded, spaces collapsed, blank to none. The normalised options are the cache key and also what the prompt receives, so one key always means one model input.
2. In one transaction, `chat.ready_documents` gives the 404 and 409 that chats give. A stored summary for the same document, options, model and prompt version is returned with `cached: true`, before any page is read. Otherwise the transaction reads the pages and commits.
3. `prompts.summary` sends the page prefix that insights read, under the same 240,000-character budget and coverage line, then the length's word target, the reader's interest and the tone's instruction.
4. One main-model call, billed to the summary id, which exists before it. It has one attempt of at most 90 s and no repair turn, like a comparison, because the request is synchronous and the web proxy gives up at 120 s.
5. `summaries.ground` keeps a source only when `pages.find_on_page` finds its quote on the page it cites, storing the source span. It reads sources until 6 are kept, skips a repeat of a kept span, and counts the rest in `dropped_sources`. No source surviving is allowed: a summary is not a fact claim. An empty summary text fails as a retryable 502 and is not stored.
6. The insert does nothing on a key conflict and the row is read back by key, so two identical requests at once store one summary. The later one returns the stored row with `cached: false`, because it did call the model. A document deleted during the call fails the insert's foreign key, which becomes the 404.

A document's content never changes, because it is keyed by its hash, so a stored summary never goes stale for its document. It goes when the document goes. A new version is a new document with new content, so it gets summaries of its own and inherits none.

**Live status.** `GET /events/documents` tells clients when a document changes, so the web UI does not poll. The flow has four links:

1. Triggers in `schema.sql` call `pg_notify('documents', <document id>)` on every change that alters what `GET /documents/{id}` returns. Row triggers cover `documents` and `document_insights`. A statement trigger with a transition table covers `chunks`, the embedding progress, and sends each changed document once. A row trigger on `step_runs` fires when the embed step closes `ok`, because `processing_ms` reads that row and `ready` commits just before it closes. Postgres delivers a notification when its transaction commits, never after a rollback, and delivers identical payloads from one transaction once. So an event never announces a state that did not happen, and no Python publish call exists. `documents.py` is unchanged. Inserting or deleting a version also changes `latest_version` for the rest of its family, so the `documents` trigger notifies each other member of the family on an `INSERT` or `DELETE`. An `UPDATE` changes no other row's body, so it notifies only its own row.
2. `events.listen` holds one `LISTEN documents` connection per API process, outside the pool, started by the lifespan. It probes the connection with `SELECT 1` every 15 s. When the connection drops, it reconnects with a backoff from 0.5 s doubling to 30 s, then sends `resync` to every stream, because notifications sent while no one listened are lost.
3. `events.Hub` waits 100 ms after the first nudge for a document, so a burst of commits costs one read. One consumer reads due documents in order with `documents.get` in a worker thread and puts the result on every open feed: `document` with the body, or `deleted` when the row is gone. One read per change for the whole process, rather than one per client, keeps the pool free for requests. A failed read sends `resync`. A feed more than 1000 events behind is replaced by one `resync`.
4. The route is async, so an open stream holds no worker thread. It reserves one of 100 slots before the response starts, so the 503 `too_many_streams` is a normal error envelope, and it frees the slot in a `finally` around the response's own call. A client that leaves before the body starts frees its slot too. The stream opens with `: connected`, which makes the proxy pass the headers at once, and `retry: 1000`, so a browser reconnects after a drop in 1 s rather than its default 3 s, before the UI would say live updates paused. It sends `: keepalive` after 15 s of silence.

The web UI opens one `EventSource` per page. It reads the list on load, on every open and on `resync`, and replays every event applied after a read started onto that read's result (`ListSync`). A hidden tab closes its stream and opens a new one when shown, because over HTTP/1.1 a browser allows six connections to one host across all its tabs. While the stream is down it polls as before, and after 3 s down the top bar reads "Live updates paused, retrying". The browser retries a dropped connection by itself but gives up after a non-200 answer, such as the cap's 503 or the proxy's 500 while the API restarts, so the page opens a new one after 2 s, doubling to 30 s.

Measured on the dev stack through the web proxy on port 3000: a 4.9 MB text file uploaded with curl showed in the rail 186 ms after the request started, moved through "Reading pages" and eight "Indexing n of 3329 passages" steps, and read Ready at 1970 ms. The page made no request in that time. A delete reached a second tab showing the document 282 ms after the request started, as the missing-document page. A version 2 uploaded with curl replaced version 1 in the rail 207 ms after the request started, and a second tab open on version 1 showed the newer-version note at 301 ms. Deleting version 2 brought version 1 back to the rail at 139 ms and cleared the note at 142 ms. The rail made no list read in either case.

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
- **answer** (main model, `answer-v2`) treats source text as data, never instructions. It asks for a verbatim quote of at most 40 words per marker, because `chat.ground` drops every quote it cannot find. It asks for figures exactly as written and a refusal (`supported: false`) over a guess, because a wrong limit costs a broker more than a follow-up question. It answers the covered part of a question and names the rest. It suggests up to three follow-up questions that its sources answer, so a click on one leads to a cited answer. The schema puts no limit on their count, so a fourth question costs no repair call. `chat.suggestions` keeps three.
- **insights** (main model) writes the summary for a broker deciding whether to open the document. It asks for the `<page n>` of each key fact and an exact quote, because `insights.verify` checks the quote on that page.
- **compare** (main model, `compare-v1`) lines up the fields a broker compares (limits, deductibles or retentions, premium, period, insured, insurer, exclusions and endorsements), or only the fields about the focus when one is given. Each cell names its document's id, because the code places cells by id into one column per document. Values are copied as written and quotes exactly from the cited page, because `compare.ground` empties a cell whose quote is not on its page and `differs` compares the values in code, so a rounded value would show a false difference. A value the document does not state is null, not a guess: an empty cell shows as a difference, and a guessed one would hide it. The schema has no row limit, so an extra row never fails validation, and the code keeps 30.
- **summary** (main model, `summary-v1`) treats the length as a word target, because a reader picks how much to read, not how much the document holds. The reader's interest steers what it leads with, and when the document says nothing about it the summary says so in one sentence instead of describing typical coverage, because invented coverage is the harm a broker's client cannot check. Sources cite the `<page n>` and copy the quote exactly, because `summaries.ground` drops a quote that is not on its page. The client tone explains insurance terms in everyday words and keeps every amount, date and name as written. The schema has no limit on sources, so a seventh never fails validation, and the code keeps 6.
- **repair** shows the model its invalid JSON and the validator's errors once, under the original prompt's name, so the ledger counts repairs against the prompt that needed them. A comparison or a summary gets no repair turn: each is synchronous, and a second call could outlast the web proxy's 120 s.

All five calls use strict JSON-schema output (`response_format`). The schemas are the `*Draft` models in `prompts.py`.

## Spending cap

`DAILY_AI_BUDGET_USD` caps what the gateway spends on model calls in one UTC day. Unset or empty means no cap. The ledger is the only meter. Today's spend is `coalesce(sum(cost_usd), 0)` over the `llm_calls` rows since midnight UTC, so every priced attempt counts, failed attempts and embeddings included.

`ai.DailySpend` reads that sum at most once every 10 s (`SPEND_TTL_S`) in each process, and again when the UTC day changes. A check runs before every completion call, and the reading keeps a busy process from summing the day's rows each time. The API and each of the worker's four children are separate processes with a reading each, because each builds its `Ctx` after the fork.

`AI.check_budget(purpose)` raises `VaultError(QUOTA_EXCEEDED)` when the reading is at or over the cap. `AI.complete` calls it before the first call and again before the repair turn. `chat.ask` also calls it before its first stage, so a streamed ask gets its 429 before the stream starts. `embed` never calls it. Uploads are indexed and reach `ready` over the cap, so they are ready to chat with when the cap resets. The error takes one of two shapes:

- An ask, a comparison or a summary fails with `retryable: true`, HTTP 429. `details` holds `limit_usd`, `spent_today_usd`, `resets_at` (the next midnight UTC) and `retry_after_s` (whole seconds until then, at least 1), and `api.vault_error_handler` copies `retry_after_s` into `Retry-After`. `rate_limited` shares the status, so a client tells the two apart by `error.code`.
- Insights fail with `retryable: false`, because a Celery retry seconds later would meet the same cap. `insights.fail` stores the code and the message, and `POST /documents/{id}/retry` runs them again.

The cap is checked, not reserved. The gateway learns a call's cost from the provider's token counts after the call, so it cannot hold the cost back before. Calls already running when the cap is reached finish and are billed, and a reading up to 10 s old can let more start. `/health` reports `ai_budget` from the same reading, and leaves it null while Postgres is unreachable rather than failing the probe.

## Caches

Both caches live in the Redis that `CACHE_URL` names, which compose runs as the `cache` service: at most 256 MB, `allkeys-lru` eviction, and nothing written to disk. The broker Redis keeps Redis's default of no eviction, because an evicted broker key is a lost task. Cached embeddings and answers expire after 30 days.

**Never a dependency.** `cache.Cache` wraps one client with 0.5 s connect and read timeouts and no retries. Any `RedisError` makes a lookup miss and drops a write or a count. The first failure logs one warning on `docvault.cache` and skips Redis for 60 s in that process, so an outage costs one warning a minute in each process and no wait on each call. With `CACHE_URL` unset, `Cache.off()` does nothing. `/health` pings the cache directly, outside the skip window, and never returns 503 for it.

**Embedding cache.** `AI.embed` looks every text up in one `MGET` under `docvault:emb:<model>:<dimensions>:<sha256 of the text>`. The model and the dimension name the embedding space, so a model change never reads another space's vectors. Only the misses go to the provider, each distinct text once, in batches of 128. Each batch is cached as soon as it is paid for, so a later batch's failure does not lose it. Vectors come back in input order, and a call whose texts are all hits writes no `llm_calls` row. A vector is stored as little-endian float32, 6 KB at 1536 dimensions.

A chunk is embedded as `<filename>\n<chunk text>` (`documents._embed_pending_chunks`), so it hits only under the same filename. A new version uploaded under the same filename hits for the chunks it shares with an earlier version. Identical bytes never get that far, because the upload dedupes them. The query embedding goes through the same cache, so a question text embedded before writes no `embed_query` row.

**Answer cache.** Only a chat's first question is looked up, because a follow-up's answer depends on the turns before it. `chat.answer_key` hashes canonical JSON of every input the answer depends on: a format version, each document's id and sha256, the question casefolded with its spaces collapsed, `prompts.ANSWER_VERSION`, the main chat model and its reasoning effort, the embedding model, `retrieval_k`, `retrieval_per_doc_min` and the gate's `min_similarity`. The Redis key is `docvault:answer:<that hash>`. A change to any input is a miss, so a stored answer never goes stale. A document's content never changes, and a new version is a new id and hash. The value is the outcome, the text, the citation snapshots that `chat_turns.citations` stores, and the suggestions.

Only `answered` turns are stored. A refusal asked again should get a fresh attempt from the model, and a cached refusal would pin it until the prompt version changed. A hit writes a new `chat_turns` row with `cached = true` and no `llm_calls` row, so its `usage` reads zero. It answers over the spending cap, because it makes no model call.

**Counts.** One hash, `docvault:cache:stats`, holds `embedding:hits`, `embedding:misses`, `answer:hits` and `answer:misses`, and `since`, which `HSETNX` sets once at the first count. Embedding counts are per text, and answer counts are per first question. Every process adds to the same hash, from `since` on, not per metrics window. The counts outlive API restarts but not a cache restart. `GET /metrics/processing` returns them as its `cache` block, the only counts in that response that do not come from Postgres.

Measured on a running stack: a first question asked again in a new chat on the same document, in another case and spacing, came back `cached: true` with zero calls, and its stream was one `delta` and then the `turn`. A new version under the same filename, with bytes that differed only in trailing newlines, raised the embedding hits from 0 to 1. With the `cache` service stopped, `/health` returned 200 with `cache: "unreachable"`, the metrics `cache` block was null, uploads still reached `ready`, and the API and the worker each logged one `docvault.cache` warning. Started again, the cache's counts began from zero with `counted_since` null.

## Request ids and logs

`api.RequestLog` gives every HTTP request an id. It is a pure ASGI middleware, added last so it is outermost, and so it also tags the 413 that `refuse_oversized_uploads` sends. It takes `X-Request-ID` through `logs.accept`, which keeps the value when it matches `[A-Za-z0-9._-]{1,64}` and otherwise makes a fresh `uuid4().hex`. Only a safe id is echoed and logged, so a client cannot put spaces, quotes or newlines into a log line.

The middleware sets the `logs.request_id` context variable, adds the header to the response's start message, and writes one `docvault.requests` line in a `finally` when the response body ends. A `BaseHTTPMiddleware` would get a stream's response when it starts, before the body runs. Wrapping `send` instead logs a streamed ask or `GET /events/documents` once, with its whole duration, however it ends. The line has `method`, `route` (the matched template, null when no route matched), `status` (500 when the app raised before a response started), `latency_ms` and `client`. Compose turns uvicorn's access log off (`--no-access-log`), so this is the only line per request.

`api.internal_error_handler` answers an exception no route expected with the usual envelope, 500 `internal_error` and `retryable: true`. Starlette sends that response from outside every middleware, so the handler adds `X-Request-ID` itself.

**Across threads and tasks.** A new thread starts with an empty context, so `AskInThread` runs `chat.ask` in a copy of the request's context, and a streamed ask's ledger rows carry the id. Celery carries the id as a task header. `before_task_publish` adds `request_id` when the variable is set, and `task_prerun` sets the variable from the header, or to None, so a beat task never keeps the id of the task before it. The insights task that the index task queues carries the id on. `AI._record` writes the variable into `llm_calls.request_id`, which has a partial index, so a request's spend is one query:

```sql
SELECT purpose, model, cost_usd FROM llm_calls WHERE request_id = 'docs-check-1';
```

**Format.** `logs.configure` gives the root logger one stderr handler at INFO. A filter stamps every record with the current `request_id`. uvicorn's loggers lose their own handlers and propagate to root, so the API's lines share one format. `LOG_FORMAT=json` writes one object per line with `time`, `level`, `logger`, `message`, `request_id` and every field passed through `extra=`. `text`, the default, writes `LEVEL logger message key=value ...`. Compose sets `json` on the API, the worker and beat, and Celery's `setup_logging` signal applies it in the worker and beat. `db.PipelineSteps` writes one `docvault.pipeline` line as it closes each step row, with `document_id`, `step`, `attempt`, `outcome`, `error_code` and `duration_ms`.

Measured on an isolated stack: an upload sent with `X-Request-ID: phase-f-trace-<tag>` gave five worker lines with that id. They were the parse and embed steps, both `ok`, Celery's `index_document` success line, the analyze step, and the `analyze_document` success line. The upload's `embed_chunks` and `insights` rows in `llm_calls` carry the same id.

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

The 50,000-document set has 135,000 `step_runs`, 150,000 `llm_calls` and 50,000 `chat_turns` rows, spread over seven days. Reruns differ by up to about 20%. The metrics reads grow with the rows inside the window. Retrieval cost depends on the chat's scope, not on the vault's size. On the fake provider, a sample document reaches `ready` in 62 to 118 ms on a warm worker and in about 900 ms on the first run after a fresh start (`scripts/demo.sh` output).

### What is reasoned

- Ingest throughput is set by worker concurrency (4 in compose) and the provider's rate limits. Embedding sends 128 chunks per call, and insights make one call per document. More `worker` replicas scale ingest, and each document's lock keeps duplicates harmless.
- `reconcile` adds at most 50 duplicate index messages a minute, however long the backlog is. It used to add one per stale document per minute.
- Duplicate uploads cost nothing: the hash makes them one document.
- Spend is visible per purpose and model in `GET /metrics/processing`, and per turn in each answer's `usage`. `DAILY_AI_BUDGET_USD` bounds it per UTC day, apart from the overshoot that Spending cap describes.

### What changes past that

- Past a few million `step_runs` or `llm_calls` rows, the windowed metrics become materialized views that beat refreshes, or the tables are partitioned by month.
- Search across the whole vault, rather than inside a chat's scope, needs an HNSW index per embedding space: a partial expression index on `embedding::vector(N)` for each `embedding_model`.
- With more than one API process, the rate-limit buckets move to Redis, because each process now keeps its own.
- File storage moves to S3. `BlobStore` is the only code that touches files.
- `reconcile` rule 2 (re-embed after an embedding-model change) enqueues every outdated document in one tick. Past a few thousand documents it needs the same batch as rule 1.
- Insights read a prefix of a long document. Past the 240,000-character budget, a map-reduce over the remaining pages is the next step. A comparison splits the same budget across its documents, so it needs the same step sooner: compare per section, then merge rows by field.
