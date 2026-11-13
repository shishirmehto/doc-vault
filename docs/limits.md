# Known limits

The README lists the main ones. This is the full list, by area.

## Answers and retrieval

- On the fake provider, the answer step often quotes a neighbouring passage: citation accuracy is 7 of 34 facts while retrieval finds 21 of 34. Broker-worded questions that share few words with the text are refused by the gate (`docs/evals.md` lists each).
- The fake gate threshold (0.184) has a margin of 0.003 on each side on the eval set. The OpenAI threshold (0.20) sits below the lowest answerable turn of the calibration run, so the gate refuses only off-topic questions and the answer step declines the rest (`docs/evals.md`).
- The tests use canned HTTP responses for the OpenAI backends. One live run on 2026-11-12 covered the eval set and 19 public insurance documents, with streaming, suggestions, comparisons and summaries (`docs/evals.md`, `AI_USAGE.md`).
- The fake provider simulates a model's pacing when it streams: it sends its finished answer a word at a time, 25 ms apart in compose (`FAKE_STREAM_DELAY_MS`). The plain route has no delay.
- During a re-embed after an embedding-model change, a chat over that document refuses with `no_evidence`, and the embedding progress count reads as complete.
- After an embedding-model change, chats answer only from documents already re-embedded until reconcile finishes.
- Retrieval sends eight passages to the answer step. On the 246-page manual, the passage that answered one question was not among them.
- An answer is kept when at least one of its quotes is found. A sentence whose quote is not found stays in the answer text, without a marker.
- A citation is checked only for its quote being in the source it names, not for the quote supporting the sentence.

## Insights

- Insights read a page prefix that fits 240,000 characters. Longer documents report partial coverage; map-reduce over the rest is not built.
- A key fact is checked only for its quote being on the cited page, not for its value appearing in the quote.
- Insights rerun only when the prompt version changes, not when the model or the provider key changes.

## Comparisons

- "Differs" in a comparison compares the cells' text, not their meaning, so two wordings of the same term are marked as different.
- A comparison runs synchronously: one model call of at most 90 s (`COMPARE_TIMEOUT_S`), with no gateway retry and no repair turn, so the API answers before the web proxy's 120 s timeout. Output that fails validation is a retryable 502 `ai_bad_output`, and the UI offers Try again.
- Each compared document gets an even share of the 240,000-character insights budget, so five long documents are read for at most 48,000 characters each. `pages_read` says how far, and the UI says when a document was read only in part.
- A comparison cell is checked for its quote being on the cited page, not for its value appearing in the quote. `differs` compares the values, so it trusts the model's copy of each one.
- `differs` knows only the comma as a thousands separator and only `$`, `€`, `£`, `USD`, `EUR` and `GBP` as currencies, so `€1.000,00` and `€1,000.00`, or `$1,000` and `US$1,000`, show as a difference. This errs toward showing an equal row, never toward hiding a change.
- The fake provider aligns fields only when two or more documents give them the same label. It misses the premium change in the samples ("Total Advance Premium" and "Total Annual Premium") and leaves out fields only one document has.

## Summaries

- A summary runs synchronously, like a comparison: one model call of at most 90 s (`SUMMARY_TIMEOUT_S`), with no gateway retry and no repair turn, so the API answers before the web proxy's 120 s timeout. The OpenAI client applies the 90 s to each phase of the HTTP call (connect, write, read), so it bounds a normal call rather than being one wall-clock deadline. This is read from the client's request options, not measured, and it holds for comparisons too.
- A stored summary is keyed by the document, the normalised options, the model and the prompt version. A change to the insights budget (`INSIGHTS_MAX_CHARS`) or the reasoning effort still serves summaries made under the old setting. Bump `SUMMARY_VERSION` with such a change.
- The fake's summaries are passages of the page text: 2, 4 or 8 of them after a tone's fixed opening, so they run far under the word targets (12 to 39 words on the samples). They show that each option changes the output, not how well a model writes.
- In the web UI, a summary request belongs to its document page. A reader who leaves while it runs and asks again on return starts a second model call, which returns the summary the first one stored.

## Versions

