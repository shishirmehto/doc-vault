# Doc Vault

Upload documents, keep each re-upload as a new version, get cited insights, chat with them, compare them side by side, and rewrite a summary at the length, focus and tone a reader needs. Every answer, every compared value and every summary source points at a page you can open and check.

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

`GET /health` returns 200 with `"ai_mode": "fake"` when no provider key is set. Swagger is at `http://localhost:8000/docs`. The web UI is at `http://localhost:3000`: upload documents, read their insights, chat with citations that open the cited page, and see the metrics. `web/README.md` describes it.

`API_PORT`, `DB_PORT` and `WEB_PORT` change the host ports, so a second stack can run beside the first:

```bash
API_PORT=18000 DB_PORT=54339 WEB_PORT=13000 docker compose -p docvault-demo up --build -d
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

New bytes under a filename the vault already holds become the next version of that document. The names are compared without case, after the upload's name is cut to its last path part. When several stored documents have the name, the newest one's family gets the version. `?replaces=<id>` makes a file with any name the next version of that document's family. Bytes the vault already holds still return 200 with the existing document, whatever the name or `replaces`. An unknown `replaces` id is 404 `document_not_found`, and the upload is not stored.

```bash
curl -s -F file=@renewal-2027.pdf "$API/documents?replaces=$POLICY"
```

A version is a document of its own, with its own id, status, indexing, insights and summaries. Uploading one changes nothing stored for the earlier versions. Each document has `family_id`, shared by every version of one document, `version`, its number in the family from 1, and `latest_version`, the highest number in the family now.

`GET /documents` lists documents, newest first, 20 per page by default. Filter with `status`, and page with the `next_cursor` of the previous page. `latest=true` keeps only the highest version of each family, so a family is listed where its newest version was uploaded. The two filters combine: `latest=true&status=ready` leaves out a family whose newest version is still processing. Without `latest`, every version is listed.

```bash
curl -s "$API/documents?status=ready&limit=10"
curl -s "$API/documents?latest=true&limit=100"
```

`GET /documents/{id}/versions` lists every version of the document's family, oldest first, in `items`. An unknown id is 404 `document_not_found`.

```bash
curl -s $API/documents/$DOC/versions
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

`DELETE /documents/{id}` deletes the document, its pages, chunks, insights, summaries and file. Processing history and spend stay, and so do chats and comparisons, with their snapshots. Deleting one version leaves the family's other versions and their numbers: delete version 1 of 2 and the other is still version 2 of 2.

```bash
curl -s -X DELETE $API/documents/$DOC
```

`GET /events/documents` streams live document status as server-sent events, so a client sees every upload, stage change, retry and delete without polling. Each event is about one document whose `GET /documents/{id}` body may have changed:

- `document`, data: that body, read after the change committed. Uploading or deleting a version also sends one for each other version of its family, because their `latest_version` changed.
- `deleted`, data `{"id": "..."}`: the document is gone.
- `resync`, data `{}`: events may have been missed, so read the list again. The server sends it after its database listener reconnects, after a failed read, and to a client more than 1000 events behind.

The stream opens with the comment `: connected` and `retry: 1000`, and sends `: keepalive` after 15 s with nothing else to send. Past 100 open streams on one API process it answers 503 `too_many_streams` with `Retry-After: 5`.

```bash
curl -N $API/events/documents
```

### Chat

`POST /chats` starts a chat over one or more ready documents. The set is fixed for the chat's life. This holds for versions too, by design: a chat keeps the document ids it was started with, so after a new version arrives an old chat still answers from the version it was asked about. To ask the new version, start a chat on it.

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

`POST /chats/{id}/messages` asks a question of at most 2,000 characters. The answer cites each claim (`[1]` matches `citations[0]`), `interpreted_as` shows how a follow-up was read, and `usage` lists the model calls, tokens and cost of the turn. A question the documents do not answer returns `status: not_found` with a `reason`. `suggestions` holds up to three follow-up questions that the documents can answer. When the relevance gate refuses a question, they come from the documents' key facts ("What is the Policy Number?").

```bash
curl -s $API/chats/$CHAT/messages -H 'content-type: application/json' \
  -d '{"question": "Compare the general aggregate limit in the two documents."}'
```

`POST /chats/{id}/messages/stream` asks the same way and spends from the same rate-limit budget, but answers with server-sent events while the answer is written. A refusal found before the stream starts (404, 409, 422 or 429) is the usual status and error envelope. After a 200, the events are:

| Event | Data | Meaning |
|---|---|---|
| `stage` | `{"stage": "condensing" \| "searching" \| "answering"}` | A step starts. `condensing` comes only after the first turn. |
| `delta` | `{"text": "..."}` | Provisional answer text, with every `[S#]` marker removed. Append it. |
| `reset` | `{}` | Discard the provisional text. It comes before a retry or a repair that follows text already sent. |
| `turn` | the `POST /chats/{id}/messages` body | The grounded answer. It replaces the provisional text and ends the stream. |
| `error` | the error envelope | `ai_unavailable`, `ai_bad_output` or `internal_error`. It ends the stream. |

