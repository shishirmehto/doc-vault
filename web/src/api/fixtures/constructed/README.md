These fixtures were not captured: no request could reach these states.
Each follows the wire shape in `docvault/wire.py` and the error envelope.

- `insights-failed.json` and `error-503-ai-unavailable.json` (pass 1). Their messages are copied from `docvault/ai.py`.
- `turn-answered-zero-citations.json` (pass 2). The captured `turn-answered-one-citation.json` with its citations
  removed. The backend never sends this (an answered turn always has a citation), but the parser must not throw on it.
- `turn-answered-markdown.json`. An answered turn written as the Markdown list the answer prompt allows since `answer-v3`: a lead
  sentence, then three items, two with a bold figure, each ending in its marker. Built from the captured
  `turn-answered-two-documents.json`: its renewal-quote citation is kept, and two more cite the quote's
  `quote-deductible` and `quote-new-exclusion` passages from `samples/facts.yaml`. The page-3 chunk id, the turn
  id and the usage figures are made up.
- `turn-not-found-unverified.json` (pass 2). The same turn as `not_found` with reason `unverified` and the
  `UNVERIFIED_TEXT` message from `docvault/chat.py`. The fake provider always quotes its source word for word, so
  its evidence is never rejected.

Everything in the parent folder was captured from the running API on 2026-11-11, including the failed
document (a generated textless PDF) and the queued, parsing and embedding states of a generated text file.
The chat fixtures (`chat-*`, `turn-*`, `history-*`, `ask-*`) come from the backend's fake provider at `453d327`.

At phase 3 (`ead25ad`), a capture script run against the live API recaptured the fixtures whose wire changed (health,
the failed document, the document list, both 422s, the 409 and the started chat) and captured the new ones: the chat
read back with a deleted document, its `documents_deleted` 409, the recent chats, a 429 from 31 duplicate uploads of
`ui-textless.pdf`, and both metrics routes, including an empty one-hour window.

After `TurnOut` gained `suggestions`, a capture script asked the same questions about the same
documents again and recaptured `turn-*` and `history-policy-four-turns.json`. It also captured
`ask-stream-deductible.txt`, the raw body of a streamed ask. `turn-answered-zero-citations.json` and
`turn-not-found-unverified.json` were rebuilt from the new `turn-answered-one-citation.json` as described above.

After `DocumentOut` gained `family_id`, `version` and `latest_version` and a compared document gained
`version`, a script added those fields to every captured body that holds a document or a compared document, as the
API returns them: each stored document is its own family at version 1 of 1, and each stored comparison
predates versions, so its compared documents read `version: null`. The same script captured the new ones from the
running API: `upload-202-version-2.json` (new bytes under a stored name), `document-version-1-of-2.json`,
`versions-two.json`, `versions-404-not-found.json` and `upload-404-replaces-unknown.json`. It deleted both documents
it uploaded afterwards.

When `TurnOut` gained `cached` and `ProcessingMetricsOut` gained `cache`, a script added `"cached": false` to
every stored turn (`turn-*`, `history-policy-four-turns.json`, the turn event of `ask-stream-deductible.txt` and the two
constructed turns), which is what the API returns for a turn answered by the model, and `"cache": null` to
`metrics-processing-1h.json`, captured when there was no cache. The fixtures new with that change were captured at `b5c649c`:
the cached turn, its history and its stream, `health.json` and `metrics-processing-24h.json` from the dev API, and the
`*-429-quota-exceeded.json` bodies, the capped health, metrics and insights from a stack with
`DAILY_AI_BUDGET_USD=0.000001` and one priced ledger row. These were built by hand:

- `ask-stream-error-quota-exceeded.txt`. The cap reached in the race between the check before the stream and the
  answer call: the `stage` events of a first question, then `error` with the captured 429 envelope, serialised as
  `error_out` in `docvault/api.py` writes it (compact, `1e-6`).
- `error-500-internal.json`. The envelope `internal_error_handler` in `docvault/api.py` sends for an exception no route
  expected, with the request id it adds. No request on a healthy stack reaches it.
- `health-cache-off.json` and `health-cache-unreachable.json`. The captured `health.json` with `cache` set to `off` (no
  `CACHE_URL`) and to `unreachable`, which a stopped cache gave on an isolated stack.
- `metrics-processing-24h-cache-off.json`. The captured `metrics-processing-24h.json` with `cache: null`, which the same
  stopped cache gave on `/metrics/processing`, and which an unset `CACHE_URL` gives too.