- A version is linked by filename alone, so two unrelated files uploaded under one name, such as `scan.pdf`, become versions of one document. No route splits a family or moves a version to another, so the only fix is to delete the version and upload it under another name.
- Two concurrent first uploads of one new name both find no stored document with that name, so each starts its own family at version 1. A later upload of the name joins the newer of the two.
- Version numbers never shift, but the highest one can be used again: delete version 2 of 2, and the next upload becomes version 2 again. A gap left by deleting an earlier version is not filled.
- An upload with no filename is stored as `upload`, so every such upload joins one family.
- Bytes the vault already holds return the existing document even when `replaces` names another family. Nothing is linked, and the response's `family_id` says which family the bytes belong to.
- `GET /documents/{id}/versions` returns the whole family in one response, with no paging.
- In the web UI, the rail and both pickers list only the latest version of each document. An earlier version is opened from a link on its family's document pages. Its own page asks about it ("Ask about this document") and compares it ("Compare with version N", or "Compare with...", which ticks it in the compare picker).

## Uploads, chats and deletes

- A delete racing an upload of the same bytes can delete the blob that the new row needs.
- An upload sent without Content-Length is read in full into a temporary file before its size is checked, so the limit bounds what is stored, not what a client can send.
- Chats and comparisons cannot be deleted, and a summary is deleted only with its document.
- `GET /chats/{id}/messages` returns at most the newest 200 turns of a chat.
- A path or a method that no route serves returns FastAPI's default `{"detail": ...}` body, not the error shape.

## Rate limits and retries

- Rate limits are per API process and per client address. Behind a proxy, such as the web UI's, every user shares the proxy's address.
- Gateway retries have no jitter, and Celery's retry countdown (2 to 32 seconds) ignores the provider's Retry-After header.
- A stored summary or an answer from the answer cache still spends one of the client's per-minute asks, because the rate limit runs before the route.

## Spending cap

- The spending cap is checked before each call to a chat model, not reserved. Calls already running when the cap is reached finish and are billed. Each process reads today's spend at most every 10 s, and the API and each worker child are separate processes. So the day's spend can pass the cap.
- Embedding calls are never refused, but their cost counts toward the day's spend, so indexing over the cap keeps adding to it.
- A call to a model with no price entry stores no cost, so it never counts toward the cap.
- The day is the UTC day, so the cap resets at midnight UTC whatever the client's time zone. Stored failed insights say "Retry after midnight UTC", not a local time, and keep no reset time.

## Caches

- Only a chat's first question is cached. Follow-ups never are, because their answer depends on the turns before them. Refusals never are, so a refused question gets a fresh attempt.
- The answer cache matches a question's text without case or extra spaces, so a reworded question is a miss.
- The embedding cache keys the exact text, and a chunk's text starts with its document's filename. The same text uploaded under another filename is a miss.
- The cache counts run from `counted_since`, not from the metrics window. The cache keeps nothing on disk and may evict any key, the counters included, so a cache restart sets them back to zero with `counted_since` null until the next count.
- While the cache is unreachable, each process logs one `docvault.cache` warning a minute and skips the cache for that minute. After the cache comes back, a process that saw it fail still skips it until its minute ends, while `/health` already reads `ok`.

## Live status and the web UI

- Live status runs one database listener per API process. Each process reads a changed document once, whatever the number of streams it serves. A deployment with several API processes holds one listener per process and fans out the same way. Each process takes at most 100 streams.
- Postgres drops notifications while the listener is disconnected. After it reconnects, every stream gets `resync` and the web UI reads the list again.
- An open event stream never ends by itself, so stopping the API gives requests 5 s (`--timeout-graceful-shutdown 5` in compose) and then cancels them. Uvicorn logs that cancellation as an error.
- Over HTTP/1.1 a browser allows six connections to one host across all its tabs, and each visible tab of the web UI holds one for its stream. A hidden tab gives its stream back and reads the list again when shown.
- In the web UI, a document the rail does not list, opened by its link, is read again on each event that names it, so it updates but shows its loading state for a moment each time. That is a document older than the newest 100, or any earlier version, which also gets an event whenever a version of its family is uploaded or deleted. While the stream is down and nothing in the list is processing, changes from elsewhere show when the stream reconnects, at most 30 s later.