If the client disconnects, the answer still finishes and is stored, and `GET /chats/{id}/messages` shows it. A question the gate refuses sends no `delta`.

```bash
curl -N $API/chats/$CHAT/messages/stream -H 'content-type: application/json' \
  -d '{"question": "What is the property damage deductible?"}'
```

```text
event: stage
data: {"stage": "searching"}

event: stage
data: {"stage": "answering"}

event: delta
data: {"text": "Property"}

event: delta
data: {"text": " Damage"}

...

event: turn
data: {"id": "...", "status": "answered", "answer": "Property Damage Deductible $2,500 per occurrence [1]", ...}
```

`GET /chats/{id}/messages` returns the chat's turns, oldest first, with the same fields.

```bash
curl -s "$API/chats/$CHAT/messages?limit=50"
```

### Comparisons

`POST /comparisons` compares 2 to 5 ready documents field by field and returns 201 with the stored comparison. `focus`, at most 200 characters, narrows the fields ("deductibles"). Without it the model lines up the fields a broker compares: insured, insurer, period, limits, deductibles, premium, exclusions and endorsements. A comparison spends from the same per-client budget as asks.

```bash
CMP=$(curl -s $API/comparisons -H 'content-type: application/json' \
  -d "{\"document_ids\": [\"$POLICY\", \"$QUOTE\"]}" | jq -r .id)
```

`documents` are the columns, in request order, each with `deleted`, `pages_read`, `page_count` and `version`, the document's version number when the comparison was made. Two versions of one file can be compared like any two documents. A comparison stored before versions existed has `version: null`. Each of the `rows` has a `field`, one cell per document (`cells[i]` belongs to `documents[i]`) and `differs`. A filled cell has the `value` as the document writes it, its `page` and `page_label`, and a `quote` found on that page. An empty cell has all four null: the document does not state the field, or the model gave a value that could not be checked against its page. `dropped_cells` counts the second kind: a quote not on the cited page, a missing page or quote, or a cell naming no document or one already placed in its row.

```json
{"field": "Property Damage Deductible", "differs": true, "cells": [
  {"document_id": "...", "value": "$2,500 per occurrence", "page": 2, "page_label": "p. 2",
   "quote": "Property Damage Deductible $2,500 per occurrence"},
  {"document_id": "...", "value": "$5,000 per occurrence", "page": 2, "page_label": "p. 2",
   "quote": "Property Damage Deductible $5,000 per occurrence"}]}
```

The code decides `differs`, never the model. It is true when the filled cells hold two different values after case, spacing, thousands separators, zero cents and trailing punctuation are normalised, so `$1,000,000` equals `USD 1,000,000.00`. Currency symbols map to their codes and are compared apart from the figure: `$1,000,000` and `€1,000,000` differ, and a figure with no currency matches either. A leading sign or parenthesis is kept, so `-$500` and `$500` differ. It is also true when an empty cell sits beside a filled one. `usage` lists the model calls, tokens and cost.

Fewer than 2 or more than 5 ids, a repeated id, or a focus over 200 characters is 422 `invalid_request`. An unknown id is 404 `document_not_found`. A document that is not ready is 409 `documents_not_ready`, with the same `details.not_ready` as chats.

`GET /comparisons/{id}` returns a stored comparison. A document deleted since keeps its column, its cells and their page labels, with `deleted: true`. `GET /comparisons` lists comparisons newest first, with the same `next_cursor` paging as `GET /chats`.

```bash
curl -s $API/comparisons/$CMP
curl -s "$API/comparisons?limit=10"
```

### Summaries

`POST /documents/{id}/summaries` writes the document's summary again for another reader and returns it. `length` is `brief`, `standard` (the default) or `detailed`, a target of about 60, 150 or 300 words. `focus`, at most 200 characters, is the reader's interest ("deductibles"). `tone` is `plain` (the default), `formal`, or `client`, which writes for the insured client and avoids broker jargon. A summary spends from the same per-client budget as asks.

```bash
curl -s $API/documents/$POLICY/summaries -H 'content-type: application/json' \
  -d '{"length": "brief", "focus": "deductibles", "tone": "client"}'
```

The model reads the same page prefix that insights read. `text` is plain text, and `sources` holds up to 6 passages that support it, each with its `page`, `page_label` and a `quote` found on that page. A quote that is not on the page it cites is dropped and counted in `dropped_sources`. When none survives, the summary is still returned with `sources` empty, because a summary is not a fact claim. `focus` comes back normalised: trimmed, lowercased and with spaces collapsed.

```json
{"id": "...", "document_id": "...", "length": "brief", "focus": "deductibles", "tone": "client",
 "text": "...", "sources": [{"page": 2, "page_label": "p. 2", "quote": "Property Damage Deductible $2,500 per occurrence"}],
 "dropped_sources": 0, "model": "fake-chat", "prompt_version": "summary-v1", "cached": false, "created_at": "..."}
```

A new summary returns 201. Documents never change once stored, so each summary is kept, keyed by the document, the length, the normalised focus, the tone, the model and the prompt version. The same request again returns 200 with the stored summary and `cached: true`, and makes no model call. `cached: true` always means the response made no model call.

