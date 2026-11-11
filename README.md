# Doc Vault

Upload documents, get cited insights, and chat with them. Every answer points at a page you can open and check.

The service is a FastAPI app with Celery workers. Postgres 16 with pgvector is the only datastore, and Redis is the Celery broker.

## Run it

```bash
cp .env.example .env      # optional. With no key the stack runs on the fake provider.
docker compose up --build -d
curl localhost:8000/health
```

`GET /health` returns 200 with `"ai_mode": "fake"` when no provider key is set. Swagger is at `http://localhost:8000/docs`.

## Test it

The tests use the compose Postgres on host port 54329, in their own database (`vault_test`). They call no model provider.

```bash
docker compose up -d db
uv run pytest
uv run ruff check
```

## Status

This section lists what works at the current commit.

Built:

- Compose stack: `api` on port 8000, `worker`, `beat`, `db` (pgvector 0.8.0 on Postgres 16), and `redis`.
- `docvault/schema.sql`, applied by `db.migrate()` at every process start. The migration is idempotent and safe to run from several processes at once.
- `db.pipeline_steps()`: the per-document advisory lease and the `step_runs` log.
- `GET /health`.

Not built yet (the functions raise `NotImplementedError`):

- Upload, indexing, and every `/documents` route.
- The model gateway (`ai.py`) beyond the fake provider's settings.
- Insights, chat, metrics, and evals.
