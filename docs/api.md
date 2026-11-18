# API reference

Every error a route returns has one shape, `{"error": {"code", "message", "retryable", "details"}}`, except the 503 from `GET /health`, which carries the health report. OpenAPI declares that shape for the error codes each route names. `GET /metrics/documents` names none, `GET /health` declares its 503 as the health report, and no route declares the 500 `internal_error`. The examples use `API=http://localhost:8000`.

Every response carries an `X-Request-ID` header. A request may send its own id of up to 64 characters, each a letter, a digit, `.`, `_` or `-`. The API returns that id unchanged and replaces any other value with a fresh 32-character hex id. The web UI's API calls each send a fresh id when the browser can make one (`crypto.randomUUID`), and its `/api` proxy passes the header both ways. The live-status stream sends none, so the API makes its id.

```bash
curl -s -D - -o /dev/null -H 'X-Request-ID: docs-check-1' $API/health | grep -i x-request-id
```

The id follows the request through the logs. The API writes one line per request on the logger `docvault.requests` when the response ends, so a stream gets one line when it closes. The worker's lines for the tasks the request queued, and for the tasks those queued in turn, carry the same id. So does every `llm_calls` row that the request and its tasks write. A line from `docker compose logs api`:

```json
{"time": "2026-11-12T02:21:27.869+00:00", "level": "INFO", "logger": "docvault.requests", "message": "request", "request_id": "docs-check-1", "method": "GET", "route": "/health", "status": 200, "latency_ms": 29, "client": "192.168.65.1"}
```

`route` is the route template, such as `/documents/{doc_id}`, and is null when no route matched. Each indexing or insights step in the worker writes one line on `docvault.pipeline` with `document_id`, `step`, `attempt`, `outcome`, `error_code` and `duration_ms`.

## Documents

`POST /documents` uploads one file (PDF, DOCX or TXT, at most 50 MiB). It returns 202 with a queued document, or 200 with the existing document when the same bytes were uploaded before.

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

`GET /documents/{id}` returns one document. `status` is `queued`, `parsing`, `embedding`, `ready` or `failed`. `pages` is the stored page count once parsing has finished, in every status. A failed document has `error` and `failed_stage` (`parse` or `embed`). A file that cannot be parsed fails at `parse` with `unreadable_file`, `encrypted_pdf` (a PDF that needs a password to open), `no_extractable_text` (no page has text, as in a scanned PDF) or `too_many_pages` (more than 2,000 pages).

```bash
curl -s $API/documents/$DOC
```

`GET /documents/{id}/insights` returns `pending`, `ready`, `failed` or `unavailable`. Ready insights have a title, a type, a summary, tags, and key facts, each with its page and a quote found on that page.

Over the daily AI spending cap (`DAILY_AI_BUDGET_USD`), new insights fail at once with `quota_exceeded` and `retryable: false`, and the message "Insights were not written because today's AI spending cap of $5.00 is used up. Retry after midnight UTC." The document itself still reaches `ready`. The gateway never caps embedding calls, so uploads are indexed over the cap. After the cap resets, `POST /documents/{id}/retry` writes them.

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

## Chat

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

`POST /chats/{id}/messages` asks a question of at most 2,000 characters. The answer carries numbered citations (`[1]` matches `citations[0]`), `interpreted_as` shows how a follow-up was read, and `usage` lists the model calls, tokens and cost of the turn. A sentence whose quote was not found in its source stays in the answer without a number. A question the documents do not answer returns `status: not_found` with a `reason`. `suggestions` holds up to three follow-up questions that the documents can answer. When a question is refused with `no_evidence`, they come from the documents' key facts ("What is the Policy Number?").

```bash
curl -s $API/chats/$CHAT/messages -H 'content-type: application/json' \
  -d '{"question": "Compare the general aggregate limit in the two documents."}'
```

A chat's first question may come from the answer cache. When the same first question was answered before in another chat on the same documents, with the same models and retrieval settings, the response is a new turn with `cached: true`. It has the stored answer, citations and suggestions, `usage` all zero, and no model call behind it. The question is compared without case or extra spaces. Follow-ups are never cached, and neither are refusals. Every other turn has `cached: false`. The answer cache is off when `CACHE_URL` is unset.