A document that is not ready is 409 `documents_not_ready`, with the same `details.not_ready` as chats. An unknown id is 404 `document_not_found`. A length or tone outside its values, or a focus over 200 characters, is 422 `invalid_request`. The model call is one attempt of at most 90 s with no repair turn, so a provider failure is 503 `ai_unavailable` and invalid output or an empty summary is 502 `ai_bad_output`, both with `retryable: true`. Nothing is stored for either.

`GET /documents/{id}/summaries` lists a document's summaries newest first, each with `cached: true`, with the same `next_cursor` paging as `GET /chats`. A summary is deleted with its document.

A summary belongs to one version, because each version's content differs. A new version starts with no summaries, and deleting a version deletes its summaries and no other version's.

```bash
curl -s "$API/documents/$POLICY/summaries?limit=20"
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
| 404 | `document_not_found`, `page_not_found`, `chat_not_found`, `comparison_not_found` |
| 409 | `documents_not_ready`, `documents_deleted`, `nothing_to_retry` |
| 413 | `file_too_large` |
| 415 | `unsupported_type` |
| 422 | `empty_file`, `invalid_request` (a parameter or body failed validation; `details.errors` names each field) |
| 429 | `rate_limited`, with `Retry-After` |
| 502 | `ai_bad_output` |
| 503 | `ai_unavailable`, with `Retry-After: 5` when `retryable` is true |

## Sample documents

`samples/` holds three fictitious documents for the same insured: a seven-page commercial general liability policy (PDF), a three-page renewal quote with changed figures (PDF), and a short endorsement (DOCX). `samples/facts.yaml` lists the facts planted in them, each with its document, page, and exact text. The tests and evals read that file as their expected values.

To regenerate the samples, run `uv run python scripts/make_samples.py`. The output is byte-identical on every run.

## Status

This section lists what works at the current commit.

Built:

- Compose stack: `api` on port 8000, `worker`, `beat`, `db` (pgvector 0.8.0 on Postgres 16), and `redis`.
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
- Document versions: new bytes under a stored filename, or sent with `replaces=<id>`, become the next version of that document's family (`family_id`, `version`, `latest_version`). A unique index on the family and number makes two concurrent uploads into one family take the next two numbers; the one that loses retries with the next number, and no error reaches the client. `GET /documents?latest=true` lists each family once and `GET /documents/{id}/versions` lists a family. In the web UI, the rail lists the latest version of each document with a "v2" mark, and follows live events as versions arrive and go. A document page says "Version 2 of 2" with links to the others. An earlier version opens with a note that links the newest. "Compare with version N" compares the two, "Versions (n)" lists the family, and "Upload a new version" sends a file of any name as the next version.
- Metrics: `GET /metrics/documents` and `GET /metrics/processing`, each figure read at request time from the one table that records it.
- Per-client rate limits on upload and ask.
- Evals: `python -m docvault.evals` scores retrieval, citations, refusals, insights, comparisons and summary sources separately on a fixed broker case set, and calibrates the relevance gate. Results are in `docs/evals.md`.
- A TLA+ model of the indexing lifecycle, checked by `formal/check.py` (`docs/lifecycle.md`).
- `scripts/demo.sh` and `scripts/scale_probe.py`.

Known limits:

- On the fake provider, the answer step often quotes a neighbouring passage: citation accuracy is 7 of 34 facts while retrieval finds 21 of 34. Broker-worded questions that share few words with the text are refused by the gate (`docs/evals.md` lists each).
- The fake gate threshold (0.184) has a margin of 0.003 on each side on the eval set. The OpenAI threshold (0.25) is uncalibrated; `docs/evals.md` gives the command that calibrates it once a key is set.
- The OpenAI backends are tested against canned HTTP responses only. They have not run against the live API. Streaming is tested against canned server-sent chunks only, and the comparison and summary calls against canned responses.
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
- The fake provider aligns fields only when two or more documents give them the same label. It misses the premium change in the samples ("Total Advance Premium" and "Total Annual Premium") and leaves out fields only one document has.
- A summary runs synchronously, like a comparison: one model call of at most 90 s (`SUMMARY_TIMEOUT_S`), with no gateway retry and no repair turn, so the API answers before the web proxy's 120 s timeout. The OpenAI client applies the 90 s to each phase of the HTTP call (connect, write, read), so it bounds a normal call rather than being one wall-clock deadline. This is read from the client's request options, not measured, and it holds for comparisons too.
- A stored summary is keyed by the document, the normalised options, the model and the prompt version. A change to the insights budget (`INSIGHTS_MAX_CHARS`) or the reasoning effort still serves summaries made under the old setting. Bump `SUMMARY_VERSION` with such a change.
- A cache hit still spends one of the client's per-minute asks, because the rate limit runs before the route.
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
- A chat keeps the version it was started with, but the chat screen names its files without their version numbers and does not say when a newer version exists.
- A delete racing an upload of the same bytes can delete the blob that the new row needs.
- An upload sent without Content-Length is read in full into a temporary file before its size is checked, so the limit bounds what is stored, not what a client can send.
