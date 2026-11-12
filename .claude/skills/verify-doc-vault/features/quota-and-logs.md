# Spending cap and request logs

Two features for the person who runs the service. Every request gets an id, which the API returns in `X-Request-ID` and writes on its log line, the worker's lines for the tasks the request queued, and the request's `llm_calls` rows. A daily AI spending cap, `DAILY_AI_BUDGET_USD`, refuses model calls for the rest of the UTC day once the ledger's spend reaches it, while uploads keep indexing.

This recipe drives the API, the logs, the web proxy's header passing and the UI over the cap.

## Sub-features

- `log-request-id`: a safe `X-Request-ID` (up to 64 characters of letters, digits, `.`, `_` and `-`) comes back unchanged. An unsafe one, such as `bad id/with space`, and a missing one each come back as a fresh 32-character hex id.
- `log-request-line`: each request writes one `docvault.requests` line with `request_id`, `method`, `route` (the template), `status`, `latency_ms` and `client`. A streamed ask and `GET /events/documents` each write one line, when the stream ends.
- `log-trace`: an upload's id is on its API line, on five worker lines, and on its `embed_chunks` and `insights` rows in `llm_calls`.
- `log-web-proxy`: through the web's `/api` rewrite, the `X-Request-ID` sent comes back unchanged.
- `quota-health`: `GET /health` has `ai_budget` with `limit_usd` and `spent_today_usd`.
- `quota-refuse`: over the cap, an ask, a streamed ask (before any event), a comparison and a new summary each return 429 `quota_exceeded`, with `Retry-After` and the `details` `limit_usd`, `spent_today_usd`, `resets_at` and `retry_after_s`. None of them writes an `llm_calls` row.
- `quota-cached`: over the cap, a first question answered before still comes back from the answer cache with `cached: true`. A first question not answered before gets 429.
- `quota-insights`: over the cap, a new upload reaches `ready`, and its insights fail with `quota_exceeded` and `retryable: false`.
- `ui-quota`: over the cap, the UI's Ask (a follow-up in an existing chat), Compare and "Rewrite the summary" each say "Today's AI spending cap of $0.000001 is used up. Asking, comparing and rewriting start again at <time> on <day>, when it resets.", in the reader's time zone, with no Try again button. The ask keeps "Edit the question". The page of the document whose insights hit the cap shows the API's sentence under "The summary failed", with "Retry anyway".
- `ui-spending-cap`: Metrics has a Spending cap section: the cap per day (UTC), spent today, left and the reset. Over the cap, a red line above the table starts "The cap is used up."

## How to get to it (user POV)

- `curl -s -D - -H 'X-Request-ID: <id>' http://localhost:18000/<route>`, or `$S/call.sh` with `-H 'X-Request-ID: <id>'`.
- `docker compose -p docvault-verify --project-directory . logs --no-log-prefix api | grep <id>`, and the same for `worker`.
- `$S/sql.sh "SELECT purpose, request_id FROM llm_calls WHERE request_id = '<id>' ORDER BY id"`.
- `DAILY_AI_BUDGET_USD=0.000001 $S/launch.sh` launches a capped stack. `$S/call.sh GET /health` shows the cap.
- In the browser at `http://localhost:13000`: the chat's question box, "Compare with..." on a document page, "Rewrite the summary" under a ready document's summary, and the "Metrics" link in the header. Run `scripts/visible.js` on each of these screens over the cap at 1440x900, 1024x768 and 375x812.

## The one write

The fake provider costs $0, so no route can bring the ledger's spend up to a cap. This recipe therefore inserts one priced `llm_calls` row. It is the one exception to the rule "never insert rows" in `SKILL.md` and `features/README.md`, and it has three conditions:

- Run it only on the isolated `docvault-verify` stack that this run launched with `DAILY_AI_BUDGET_USD=0.000001 $S/launch.sh`. Never run it on the developer's stack.
- `sql.sh` is read-only, so the write goes through `psql` in the stack's `db` service, from the repo root:

  ```bash
  docker compose -p docvault-verify --project-directory . exec -T db psql -U vault -d vault -c "INSERT INTO llm_calls (purpose, provider, model, document_id, cost_usd, latency_ms, ok) VALUES ('answer', 'fake', 'seeded-cap-proof', '<a document id>', 0.01, 1, true)"
  ```

- Run `cleanup.sh` when the recipe ends. It removes the stack's volumes, and the row goes with them.

The row adds $0.01 to today's spend, against a cap of $0.000001. Each process reads today's spend at most once every 10 s, so wait 11 s after the insert before any step that expects the cap.

## Driving it

Preconditions: no `docvault-verify` stack is up. Launch the capped stack with the web UI, because `log-web-proxy` needs it:

```bash
DAILY_AI_BUDGET_USD=0.000001 $S/launch.sh
R=${EVIDENCE_ROOT:-/tmp/doc-vault-verify}/$(cat "${EVIDENCE_ROOT:-/tmp/doc-vault-verify}/current")
H='content-type: application/json'
L() { docker compose -p docvault-verify --project-directory . logs --no-log-prefix "$@"; }
```

`call.sh` saves the body only. To keep a response's headers, add `-D "$R/<name>-headers.txt"`. The path must be absolute, because `call.sh` runs curl from the repo root.

Make three throwaway text files with a run tag in their names and text, A, B and C, each like this one with its own policy number:

```text
Named Insured:
Quota Check LLC

Policy Number:
QUOTA-<tag>-A

Property Damage Deductible:
$2,500 per occurrence
```

Under the cap:

- **Health.** In `00-health.json`, `ai_budget.limit_usd` is 0.000001 (JSON may print it as `1e-06`), `ai_budget.spent_today_usd` is 0, and `cache` is `"ok"`.
- **Ids.** `curl -s -D - -o /dev/null -H 'X-Request-ID: verify.ID-1_2' http://localhost:18000/health` shows `x-request-id: verify.ID-1_2`. The same with `-H 'X-Request-ID: bad id/with space'`, and with no header, each shows a fresh 32-character hex id.
- **Trace.** `$S/call.sh POST /documents -H 'X-Request-ID: trace-<tag>' -F file=@<file A>`: 202. Wait for `ready` and for insights. `L api | grep trace-<tag>` shows one line: `docvault.requests`, `"method": "POST"`, `"route": "/documents"`, `"status": 202`. `L worker | grep trace-<tag>` shows five lines, in this order: the `docvault.pipeline` `step` line for `parse` with `"outcome": "ok"`, the same for `embed`, the `celery.app.trace` line "Task docvault.tasks.index_document[...] succeeded", the `step` line for `analyze`, and "Task docvault.tasks.analyze_document[...] succeeded". `$S/sql.sh "SELECT purpose, request_id FROM llm_calls WHERE request_id = 'trace-<tag>' ORDER BY id"` shows `embed_chunks` and `insights`.
- **Second document.** Upload file B and wait for `ready`.
- **Ask.** Start a chat on A and ask `What is the property damage deductible?` with `-H 'X-Request-ID: ask-<tag>'`: 200, `status: answered`. Its `llm_calls` rows carry `ask-<tag>`. This answer also fills the answer cache for `quota-cached`.
- **Streamed ask.** In the same chat, stream the follow-up `What is the policy number?` with `curl -sN -H 'X-Request-ID: stream-<tag>'`. After the `turn` event, `L api | grep stream-<tag>` shows exactly one line, with `"route": "/chats/{chat_id}/messages/stream"` and `"status": 200`. The turn's `llm_calls` rows carry `stream-<tag>`.
- **Event stream.** `curl -sN --max-time 3 -H 'X-Request-ID: events-<tag>' http://localhost:18000/events/documents` exits after 3 s (exit code 28). Once the stream has ended, `L api | grep events-<tag>` shows exactly one line, with `"route": "/events/documents"` and `"status": 200`.
- **Web proxy.** `curl -s -D - -o /dev/null -H 'X-Request-ID: proxy-<tag>' http://localhost:13000/api/health` shows `x-request-id: proxy-<tag>`.

Over the cap:

- **Seed.** Insert the priced row with A's id, as **The one write** shows, then wait 11 s.
- **Health.** In `GET /health`, `ai_budget.limit_usd` is 0.000001 and `ai_budget.spent_today_usd` is 0.01.
- **Ask.** In the chat on A, ask `What is the named insured?` with `-H 'X-Request-ID: capped-ask-<tag>'` and `-D "$R/capped-ask-headers.txt"`. 429, `error.code` is `quota_exceeded`, `retryable` is true, and the message reads "Today's AI spending cap of $0.000001 is used up. It resets at midnight UTC." `details` has `limit_usd` 0.000001, `spent_today_usd` 0.01, `resets_at` at the next midnight UTC, and `retry_after_s`, which equals the `Retry-After` header. `$S/sql.sh "SELECT count(*) FROM llm_calls WHERE request_id = 'capped-ask-<tag>'"` reads 0.
- **Streamed ask.** `curl -s -D - .../chats/<chat id>/messages/stream` with the same question: 429 with `content-type: application/json` and the same envelope. No `event:` line comes.
- **Comparison.** `POST /comparisons` with A and B: 429 `quota_exceeded`. `GET /comparisons` lists no new comparison.
- **Summary.** `POST /documents/<A>/summaries` with `{}`: 429 `quota_exceeded`.
- **Cached.** Start a new chat on A and ask `  WHAT is the property damage deductible? `: 200, `cached: true`, `usage.calls` 0. Start another chat on A and ask `What is the policy number?`: 429 `quota_exceeded`, because no chat asked it as a first question.
- **Insights.** `$S/call.sh POST /documents -H 'X-Request-ID: capped-<tag>' -F file=@<file C>`: 202. `$S/wait-doc.sh <C>` exits 0 (`ready`), and `$S/wait-doc.sh <C> insights` exits 1 (`failed`). `GET /documents/<C>/insights` has `status: failed` and an `error` with `code` `quota_exceeded`, `retryable` false, and the message "Insights were not written because today's AI spending cap of $0.000001 is used up. Retry after midnight UTC." `L worker | grep capped-<tag>` shows the `analyze` step line with `"outcome": "failed"` and `"error_code": "quota_exceeded"`. The ledger for `capped-<tag>` has one `embed_chunks` row and no `insights` row.
- Run `$S/cleanup.sh`. Every later recipe on this stack would get 429.

## Gotchas

- `rate_limited` and `quota_exceeded` share the 429 status. Assert `error.code` on every refusal. Asks, comparisons and summaries share 20 asks a minute per client, so pace this recipe or a step gets `rate_limited`.
- The cap counts from midnight UTC. A run that crosses midnight UTC loses the seeded spend, because the row then belongs to yesterday.
- The API and each worker child read the spend on their own, at most once every 10 s. Wait 11 s after the insert before the insights step too.
- The answer cache is on in this stack. A first question that an earlier step answered on the same documents comes back cached, with no `llm_calls` row, even over the cap.
- `--no-log-prefix` drops the service name from each line, so read the `api` and `worker` logs with separate commands.
