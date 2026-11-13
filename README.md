# Doc Vault

Upload documents, keep each re-upload as a new version, get cited insights, chat with them, compare them side by side, and rewrite a summary at the length, focus and tone a reader needs. Every answer, every compared value and every summary source points at a page you can open and check.

The service is a FastAPI app with Celery workers. Postgres 16 with pgvector is the only datastore. One Redis is the Celery broker, and a second one holds the embedding cache and the answer cache, which the app runs without. `docs/design.md` explains the data model, the request flows, retrieval, the prompts, the assumptions and how it scales. `docs/lifecycle.md` covers the indexing state machine, and `docs/evals.md` covers answer quality.

## How it was built and checked

- `AI_USAGE.md` explains how AI coding tools were used, what went wrong and what caught it.
- `docs/design.md` covers the data model, the request flow, retrieval, the prompts and how the system scales. `docs/evals.md` has the eval results, and `docs/lifecycle.md` has the model-checked document lifecycle.
- `.claude/skills/verify-doc-vault/` drives the running app end to end on an isolated stack, and `.claude/skills/verify-lifecycle-model/` rechecks the lifecycle model after a code change.

## Run it

```bash
cp .env.example .env      # then set OPENAI_API_KEY in .env. With no key the stack runs on the fake provider.
docker compose up --build -d
curl localhost:8000/health
```

`GET /health` returns 200 with `"ai_mode": "live"` when `OPENAI_API_KEY` is set, and `"ai_mode": "fake"` when it is not. Swagger is at `http://localhost:8000/docs`. The web UI is at `http://localhost:3000`: upload documents, read their insights, chat with citations that open the cited page, and see the metrics. `web/README.md` describes it.

`API_PORT`, `DB_PORT` and `WEB_PORT` change the host ports, so a second stack can run beside the first:

```bash
API_PORT=18000 DB_PORT=54339 WEB_PORT=13000 docker compose -p docvault-demo up --build -d
```

Three settings control spend, caching and log format. `docvault/config.py` lists every setting, and `.env.example` the common ones.

| Variable | In compose | Effect |
|---|---|---|
| `DAILY_AI_BUDGET_USD` | Taken from the shell or `.env` by `api` and `worker`. Unset by default. | A cap on AI spend per UTC day, in US dollars, above 0. Unset or empty means no cap. |
| `CACHE_URL` | Set to `redis://cache:6379/0` on `api` and `worker`. | The Redis for the embedding cache and the answer cache. Unset or empty means no cache. |
| `LOG_FORMAT` | Set to `json` on `api`, `worker` and `beat`. | `json` writes one JSON object per line. `text`, the default outside compose, writes `LEVEL logger message key=value ...`. |

Compose sets `CACHE_URL` and `LOG_FORMAT` in each service's `environment`, which overrides `.env`. To start the stack with a cap of $5 a day:

```bash
DAILY_AI_BUDGET_USD=5 docker compose up -d
```

## Run the demo

`scripts/demo.sh` drives the API with `curl` and `jq`. It uploads the three samples, waits until they are ready and their insights exist, prints the insights, then chats with them and prints every answer with its citations. It ends with the metrics.

```bash
scripts/demo.sh                              # API on localhost:8000
API=http://localhost:18000 scripts/demo.sh   # another stack
```

A rerun on the same stack finds the samples already uploaded (HTTP 200) and reuses them.

## Test it

The tests use the compose Postgres on host port 54329, in their own database (`vault_test`). They call no model provider.

```bash
docker compose up -d db
uv run pytest
uv run ruff check
```

`TEST_DATABASE_URL` points the tests at another database. The eval test also creates `<that name>_evals`.

## Assumptions and design choices

The app assumes:

- The users are insurance brokers and their clients, and the documents are policies, quotes and endorsements in English.
- Files are PDF, DOCX or TXT, at most 50 MB and 2,000 pages. A scanned PDF fails with `no_extractable_text`, because OCR is out of scope. A PDF that needs a password to open fails with `encrypted_pdf`.
- DOCX and TXT have no reliable page numbers, so their citations name a section, not a page.
- There is one vault and no login. Rate limits apply per client address.
- A chat's documents are fixed when the chat starts. If one of them is deleted, the chat refuses new questions with 409 `documents_deleted`.

The app uses neither LangChain nor LlamaIndex. Every citation must point at the page its words are on, and two steps in the code make that hold:

- `pages.chunk` cuts the text into chunks and records the page of every character, so a chunk that crosses a page break still knows where each word came from.
- `chat.ground` keeps a quoted passage only when `pages.locate_quote` finds it in the source the model named, and cites the page where that passage starts.

