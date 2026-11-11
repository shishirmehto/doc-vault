# doc-vault verification map

This folder is the maintained source for verifying what a doc-vault user sees and does. Read this index first, then use the matching feature file as the recipe.

## Baseline preconditions

- A stack started by `scripts/launch.sh` (project `docvault-verify`, API `http://localhost:18000`, web `http://localhost:13000`), with empty volumes.
- `scripts/doctor.sh` exits 0.
- `00-health.json` in the run folder shows `"ai_mode": "fake"`, unless the run set `VERIFY_LIVE=1` on purpose.
- Run every command from the repo root. `S=.claude/skills/verify-doc-vault/scripts`.
- Never drive a stack this run did not start.

## Driving conventions

- Use only public routes (`/documents`, `/chats`, `/metrics`, `/health`) and the UI. Never call Python, insert rows, or edit the database to reach a state.
- Take expected values from `samples/facts.yaml` (document, page, exact text). Never take them from an earlier response.
- In the browser, find elements by role and accessible name, not position.
- A fresh upload is a fresh hash. To watch processing again, upload a new file. Do not delete a sample just to re-upload it.

## Proof and skip reporting

- Capture the action and the state after it: the request, the response, and a second read-only view (a GET, `sql.sh`, or the UI).
- Name the feature ID and the entry point next to every artifact.
- If an entry point cannot be reached, report the command you tried and the precondition that was not met. Never report it as verified through another entry point.

## Features

| File | Feature IDs |
|---|---|
| [upload-and-indexing.md](upload-and-indexing.md) | `upload-new`, `upload-dedupe`, `upload-reject`, `index-ready`, `index-fail-retry`, `page-read` |
| [insights.md](insights.md) | `insights-ready`, `insights-grounded`, `insights-coverage`, `insights-fail-isolated` |
| [chat-with-citations.md](chat-with-citations.md) | `chat-start`, `chat-not-ready`, `chat-cited-answer`, `chat-follow-up`, `chat-multi-doc`, `chat-not-found`, `chat-history` |
| [metrics.md](metrics.md) | `metrics-documents`, `metrics-processing` |
| [web-ui.md](web-ui.md) | `ui-upload`, `ui-status`, `ui-insights`, `ui-chat`, `ui-citation-open`, `ui-metrics` |
