# Doc Vault

Upload documents, get cited insights, and chat with them. Every answer points at a page you can open and check.

The service is a FastAPI app with Celery workers. Postgres 16 with pgvector is the only datastore, and Redis is the Celery broker. `docs/design.md` explains the data model, the request flows, retrieval, the prompts, the assumptions and how it scales. `docs/lifecycle.md` covers the indexing state machine, and `docs/evals.md` covers answer quality.

## How it was built and checked

- `AI_USAGE.md` explains how AI coding tools were used, what went wrong and what caught it.
- `docs/design.md` covers the data model, the request flow, retrieval, the prompts and how the system scales. `docs/evals.md` has the eval results, and `docs/lifecycle.md` has the model-checked document lifecycle.
- `.claude/skills/verify-doc-vault/` drives the running app end to end on an isolated stack, and `.claude/skills/verify-lifecycle-model/` rechecks the lifecycle model after a code change.

## Run it

```bash
cp .env.example .env      # optional. With no key the stack runs on the fake provider.
docker compose up --build -d
curl localhost:8000/health
```

`GET /health` returns 200 with `"ai_mode": "fake"` when no provider key is set. Swagger is at `http://localhost:8000/docs`. `API_PORT` and `DB_PORT` change the host ports, so a second stack can run beside the first:

```bash
API_PORT=18000 DB_PORT=54339 docker compose -p docvault-demo up --build -d
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

## API

Every error has one shape, `{"error": {"code", "message", "retryable", "details"}}`, and OpenAPI declares it on every route. The examples use `API=http://localhost:8000`.

### Documents

`POST /documents` uploads one file (PDF, DOCX or TXT, at most 50 MB). It returns 202 with a queued document, or 200 with the existing document when the same bytes were uploaded before.

```bash
curl -s -F file=@samples/sample-cgl-policy.pdf $API/documents
```

`GET /documents` lists documents, newest first, 20 per page by default. Filter with `status`, and page with the `next_cursor` of the previous page.

```bash
curl -s "$API/documents?status=ready&limit=10"
```

`GET /documents/{id}` returns one document. `status` is `queued`, `parsing`, `embedding`, `ready` or `failed`. `pages` is the stored page count once parsing has finished, in every status. A failed document has `error` and `failed_stage` (`parse` or `embed`).

```bash
curl -s $API/documents/$DOC
```

`GET /documents/{id}/insights` returns `pending`, `ready`, `failed` or `unavailable`. Ready insights have a title, a type, a summary, tags, and key facts, each with its page and a quote found on that page.

```bash
curl -s $API/documents/$DOC/insights
```

`GET /documents/{id}/pages/{n}` returns the text a citation points at, labelled `p. n` for a PDF and `section n` for DOCX and TXT.

```bash
curl -s $API/documents/$DOC/pages/2
```

`POST /documents/{id}/retry` reruns whichever step failed: indexing for a failed document, insights for failed insights. It returns 409 `nothing_to_retry` when nothing failed.

```bash
curl -s -X POST $API/documents/$DOC/retry
```

`DELETE /documents/{id}` deletes the document, its pages, chunks, insights and file. Processing history and spend stay.

```bash
curl -s -X DELETE $API/documents/$DOC
```

### Chat

`POST /chats` starts a chat over one or more ready documents. The set is fixed for the chat's life.

```bash
CHAT=$(curl -s $API/chats -H 'content-type: application/json' \
  -d "{\"document_ids\": [\"$POLICY\", \"$QUOTE\"]}" | jq -r .id)
```

A document that is not ready returns 409 `documents_not_ready`, and `details.not_ready` lists each one's `id`, `filename` and `status`.

`GET /chats` lists chats, newest first, 20 per page by default, with the same `next_cursor` paging as `GET /documents`.

```bash
curl -s "$API/chats?limit=10"
```

`GET /chats/{id}` returns one chat and its documents. A document deleted since the chat started has `deleted: true`, and the chat then refuses new questions with 409 `documents_deleted`.

```bash
curl -s $API/chats/$CHAT
```

`POST /chats/{id}/messages` asks a question of at most 2,000 characters. The answer cites each claim (`[1]` matches `citations[0]`), `interpreted_as` shows how a follow-up was read, and `usage` lists the model calls, tokens and cost of the turn. A question the documents do not answer returns `status: not_found` with a `reason`.

```bash
curl -s $API/chats/$CHAT/messages -H 'content-type: application/json' \
  -d '{"question": "Compare the general aggregate limit in the two documents."}'
```

`GET /chats/{id}/messages` returns the chat's turns, oldest first, with the same fields.

```bash
curl -s "$API/chats/$CHAT/messages?limit=50"
```