Over the daily AI spending cap, an ask is 429 `quota_exceeded` before any model call (see Errors). An answer from the answer cache still comes back over the cap, because it makes no model call.

`POST /chats/{id}/messages/stream` asks the same way and spends from the same rate-limit budget, but answers with server-sent events while the answer is written. A refusal found before the stream starts (404, 409, 422 or 429, `quota_exceeded` included) is the usual status and error envelope. After a 200, the events are:

| Event | Data | Meaning |
|---|---|---|
| `stage` | `{"stage": "condensing" \| "searching" \| "answering"}` | A step starts. `condensing` comes only after the first turn. |
| `delta` | `{"text": "..."}` | Provisional answer text, with every `[S#]` marker removed. Append it. |
| `reset` | `{}` | Discard the provisional text. It comes before a retry or a repair that follows text already sent. |
| `turn` | the `POST /chats/{id}/messages` body | The grounded answer. It replaces the provisional text and ends the stream. |
| `error` | the error envelope | `ai_unavailable`, `ai_bad_output`, `internal_error`, or `quota_exceeded` when the spending cap is reached after the stream started. It ends the stream. |

If the client disconnects, the answer still finishes and is stored, and `GET /chats/{id}/messages` shows it. A `no_evidence` refusal sends no `delta`.

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

An answer from the answer cache sends no `stage`. It sends one `delta` with the whole answer, markers removed, and then the `turn`:

```text
event: delta
data: {"text": "Property Damage Deductible $2,500 per occurrence"}

event: turn
data: {"id": "...", "status": "answered", "answer": "Property Damage Deductible $2,500 per occurrence [1]", ..., "usage": {"calls": 0, ...}, "cached": true}
```

`GET /chats/{id}/messages` returns the chat's turns, oldest first, with the same fields, `cached` included. `limit` (50 by default, at most 200) keeps the newest turns, and there is no paging past them.

```bash
curl -s "$API/chats/$CHAT/messages?limit=50"
```

## Comparisons

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

The code decides `differs`, never the model. It is true when the filled cells hold two different values after case, spacing, thousands separators, zero cents and trailing sentence punctuation (`.`, `,`, `;`, `:`, `!`, `?`) are normalised, so `$1,000,000` equals `USD 1,000,000.00` and `$500.` equals `$500`. A comma counts as a thousands separator only inside whole groups of three digits, so `1,5` and `15` differ. Signs and other marks are kept, so `A+` and `A-` differ, and so do `-$500` and `$500` or `($500)` and `$500`. Currency symbols map to their codes and are compared apart from the figure, in the order they appear: `$1,000,000` and `€1,000,000` differ, `$500 or €450` and `€500 or $450` differ, and a figure with no currency matches either. It is also true when an empty cell sits beside a filled one. `usage` lists the model calls, tokens and cost.

Fewer than 2 or more than 5 ids, a repeated id, or a focus over 200 characters is 422 `invalid_request`. An unknown id is 404 `document_not_found`. A document that is not ready is 409 `documents_not_ready`, with the same `details.not_ready` as chats. Over the daily AI spending cap, a comparison is 429 `quota_exceeded`, and nothing is stored.

`GET /comparisons/{id}` returns a stored comparison. A document deleted since keeps its column, its cells and their page labels, with `deleted: true`. `GET /comparisons` lists comparisons newest first, with the same `next_cursor` paging as `GET /chats`.

```bash
curl -s $API/comparisons/$CMP
curl -s "$API/comparisons?limit=10"
```

## Summaries

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

A document that is not ready is 409 `documents_not_ready`, with the same `details.not_ready` as chats. An unknown id is 404 `document_not_found`. A length or tone outside its values, or a focus over 200 characters, is 422 `invalid_request`. The model call is one attempt of at most 90 s with no repair turn, so a provider failure is 503 `ai_unavailable` and invalid output or an empty summary is 502 `ai_bad_output`, both with `retryable: true`. Nothing is stored for either. Over the daily AI spending cap, a new summary is 429 `quota_exceeded` and nothing is stored. A stored summary still comes back over the cap, with `cached: true`, because it makes no model call.

