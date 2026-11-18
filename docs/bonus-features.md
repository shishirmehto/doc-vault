# How the bonus features work

This page covers the features from the case study's bonus list that need more than a line: streamed answers, document versions, comparisons, summary options, live status, the spending cap, the caches, and request ids. [`docs/design.md`](design.md) covers the core: the data model, upload, indexing, insights, chat, retrieval, the prompts and scale.

## Streamed answers

`POST /chats/{id}/messages/stream` runs the same `chat.ask`. The only difference is three optional sinks, which the plain route does not pass:

- `on_stage` hears `condensing` (only when there is history), `searching` and `answering` as each step starts. The first one comes after the chat, its scope, the answer cache and the spending cap are checked. An answer from the answer cache sends no stage. It sends its whole text to `on_text` once, with the `[n]` markers and the spaces before them removed.
- `on_text` gets provisional answer text. The gateway streams the answer call's raw JSON into a `TextSink`. `answer_text.AnswerField` decodes the top-level `answer` string as it arrives, and `answer_text.MarkerFilter` removes `[S#]` markers, holding back a partial one until it resolves. `follow_ups` comes after `evidence` in the draft, so the answer streams first.
- `on_reset` fires when a retry or the repair turn starts after text was sent, because that text is void.

`api.AskInThread` runs `ask` in a worker thread that puts server-sent event frames on an unbounded queue. The route blocks until the first item arrives. If that item is an error (`chat_not_found`, `documents_deleted`, `quota_exceeded`), it answers with the usual status and envelope. Otherwise it streams the frames, then one `turn` or `error` event. A client that disconnects stops reading the queue, but the thread runs on, stores the turn and bills every attempt, so the history shows the answer. The provisional text is never trusted: the `turn` event carries the grounded answer, which replaces it.

The response has `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no`. `no-transform` matters for the web UI: its Next.js rewrite proxy gzips `text/event-stream` and so delivered every event at once when the answer ended. The proxy's compression skips a response marked `no-transform`.

## Versions

The insert that stores an upload also places the new document in a family. One statement (`documents._INSERT_VERSION_SQL`) picks the family, given by `replaces=<id>` or else the family of the newest document whose `lower(filename)` matches, and takes the family's highest version plus 1. With no family it starts one at version 1, whose id is the new row's own id. Its `ON CONFLICT DO NOTHING` names no index, so it covers both unique keys: `sha256` and `documents_family_version` on `(family_id, version)`. When no row comes back, `_insert_version` reads by hash. A match means the bytes are stored, so the upload returns Existing. No match means a concurrent upload committed that version number first. The next attempt is a new statement with a new snapshot, so it sees that version and takes the number after it. Each retry follows another upload's commit, and after 10 attempts the upload fails with a retryable 409 `version_conflict`. A `replaces` id is checked before the file is read, so an unknown one stores nothing.

`latest_version` is read, never stored: `max(version)` over the family, in the same statement as the document. `GET /documents?latest=true` keeps the rows with no higher version in their family. Deleting a version deletes one row, so the others keep their numbers, and the family's next upload takes the highest remaining number plus 1. Chats store document ids, so a chat keeps the version it was started with. A chat over two versions of one file names each as `<filename> (version N)`, so the model, the citations and the chat's document list tell them apart. Summaries cascade with their own version.

## Comparisons

`compare.create` lines up 2 to 5 ready documents field by field:

1. In one transaction, `documents.ready_documents` checks the documents exist and are ready, with the 404 and 409 that `chat.start` gives. The same transaction reads each document's pages and verified key facts, then commits.
2. `prompts.compare` labels the documents `D1` to `D5` in request order. Each gets an even share of the insights budget (240,000 characters), filled by the page-prefix rule that insights use, and each lists its key facts before its pages. The prompt returns how many pages of each it read.
3. One main-model call returns rows, each with one cell per document: value, page and quote. It is billed to the comparison id, which exists before the call. The call has one attempt of at most 90 s, because the request is synchronous and the web proxy gives up at 120 s.
4. `compare.ground` places each cell by its document id, ignoring case and spaces, and keeps it only when `pages.find_on_page` finds its quote on the page it cites. It stores the source span, not the model's copy. Any other filled cell becomes empty and counts in `dropped_cells`: a quote not on its page, a missing page or quote, or a cell naming no document or one already placed. A row left with no filled cell is removed. Rows are read until 30 are kept, and the rest are not read.
5. One `comparisons` row is written and read back with its usage from `llm_calls`.

`Row.differs` in `domain.py` decides a difference when the comparison is read, from the values: casefolded, with commas removed from whole thousands groups only, zero cents removed, whitespace collapsed and trailing sentence punctuation stripped; signs such as `+` and `-` stay. Currency symbols and codes become one code per currency, kept apart from the figure in the order they appear. Two distinct figures differ, two different currencies or the same currencies in another order differ (a figure with no currency matches either), and an empty cell beside a filled one differs. The model is never asked. A comparison is a snapshot: its documents' filenames, page bases and coverage are stored with it, so a deleted document keeps its column and page labels, and `deleted` comes from the `documents` table at read time.

