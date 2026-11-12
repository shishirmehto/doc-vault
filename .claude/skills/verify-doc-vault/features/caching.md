# Caches

With `CACHE_URL` set, two caches in the `cache` Redis save model calls. Compose sets it on the `api` and `worker`, so a stack from `launch.sh` has both caches on. The embedding cache sends the provider only texts that no earlier call embedded with the same model. The answer cache returns a chat's first question, answered before on the same documents, as a new turn marked `cached` with no model call. Neither cache is a dependency: with the cache down, the app runs as before, without the savings.

This recipe drives the API only.

## Sub-features

- `cache-health`: `GET /health` has `"cache": "ok"`.
- `cache-answer`: the same first question in a new chat on the same document, in another case and spacing, returns 200 with `cached: true`, the first turn's answer, citations and suggestions, `usage.calls` 0, and no new `llm_calls` row.
- `cache-answer-stream`: the same first question on the stream route sends exactly one `delta`, with no `[n]` marker, then `turn` with `cached: true`. It sends no `stage`.
- `cache-answer-skip`: a follow-up, a refused first question and a first question on another document set each come back `cached: false`.
- `cache-embedding`: a new version under the same filename, whose chunk text is unchanged, is an embedding hit and writes no `embed_chunks` row. The same text under another filename is a miss.
- `cache-metrics`: `GET /metrics/processing` has a `cache` block with `embedding` and `answer` hits and misses and `counted_since`.
- `cache-unreachable`: with the `cache` service stopped, `/health` returns 200 with `"cache": "unreachable"`, the metrics `cache` block is null, uploads reach `ready`, and each process logs one `docvault.cache` warning a minute.

## How to get to it (user POV)

- `$S/call.sh GET /health` and `$S/call.sh GET '/metrics/processing?window=1h'`.
- `$S/call.sh POST /chats -H "$H" -d '{"document_ids": ["<id>"]}'`, then `$S/call.sh POST /chats/<chat id>/messages -H "$H" -d '{"question": "..."}'`.
- `curl -sN $API/chats/<chat id>/messages/stream -H "$H" -d '{"question": "..."}'`, with `API=http://localhost:18000`.
- The outage: `docker compose -p docvault-verify --project-directory . stop cache`, and `start cache` to end it.

## Driving it

Preconditions: a stack from `launch.sh`. `H='content-type: application/json'`. Every embedding and every first question on the stack moves the counters, so read `GET /metrics/processing?window=1h` before each step and assert the change, not the total.

Make a throwaway text file named with a run tag, `cache-<tag>.txt`, so a rerun is not a duplicate of an earlier run's bytes or family:

```text
Named Insured:
Cache Check LLC

Policy Number:
CACHE-<tag>

Property Damage Deductible:
$2,500 per occurrence
```

- **Health.** `$S/call.sh GET /health` has `"cache": "ok"`.
- **Upload.** Upload the file and wait for it (`$S/wait-doc.sh <id>`, then `$S/wait-doc.sh <id> insights`).
- **First answer.** Start chat A on the document and ask `What is the property damage deductible?`. 200, `status: answered`, `cached: false`, `usage.calls` at least 1. The answer quotes `$2,500 per occurrence` with one citation on `section 1`. `answer.misses` goes up by 1.
- **Cached answer.** Start chat B on the same document and ask `  what is the PROPERTY damage deductible? `. 200, `cached: true`, the same `answer`, `citations` and `suggestions` as chat A's turn, `usage` all 0, and `interpreted_as` equal to its `question`. `$S/sql.sh "SELECT count(*) FROM llm_calls WHERE turn_id = '<chat B turn id>'"` reads 0, and `answer.hits` goes up by 1.
- **Stream.** Start chat C on the same document and stream `What is the property damage deductible?` with `curl -sN -D -`. Status 200. The body is exactly one `delta` whose text is chat A's answer without ` [1]`, then one `turn` with `cached: true` and `usage.calls` 0. No `stage` event comes.
- **History.** `$S/call.sh GET /chats/<chat B>/messages` shows the turn with `cached: true`. `$S/sql.sh "SELECT cached FROM chat_turns WHERE chat_id = '<chat B>'"` reads `t`.
- **Follow-up.** In chat B, ask `What is the policy number?`. `cached: false`. Ask the same follow-up in chat C: `cached: false` again, because a follow-up is never looked up. The `answer` counts do not move for either.
- **Refusal.** Start chat D on the document and ask `Who won the 2022 football World Cup?`. `status: not_found`, `reason: no_evidence`, `cached: false`. Start chat E and ask it again: `cached: false`, because a refusal is never stored. `answer.misses` goes up by 1 each time.
- **Other documents.** Upload a second throwaway file with another tag, wait for it, then start a chat on both documents and ask `What is the property damage deductible?`. `cached: false`: the document set is part of the key.
- **Embedding hit.** Upload a file with the same name, `cache-<tag>.txt`, whose bytes add two blank lines at the end. It becomes version 2. Once it is `ready`, `embedding.hits` has gone up by 1, and `$S/sql.sh "SELECT count(*) FROM llm_calls WHERE purpose = 'embed_chunks' AND document_id = '<v2 id>'"` reads 0.
- **Embedding miss.** Upload the original text with one blank line added at the end, under another name, `cache-<tag>-copy.txt`. Its bytes are new, so the upload is not deduplicated, and its chunk text is the same as version 1's. `embedding.misses` goes up by 1, and the new document has one `embed_chunks` row, because a chunk is embedded with its filename in front.
- **Outage.** Run `docker compose -p docvault-verify --project-directory . stop cache`. `GET /health` returns 200 with `"cache": "unreachable"`. `GET /metrics/processing?window=1h` has `cache: null`. Upload another throwaway file: it reaches `ready`. Ask a first question in a new chat: it answers with `cached: false`. Read the warnings with `docker compose -p docvault-verify --project-directory . logs --no-log-prefix api worker | grep docvault.cache`. Each process that touched the cache logged one line, "cache unreachable; skipping the cache for 60 s", and no more within that minute.
- **Back.** Run `docker compose -p docvault-verify --project-directory . start cache`. `GET /health` reads `"cache": "ok"` at once. Wait 61 s for each process's skip window to end. `GET /metrics/processing?window=1h` then has `cache` with every count 0 and `counted_since` null, because the cache keeps nothing on disk. The next ask sets `counted_since`, and no new warning is logged.

## Gotchas

- `doctor.sh` checks that `cache` is running, so it fails while the outage step has the cache stopped. Run it before the outage or after **Back**.
- After the cache is started again, a process that saw it fail still skips it until its 60 s window ends, while `/health` already reads `ok`. Assert hits only after waiting that out.
- Each worker child is its own process with its own skip window. So an outage can log up to one warning a minute from the API and from each of the four children that touched the cache.
- The answer cache serves any first question this stack has answered before on the same documents. Other recipes that expect stages and deltas on a first question (`chat-with-citations.md`) need a question or a document set this stack has not seen.
- The counters count across the API and the worker since `counted_since`, not per window. The `window` parameter does not change them.
- A question text embedded before is an embedding hit, so a first answer's `usage.calls` can be 1 (the answer call alone) rather than 2.