`GET /documents/{id}/summaries` lists a document's summaries newest first, each with `cached: true`, with the same `next_cursor` paging as `GET /chats`. A summary is deleted with its document.

A summary belongs to one version, because each version's content differs. A new version starts with no summaries, and deleting a version deletes its summaries and no other version's.

```bash
curl -s "$API/documents/$POLICY/summaries?limit=20"
```

## Metrics and health

`GET /metrics/documents` counts documents by status and kind, insights by status, and totals pages, chunks, bytes and failures by error code.

```bash
curl -s $API/metrics/documents
```

`GET /metrics/processing?window=24h` reports what happened in the window (`1h` to `9999d`): per-step runs, failures by code, retries, crashes and p50/p95 durations, time to ready, queue wait, tokens and cost by purpose and model, and chat outcomes and latency.

```bash
curl -s "$API/metrics/processing?window=7d"
```

Its `cache` block counts the embedding cache per text embedded and the answer cache per first question in a chat. The counts run from `counted_since`, the first count since the cache Redis started, not from the window's start. `cache` is null when the cache is off or unreachable.

```json
"cache": {"embedding": {"hits": 1, "misses": 3}, "answer": {"hits": 2, "misses": 1}, "counted_since": "2026-11-12T02:00:55.418090Z"}
```

`GET /health` reports Postgres, the broker Redis, the AI mode, the embedding model, `max_upload_bytes`, the spending cap and the cache. `ai_mode` is `live`, `fake`, or `mixed` when only one of `CHAT_PROVIDER` and `EMBED_PROVIDER` resolves to the fake provider. It returns 503 when Postgres or the broker Redis is unreachable. `ai_budget` holds `limit_usd` and `spent_today_usd`, the ledger's spend since midnight UTC. It is null when no cap is set, or while Postgres is unreachable. `cache` is `ok`, `unreachable` or `off` (no `CACHE_URL`). An unreachable cache never makes `/health` 503, because the app answers without it.

```json
{"db": "ok", "redis": "ok", "ai_mode": "fake", "embed_model": "fake-hash-1536", "max_upload_bytes": 52428800,
 "ai_budget": {"limit_usd": 5.0, "spent_today_usd": 0.01}, "cache": "ok"}
```

```bash
curl -s $API/health
```

## Errors

| Status | Codes |
|---|---|
| 400 | `invalid_cursor` |
| 404 | `document_not_found`, `page_not_found`, `chat_not_found`, `comparison_not_found` |
| 409 | `documents_not_ready`, `documents_deleted`, `nothing_to_retry`, `version_conflict` |
| 413 | `file_too_large` |
| 415 | `unsupported_type` |
| 422 | `empty_file`, `invalid_request` (a parameter or body failed validation; `details.errors` names each field) |
| 429 | `rate_limited` and `quota_exceeded`, each with `Retry-After` |
| 500 | `internal_error` (an exception no route expected; `retryable: true`, with `X-Request-ID`) |
| 502 | `ai_bad_output` |
| 503 | `ai_unavailable`, with `Retry-After: 5` when `retryable` is true, and `too_many_streams`, with `Retry-After: 5` |

A path or a method that no route serves gets FastAPI's default body, not this shape: `{"detail": "Not Found"}` with 404, or `{"detail": "Method Not Allowed"}` with 405.

`quota_exceeded` means today's AI spend has reached `DAILY_AI_BUDGET_USD`. Asks, streamed asks, comparisons and new summaries get it before any model call, with `retryable: true` and `Retry-After` set to the seconds until midnight UTC, when the cap resets. `details` names the cap, the spend, the reset time and the same wait:

```json
{"error": {"code": "quota_exceeded", "message": "Today's AI spending cap of $5.00 is used up. It resets at midnight UTC.",
  "retryable": true, "details": {"limit_usd": 5.0, "spent_today_usd": 5.02,
  "resets_at": "2026-11-13T00:00:00+00:00", "retry_after_s": 79118}}}
```