## Summary options

`summaries.create` writes a document's summary again at a length, about a focus, in a tone:

1. `SummaryOptions.of` normalises the focus: trimmed, casefolded, spaces collapsed, blank to none. The normalised options are the cache key and also what the prompt receives, so one key always means one model input.
2. In one transaction, `documents.ready_documents` gives the 404 and 409 that chats give. A stored summary for the same document, options, model and prompt version is returned with `cached: true`, before any page is read. Otherwise the transaction reads the pages and commits.
3. `prompts.summary` sends the page prefix that insights read, under the same 240,000-character budget and coverage line, then the length's word target, the reader's interest and the tone's instruction.
4. One main-model call, billed to the summary id, which exists before it. It has one attempt of at most 90 s and no repair turn, like a comparison.
5. `summaries.ground` keeps a source only when `pages.find_on_page` finds its quote on the page it cites, storing the source span. It reads sources until 6 are kept, skips a repeat of a kept span, and counts the rest in `dropped_sources`. No source surviving is allowed: a summary is not a fact claim. An empty summary text fails as a retryable 502 and is not stored.
6. The insert does nothing on a key conflict and the row is read back by key, so two identical requests at once store one summary. The later one returns the stored row with `cached: false`, because it did call the model. A document deleted during the call fails the insert's foreign key, which becomes the 404.

A document's content never changes, because it is keyed by its hash, so a stored summary never goes stale for its document. It goes when the document goes. A new version is a new document with new content, so it gets summaries of its own and inherits none.

## Live status

`GET /events/documents` tells clients when a document changes, so the web UI does not poll. The flow has four links:

1. Triggers in `schema.sql` call `pg_notify('documents', <document id>)` on every change that alters what `GET /documents/{id}` returns. Row triggers cover `documents` and `document_insights`. A statement trigger with a transition table covers `chunks`, the embedding progress, and sends each changed document once. A row trigger on `step_runs` fires when the embed step closes `ok`, because `processing_ms` reads that row and `ready` commits just before it closes. Postgres delivers a notification when its transaction commits, never after a rollback, and delivers identical payloads from one transaction once. So an event never announces a state that did not happen, and no Python publish call exists. Inserting or deleting a version also changes `latest_version` for the rest of its family, so the `documents` trigger notifies each other member of the family on an `INSERT` or `DELETE`. An `UPDATE` changes no other row's body, so it notifies only its own row.
2. `events.listen` holds one `LISTEN documents` connection per API process, outside the pool, started by the lifespan. It probes the connection with `SELECT 1` every 15 s. When the connection drops, it reconnects with a backoff from 0.5 s doubling to 30 s, then sends `resync` to every stream, because notifications sent while no one listened are lost.
3. `events.Hub` waits 100 ms after the first nudge for a document, so a burst of commits costs one read. One consumer reads due documents in order with `documents.get` in a worker thread and puts the result on every open feed: `document` with the body, or `deleted` when the row is gone. One read per change for the whole process, rather than one per client, keeps the pool free for requests. A failed read sends `resync`. A feed more than 1000 events behind is replaced by one `resync`.
4. The route is async, so an open stream holds no worker thread. It reserves one of 100 slots before the response starts, so the 503 `too_many_streams` is a normal error envelope, and it frees the slot in a `finally` around the response's own call. A client that leaves before the body starts frees its slot too. The stream opens with `: connected`, which makes the proxy pass the headers at once, and `retry: 1000`, so a browser reconnects after a drop in 1 s rather than its default 3 s, before the UI would say live updates paused. It sends `: keepalive` after 15 s of silence.

The web UI opens one `EventSource` per page. It reads the list on load, on every open and on `resync`, and replays every event applied after a read started onto that read's result (`ListSync`). A hidden tab closes its stream and opens a new one when shown, because over HTTP/1.1 a browser allows six connections to one host across all its tabs. While the stream is down it polls as before, and after 3 s down the top bar reads "Live updates paused, retrying". The browser retries a dropped connection by itself but gives up after a non-200 answer, such as the cap's 503 or the proxy's 500 while the API restarts, so the page opens a new one after 2 s, doubling to 30 s.

Measured on the dev stack through the web proxy on port 3000: a 4.9 MB text file uploaded with curl showed in the rail 186 ms after the request started, moved through "Reading pages" and eight "Indexing n of 3329 passages" steps, and read Ready at 1970 ms. The page made no request in that time. A delete reached a second tab showing the document 282 ms after the request started, as the missing-document page. A version 2 uploaded with curl replaced version 1 in the rail 207 ms after the request started, and a second tab open on version 1 showed the newer-version note at 301 ms. Deleting version 2 brought version 1 back to the rail at 139 ms and cleared the note at 142 ms. The rail made no list read in either case.

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
