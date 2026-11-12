# Metrics

Two read-only endpoints report the vault's state and its recent processing. Every figure is computed when the request arrives, from the one table that records it, so there are no counters to drift.

## Sub-features

- `metrics-documents` (`GET /metrics/documents`) returns `total`, `by_status`, `by_kind`, insights states, `pages`, `chunks`, `bytes` and `failures_by_code`.
- `metrics-processing` (`GET /metrics/processing?window=24h`) returns, for the window:
  - `uploaded` and `indexed`;
  - p50/p95 for `time_to_ready_ms` and `queue_wait_ms`;
  - per-step runs, failures, retries and durations (`parse`, `embed`, `analyze`);
  - AI calls, tokens and cost by purpose and model;
  - chat outcomes and latency;
  - the `cache` block, which is not per window and is the one exception to "no counters". The cache Redis keeps its hits and misses since `counted_since` (see `caching.md`).

## How to get to it (user POV)

- `GET /metrics/documents`
- `GET /metrics/processing?window=1h|24h|7d`
- The web UI's metrics screen (see `web-ui.md`).

## Driving it with call.sh

Preconditions:

- The steps in `upload-and-indexing.md` and `chat-with-citations.md` have run on this stack: three samples ready, one textless PDF failed, and at least four chat turns.

- **Document state.** Run `$S/call.sh GET /metrics/documents`. `total` equals the number of documents in `GET /documents`. `by_status.failed` equals the failed rows there, and `failures_by_code.no_extractable_text` is at least 1.
- **Cross-check.** Run `$S/sql.sh "SELECT status, count(*) FROM documents GROUP BY 1"`. It matches `by_status`.
- **Processing.** Run `$S/call.sh GET '/metrics/processing?window=24h'`. `steps.parse.failed` is at least 1 with `no_extractable_text`. `ai.by_purpose_and_model` lists `embed_chunks`, `insights`, `embed_query` and `answer`. `chat.outcomes` counts at least one `answered` and one `not_found`.
- **Bad window.** Run `$S/call.sh GET '/metrics/processing?window=forever'`. You get `422` with `error.code` set to `invalid_request`.

## Gotchas

- `cost_usd` is 0 under `ai_mode: fake`. `unpriced_calls` counts calls to a model missing from `ai.PRICES`, so a non-zero value means a price is missing, not that the call was free.
- Percentiles are `null` when the window holds no runs of that step.
