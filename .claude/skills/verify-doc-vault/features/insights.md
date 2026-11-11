# Insights

Once a document is parsed, the vault writes AI insights for it: a title, a document type, a summary, tags, and key facts. Each key fact cites a page and quotes it word for word. A key fact whose quote is not on the cited page is dropped and counted. An insights failure never fails the document.

## Sub-features

- `insights-ready` returns `"status": "ready"` with title, document_type, summary, tags and key_facts.
- `insights-grounded` gives every key fact a `page` and a `quote` that appears on that page, plus a `page_url` that opens it.
- `insights-coverage` reports `coverage.pages_read` and `coverage.page_count` when the document is longer than the prompt budget.
- `insights-fail-isolated` leaves the document `ready` and searchable when insights fail.

## How to get to it (user POV)

- `GET /documents/{id}/insights`, which returns `pending`, `ready`, `failed` or `unavailable`.
- `insights_status` on `GET /documents/{id}`.
- The document view in the UI (see `web-ui.md`).

## Driving it with call.sh

Preconditions:

- `sample-cgl-policy.pdf` uploaded and `ready` (see `upload-and-indexing.md`).

- **Wait.** Run `$S/wait-doc.sh <id> insights`. It prints `insights_status=ready`.
- **Read.** Run `$S/call.sh GET /documents/<id>/insights`. Status `200`, `"status": "ready"`, and a non-empty `summary`, `tags` and `key_facts`.
- **Grounding.** For each key fact, run `$S/call.sh GET /documents/<id>/pages/<page>` and confirm the page text contains the `quote` (whitespace may differ). Record how many were checked and `dropped_key_facts`.
- **Ledger.** Run `$S/sql.sh "SELECT purpose, status, input_tokens, output_tokens, cost_usd FROM llm_calls WHERE document_id = '<id>' AND purpose = 'insights'"`. At least one row exists. The cost is 0 under `ai_mode: fake`.
- **Failure stays isolated.** The textless PDF from `upload-and-indexing.md` has no text to analyse, so its insights read `unavailable` with a reason, not `ready`. A ready document never moves to `failed` because of insights.

## Gotchas

- Under the fake provider, the summary and key facts come from the fake chat backend: grounded and shaped right, but not real analysis. Say so in the proof.
- Insights run beside embedding, so `insights_status` can be `ready` before `status` is.
- `GET /documents/{id}/insights` on a document that is still queued returns `pending`, not 404.
