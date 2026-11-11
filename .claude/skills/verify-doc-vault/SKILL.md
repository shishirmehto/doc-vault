---
name: verify-doc-vault
description: Launch doc-vault (FastAPI, Celery, Postgres with pgvector, Next.js UI) as an isolated docker compose stack and prove a feature works the way a user meets it, through the HTTP API or the web UI, with saved evidence. Use after any change to upload, indexing, insights, chat, metrics or the UI, before you say it works.
---

# Verify doc-vault

doc-vault has two surfaces. The HTTP API on the `api` service is the primary one, and the case study grades it. The Next.js UI on the `web` service calls that API through its `/api/*` rewrite. Tests in `tests/` prove the modules work. This skill proves the running product works.

Every helper is in `.claude/skills/verify-doc-vault/scripts/`. Run them from the repo root. They read `scripts/env.sh`, where each setting can be overridden from the shell.

## Launch

```bash
.claude/skills/verify-doc-vault/scripts/launch.sh            # API, worker, beat, db, redis and web
.claude/skills/verify-doc-vault/scripts/launch.sh api worker beat   # API only, skips the UI build
```

`launch.sh` starts its own compose project, `docvault-verify`. It uses API port 18000, DB port 54339 and web port 13000, and its volumes are new and empty. So it never touches the developer's stack on 8000/54329/3000 or that stack's documents. It stops with an error in three cases: the project is already up, one of its ports is taken, or `.env` holds an OpenAI key and `VERIFY_LIVE=1` is not set. A key means paid calls; without one, the stack runs on the deterministic fake provider. The stack is ready when `launch.sh` prints `ready:` and the `/health` JSON. A cold build takes about two minutes.

Two instances can run side by side when each sets its own `VERIFY_PROJECT`, `VERIFY_API_PORT`, `VERIFY_DB_PORT` and `VERIFY_WEB_PORT`. Never drive the developer's own stack (project `doc-vault`, port 8000) with this skill.

## Doctor

```bash
.claude/skills/verify-doc-vault/scripts/doctor.sh
```

Read-only. It checks five things:
- every service in the project is running;
- port 18000 belongs to this project's `api`;
- `/health` returns 200 with `db` and `redis` both `ok`;
- the worker has logged `ready.`;
- whether the api image is older than the last commit or than uncommitted changes in `docvault/`.

Run it first whenever a result looks wrong. A stale image means the stack runs old code: run `cleanup.sh`, then `launch.sh`.

## Drive

Use the real user path: the public HTTP routes, or the UI in a browser. Never call Python functions, insert rows, or use test-only routes to make a feature "work".

API helpers. Each one saves what it sees in the run's evidence folder.

```bash
S=.claude/skills/verify-doc-vault/scripts
$S/call.sh POST /documents -F file=@samples/sample-cgl-policy.pdf     # prints the status code, then the body
$S/wait-doc.sh <doc_id>                  # polls GET /documents/<id> until status is ready (0) or failed (1)
$S/wait-doc.sh <doc_id> insights         # the same for insights_status
$S/call.sh POST /chats -H 'content-type: application/json' -d '{"document_ids": ["<doc_id>"]}'
$S/call.sh POST /chats/<chat_id>/messages -H 'content-type: application/json' -d '{"question": "What is the deductible?"}'
$S/sql.sh "SELECT purpose, count(*) FROM llm_calls GROUP BY 1"      # side effects, in a read-only transaction
```

Expected values come from `samples/facts.yaml`: for each planted fact, its document, page and exact text. Assert against those, not against what the API happened to return last time.

Drive the UI at `http://localhost:13000` with the Playwright MCP tools (`mcp__plugin_playwright_playwright__*`), or with the built-in browser if you can see it. Use the accessible names and roles listed in each feature file, not coordinates.

The feature map in `features/` lists every user-facing feature, how to reach it, how to drive it and what end state proves it. A proof is complete only when it drives every entry point that the feature's file lists.

## Evidence

Every helper writes to `$EVIDENCE_ROOT/$RUN_ID`, which is `/tmp/doc-vault-verify/<timestamp>` by default. `launch.sh` prints the path.

| File | What it holds |
|---|---|
| `build.txt` | The commit and uncommitted files the stack was built from |
| `00-health.json` | Health at launch, including `ai_mode` |
| `NN-<method>-<path>.json` | One file per `call.sh`, numbered in order |
| `transcript.log` | Time, method, path and status code for each `call.sh` |
| `doc-<id>-<field>-<state>.json` | The final document JSON from `wait-doc.sh` |
| `sql-*.txt` | Each `sql.sh` query and its output |

Save screenshots from UI runs into the same folder.

A proof meets four standards:
- It shows the action and the state after it: the upload response and the ready document, or the question and the cited answer.
- It checks the side effects as well as the response: chunks stored, `llm_calls` rows written, `chat_turns` recorded.
- It states `ai_mode` from `00-health.json`, because the fake provider proves wiring and grounding, not answer quality.
- It quotes the literal values it compared, such as page 3 and the quote text from `facts.yaml`.

## Cleanup

```bash
.claude/skills/verify-doc-vault/scripts/cleanup.sh
```

It removes only the project that `launch.sh` started here (it checks the marker file `$EVIDENCE_ROOT/.started-docvault-verify`). It runs `docker compose down -v` on that project alone, so the throwaway volumes go too. It never removes the evidence folder, and it prints the evidence folder's contents at the end. Run it after every run, failed runs included, so no stack holds the ports. Never `docker kill` or `pkill` by name.

## Helpers

| Script | Use |
|---|---|
| `scripts/env.sh` | Shared settings, sourced by the rest |
| `scripts/launch.sh [services]` | Start the isolated stack and wait for health |
| `scripts/doctor.sh` | Read-only health and freshness check |
| `scripts/call.sh METHOD PATH [curl args]` | One API call, saved as evidence |
| `scripts/wait-doc.sh ID [status\|insights] [timeout]` | Poll a document until it settles |
| `scripts/sql.sh "SQL"` | Read-only query in this project's database |
| `scripts/cleanup.sh` | Remove what `launch.sh` started, keep the evidence |