### Metrics and health

`GET /metrics/documents` counts documents by status and kind, insights by status, and totals pages, chunks, bytes and failures by error code.

```bash
curl -s $API/metrics/documents
```

`GET /metrics/processing?window=24h` reports what happened in the window (`1h` to `9999d`): per-step runs, failures by code, retries, crashes and p50/p95 durations, time to ready, queue wait, tokens and cost by purpose and model, and chat outcomes and latency.

```bash
curl -s "$API/metrics/processing?window=7d"
```

`GET /health` reports Postgres, Redis, the AI mode, the embedding model and `max_upload_bytes`. It returns 503 when Postgres or Redis is unreachable.

```bash
curl -s $API/health
```

### Errors

| Status | Codes |
|---|---|
| 400 | `invalid_cursor` |
| 404 | `document_not_found`, `page_not_found`, `chat_not_found` |
| 409 | `documents_not_ready`, `documents_deleted`, `nothing_to_retry` |
| 413 | `file_too_large` |
| 415 | `unsupported_type` |
| 422 | `empty_file`, `invalid_request` (a parameter or body failed validation; `details.errors` names each field) |
| 429 | `rate_limited`, with `Retry-After` |
| 502 | `ai_bad_output` |
| 503 | `ai_unavailable`, with `Retry-After: 5` |

## Sample documents

`samples/` holds three fictitious documents for the same insured: a seven-page commercial general liability policy (PDF), a three-page renewal quote with changed figures (PDF), and a short endorsement (DOCX). `samples/facts.yaml` lists the facts planted in them, each with its document, page, and exact text. The tests and evals read that file as their expected values.

To regenerate the samples, run `uv run python scripts/make_samples.py`. The output is byte-identical on every run.

## Status

This section lists what works at the current commit.

Built:

- Compose stack: `api` on port 8000, `worker`, `beat`, `db` (pgvector 0.8.0 on Postgres 16), and `redis`.
- Upload with content-hash dedupe, format detection from the file's bytes, and a 413 from the headers when Content-Length is over the limit.
- Indexing in Celery: parse, chunk and embed, crash-safe under a per-document lock. A rerun after a crash embeds only the chunks still pending. `documents.reconcile` runs every 60 seconds. It re-enqueues stalled documents (at most 50 a tick, oldest re-enqueue first), re-embeds ready documents after the embedding model changes, and enqueues missing insights.
- The AI gateway (`docvault/ai.py`): OpenAI and a deterministic fake, retries with backoff for chat calls, strict JSON-schema output with one repair turn, and one priced `llm_calls` row per provider attempt.
- Insights: title, type, summary, tags and key facts. A key fact is kept only when its quote is on the page it cites, and `coverage` reports how much of a long document was read.
- Chat: multi-document, follow-ups rewritten into standalone queries, hybrid retrieval (vector and full text fused by reciprocal rank, with a per-document quota), a relevance gate, and citations verified against the source text with their exact page.
- Metrics: `GET /metrics/documents` and `GET /metrics/processing`, each figure read at request time from the one table that records it.
- Per-client rate limits on upload and ask.
- Evals: `python -m docvault.evals` scores retrieval, citations, refusals and insights separately on a fixed broker case set, and calibrates the relevance gate. Results are in `docs/evals.md`.
- A TLA+ model of the indexing lifecycle, checked by `formal/check.py` (`docs/lifecycle.md`).
- `scripts/demo.sh` and `scripts/scale_probe.py`.

Known limits:

- On the fake provider, the answer step often quotes a neighbouring passage: citation accuracy is 7 of 34 facts while retrieval finds 21 of 34. Broker-worded questions that share few words with the text are refused by the gate (`docs/evals.md` lists each).
- The fake gate threshold (0.184) has a margin of 0.003 on each side on the eval set. The OpenAI threshold (0.25) is uncalibrated; `docs/evals.md` gives the command that calibrates it once a key is set.
- The OpenAI backends are tested against canned HTTP responses only. They have not run against the live API.
- Insights read a page prefix that fits 240,000 characters. Longer documents report partial coverage; map-reduce over the rest is not built.
- A key fact is checked only for its quote being on the cited page, not for its value appearing in the quote.
- Insights rerun only when the prompt version changes, not when the model or the provider key changes.
- During a re-embed after an embedding-model change, a chat over that document refuses with `no_evidence`, and the embedding progress count reads as complete.
- Rate limits are per API process and per client address. Behind a proxy, such as the web UI's, every user shares the proxy's address.
- Gateway retries have no jitter, and Celery's retry countdown (2 to 32 seconds) ignores the provider's Retry-After header.
- Chats cannot be deleted.