Retrieval is one SQL statement, `chat.EXACT_SQL`, that ranks the chat's chunks by pgvector distance and Postgres full-text rank. A framework would put its own chunking and retrieval in place of these steps. Kept in the repo, each step is tested and can be explained line by line. `docs/design.md` covers the other choices.

## API

`docs/api.md` is the full reference, with a `curl` example for every route. Swagger is at `http://localhost:8000/docs`.

## Sample documents

`samples/` holds three fictitious documents for the same insured: a seven-page commercial general liability policy (PDF), a three-page renewal quote with changed figures (PDF), and a short endorsement (DOCX). `samples/facts.yaml` lists the facts planted in them, each with its document, page, and exact text. The tests and evals read that file as their expected values.

To regenerate the samples, run `uv run python scripts/make_samples.py`. The output is byte-identical on every run.

## Status

This section lists what works at the current commit.

Built:

- Compose stack: `api` on port 8000, `worker`, `beat`, `db` (pgvector 0.8.0 on Postgres 16), `redis` (the broker) and `cache` (a second Redis for the caches, which may evict any key and keeps nothing on disk).
- Upload with content-hash dedupe, format detection from the file's bytes, and a 413 from the headers when Content-Length is over the limit.
- Indexing in Celery: parse, chunk and embed, crash-safe under a per-document lock. A rerun after a crash embeds only the chunks still pending. `documents.reconcile` runs every 60 seconds. It re-enqueues stalled documents (at most 50 a tick, oldest re-enqueue first), re-embeds ready documents after the embedding model changes, and enqueues missing insights.
- The AI gateway (`docvault/ai.py`): OpenAI and a deterministic fake, retries with backoff for chat calls, strict JSON-schema output with one repair turn (none for a comparison or a summary), and one priced `llm_calls` row per provider attempt.
- Insights: title, type, summary, tags and key facts. A key fact is kept only when its quote is on the page it cites, and `coverage` reports how much of a long document was read.
- Chat: multi-document, follow-ups rewritten into standalone queries, hybrid retrieval (vector and full text fused by reciprocal rank, with a per-document quota), a relevance gate, and citations verified against the source text with their exact page.
- Streamed answers over server-sent events (`POST /chats/{id}/messages/stream`), through the same `chat.ask` as the plain route. The web UI shows the answer as it is written.
- Live document status over server-sent events (`GET /events/documents`). Postgres triggers send a NOTIFY on every commit that changes what `GET /documents/{id}` returns. One listener per API process reads each changed document once per 100 ms burst and sends it to every open stream. The web UI updates its document list from the stream and polls only while the stream is down.
- Up to three follow-up suggestions per turn, which the UI offers as one-click questions. On the evals, 108 of 108 suggestions asked come back answered.
- Side-by-side comparison (`POST /comparisons`): 2 to 5 ready documents aligned field by field in one model call. Each cell's quote is checked on the page it cites, and `differs` is computed in code. In the web UI, the Compare section, the new-chat screen and each document page start one, and every value opens its page with the quote marked. On the samples, the fake marks 3 of the 4 planted changes between the policy and its renewal quote.
- Summary options (`POST /documents/{id}/summaries`): the document's summary written again at a length (about 60, 150 or 300 words), about a focus, and in a plain, formal or client tone, from the page prefix insights read. Up to 6 sources are each checked on the page they cite. A summary is stored by document, normalised options, model and prompt version, so asking again returns it with no model call. In the web UI, "Rewrite the summary" sits under each ready document's summary, with its earlier rewrites, and each source opens its page with the quote marked. On the fake, every source of the three samples grounds at every length (`docs/evals.md`).
- Document versions: new bytes under a stored filename, or sent with `replaces=<id>`, become the next version of that document's family (`family_id`, `version`, `latest_version`). A unique index on the family and number makes two concurrent uploads into one family take the next two numbers; the one that loses retries with the next number. An upload that loses 10 races in a row is 409 `version_conflict` with `retryable: true`, and its bytes are not kept. `GET /documents?latest=true` lists each family once and `GET /documents/{id}/versions` lists a family. In the web UI, the rail lists the latest version of each document with a "v2" mark, and follows live events as versions arrive and go. A document page says "Version 2 of 2" with links to the others. An earlier version opens with a note that links the newest. "Compare with version N" compares the two, "Versions (n)" lists the family, and "Upload a new version" sends a file of any name as the next version.
- Request ids and logs: every response carries `X-Request-ID`, the client's own when it is safe to echo, or a fresh one. The API logs one line per request when the response ends, streams included, with the method, route template, status, latency and client. Celery carries the id into the tasks a request queues, so the worker's step lines and every `llm_calls` row name the request that caused them. Compose writes the API, worker and beat logs as one JSON object per line. In the web UI, a server failure ends with "Reference <id>.", so a reader can quote it.
- Daily AI spending cap (`DAILY_AI_BUDGET_USD`): once today's spend in the ledger reaches the cap, asks, comparisons and new summaries get 429 `quota_exceeded` with `Retry-After` until midnight UTC, and new insights fail with a reason that says why. The gateway never caps embedding calls, so uploads are indexed and reach `ready` over the cap. `/health` reports the cap and today's spend. In the web UI, Ask, Compare and Rewrite the summary say the cap and when it resets in the reader's time, with no Try again until then, and Metrics has a Spending cap section.
- Caches in a second Redis (`CACHE_URL`): the embedding cache sends the provider only texts that no earlier call embedded with the same model, so a repeated text costs nothing. The answer cache returns a chat's first question, answered before on the same documents, as a new turn marked `cached` with no model call, over the spending cap too. When the cache is down, the app runs as before without the savings, and `/health` reports `cache: "unreachable"`. `/metrics/processing` counts each cache's hits and misses. In the web UI, a cached turn reads "Saved earlier, no new model call.", and Metrics has a Caches section with hits, misses and hit rate.
- Metrics: `GET /metrics/documents` and `GET /metrics/processing`, each figure read at request time from the one table that records it, apart from the cache counts, which the cache Redis keeps.
- Per-client rate limits on upload and ask.
- Evals: `python -m docvault.evals` scores retrieval, citations, refusals, insights, comparisons and summary sources separately on a fixed broker case set, and calibrates the relevance gate. Results are in `docs/evals.md`.
- A TLA+ model of the indexing lifecycle, checked by `formal/check.py` (`docs/lifecycle.md`).
- `scripts/demo.sh` and `scripts/scale_probe.py`.

