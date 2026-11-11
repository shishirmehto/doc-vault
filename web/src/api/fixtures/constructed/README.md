These fixtures were not captured: no request could reach these states.
Each follows the wire shape in `docvault/wire.py` and the error envelope.

- `insights-failed.json` and `error-503-ai-unavailable.json` (pass 1). Their messages are copied from `docvault/ai.py`.
- `turn-answered-zero-citations.json` (pass 2). The captured `turn-answered-one-citation.json` with its citations
  removed. The backend never sends this (an answered turn always has a citation), but the parser must not throw on it.
- `turn-not-found-unverified.json` (pass 2). The same turn as `not_found` with reason `unverified` and the
  `UNVERIFIED_TEXT` message from `docvault/chat.py`. The fake provider always quotes its source word for word, so
  its evidence is never rejected.

Everything in the parent folder was captured from the running API on 2026-11-11, including the failed
document (a generated textless PDF) and the queued, parsing and embedding states of a generated text file.
The chat fixtures (`chat-*`, `turn-*`, `history-*`, `ask-*`) come from the backend's fake provider at `453d327`.