Known limits:

- On the fake provider, the answer step often quotes a neighbouring passage: citation accuracy is 7 of 34 facts while retrieval finds 21 of 34. Broker-worded questions that share few words with the text are refused by the gate (`docs/evals.md` lists each).
- The fake gate threshold (0.184) has a margin of 0.003 on each side on the eval set. The OpenAI threshold (0.20) sits below the lowest answerable turn of the calibration run, so the gate refuses only off-topic questions and the answer step declines the rest (`docs/evals.md`).
- The tests use canned HTTP responses for the OpenAI backends. One live run on 2026-11-12 covered the eval set and 19 public insurance documents, with streaming, suggestions, comparisons and summaries (`docs/evals.md`, `AI_USAGE.md`).
- "Differs" in a comparison compares the cells' text, not their meaning, so two wordings of the same term are marked as different.
- The fake provider simulates a model's pacing when it streams: it sends its finished answer a word at a time, 25 ms apart in compose (`FAKE_STREAM_DELAY_MS`). The plain route has no delay.
- Insights read a page prefix that fits 240,000 characters. Longer documents report partial coverage; map-reduce over the rest is not built.
- A key fact is checked only for its quote being on the cited page, not for its value appearing in the quote.
- Insights rerun only when the prompt version changes, not when the model or the provider key changes.
- During a re-embed after an embedding-model change, a chat over that document refuses with `no_evidence`, and the embedding progress count reads as complete.
- After an embedding-model change, chats answer only from documents already re-embedded until reconcile finishes.
- Rate limits are per API process and per client address. Behind a proxy, such as the web UI's, every user shares the proxy's address.
- Gateway retries have no jitter, and Celery's retry countdown (2 to 32 seconds) ignores the provider's Retry-After header.
- Live status runs one database listener per API process. Each process reads a changed document once, whatever the number of streams it serves. A deployment with several API processes holds one listener per process and fans out the same way. Each process takes at most 100 streams.
- Postgres drops notifications while the listener is disconnected. After it reconnects, every stream gets `resync` and the web UI reads the list again.
- An open event stream never ends by itself, so stopping the API gives requests 5 s (`--timeout-graceful-shutdown 5` in compose) and then cancels them. Uvicorn logs that cancellation as an error.
- Over HTTP/1.1 a browser allows six connections to one host across all its tabs, and each visible tab of the web UI holds one for its stream. A hidden tab gives its stream back and reads the list again when shown.
- In the web UI, a document the rail does not list, opened by its link, is read again on each event that names it, so it updates but shows its loading state for a moment each time. That is a document older than the newest 100, or any earlier version, which also gets an event whenever a version of its family is uploaded or deleted. While the stream is down and nothing in the list is processing, changes from elsewhere show when the stream reconnects, at most 30 s later.
- A comparison runs synchronously: one model call of at most 90 s (`COMPARE_TIMEOUT_S`), with no gateway retry and no repair turn, so the API answers before the web proxy's 120 s timeout. Output that fails validation is a retryable 502 `ai_bad_output`, and the UI offers Try again.
- Each compared document gets an even share of the 240,000-character insights budget, so five long documents are read for at most 48,000 characters each. `pages_read` says how far, and the UI says when a document was read only in part.
- A comparison cell is checked for its quote being on the cited page, not for its value appearing in the quote. `differs` compares the values, so it trusts the model's copy of each one.
- `differs` knows only the comma as a thousands separator and only `$`, `€`, `£`, `USD`, `EUR` and `GBP` as currencies, so `€1.000,00` and `€1,000.00`, or `$1,000` and `US$1,000`, show as a difference. This errs toward showing an equal row, never toward hiding a change.
- The fake provider aligns fields only when two or more documents give them the same label. It misses the premium change in the samples ("Total Advance Premium" and "Total Annual Premium") and leaves out fields only one document has.
- A summary runs synchronously, like a comparison: one model call of at most 90 s (`SUMMARY_TIMEOUT_S`), with no gateway retry and no repair turn, so the API answers before the web proxy's 120 s timeout. The OpenAI client applies the 90 s to each phase of the HTTP call (connect, write, read), so it bounds a normal call rather than being one wall-clock deadline. This is read from the client's request options, not measured, and it holds for comparisons too.
- A stored summary is keyed by the document, the normalised options, the model and the prompt version. A change to the insights budget (`INSIGHTS_MAX_CHARS`) or the reasoning effort still serves summaries made under the old setting. Bump `SUMMARY_VERSION` with such a change.
- A stored summary or an answer from the answer cache still spends one of the client's per-minute asks, because the rate limit runs before the route.
- The fake's summaries are passages of the page text: 2, 4 or 8 of them after a tone's fixed opening, so they run far under the word targets (12 to 39 words on the samples). They show that each option changes the output, not how well a model writes.
- In the web UI, a summary request belongs to its document page. A reader who leaves while it runs and asks again on return starts a second model call, which returns the summary the first one stored.
- Chats and comparisons cannot be deleted, and a summary is deleted only with its document.
- A version is linked by filename alone, so two unrelated files uploaded under one name, such as `scan.pdf`, become versions of one document. No route splits a family or moves a version to another, so the only fix is to delete the version and upload it under another name.
- Two concurrent first uploads of one new name both find no stored document with that name, so each starts its own family at version 1. A later upload of the name joins the newer of the two.
- Version numbers never shift, but the highest one can be used again: delete version 2 of 2, and the next upload becomes version 2 again. A gap left by deleting an earlier version is not filled.
- An upload with no filename is stored as `upload`, so every such upload joins one family.
- Bytes the vault already holds return the existing document even when `replaces` names another family. Nothing is linked, and the response's `family_id` says which family the bytes belong to.
- `GET /documents/{id}/versions` returns the whole family in one response, with no paging.
- In the web UI, the rail and both pickers list only the latest version of each document. An earlier version is opened from a link on its family's document pages. Its own page asks about it ("Ask about this document") and compares it ("Compare with version N", or "Compare with...", which ticks it in the compare picker).
- The spending cap is checked before each call to a chat model, not reserved. Calls already running when the cap is reached finish and are billed. Each process reads today's spend at most every 10 s, and the API and each worker child are separate processes. So the day's spend can pass the cap.
- Embedding calls are never refused, but their cost counts toward the day's spend, so indexing over the cap keeps adding to it.
- A call to a model with no price entry stores no cost, so it never counts toward the cap.
- The day is the UTC day, so the cap resets at midnight UTC whatever the client's time zone. Stored failed insights say "Retry after midnight UTC", not a local time, and keep no reset time.
- Only a chat's first question is cached. Follow-ups never are, because their answer depends on the turns before them. Refusals never are, so a refused question gets a fresh attempt.
- The answer cache matches a question's text without case or extra spaces, so a reworded question is a miss.
- The embedding cache keys the exact text, and a chunk's text starts with its document's filename. The same text uploaded under another filename is a miss.
- The cache counts run from `counted_since`, not from the metrics window. The cache keeps nothing on disk and may evict any key, the counters included, so a cache restart sets them back to zero with `counted_since` null until the next count.
- While the cache is unreachable, each process logs one `docvault.cache` warning a minute and skips the cache for that minute. After the cache comes back, a process that saw it fail still skips it until its minute ends, while `/health` already reads `ok`.
- A delete racing an upload of the same bytes can delete the blob that the new row needs.
- An upload sent without Content-Length is read in full into a temporary file before its size is checked, so the limit bounds what is stored, not what a client can send.
