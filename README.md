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

## Sample documents

`samples/` holds three fictitious documents for the same insured: a seven-page commercial general liability policy (PDF), a three-page renewal quote with changed figures (PDF), and a short endorsement (DOCX). `samples/facts.yaml` lists the facts planted in them, each with its document, page, and exact text. The tests and evals read that file as their expected values.

To regenerate the samples, run `uv run python scripts/make_samples.py`. The output is byte-identical on every run.

## Try the upload flow

```bash
cd samples
curl -s -F file=@sample-cgl-policy.pdf localhost:8000/documents        # 202, "status": "queued"
curl -s localhost:8000/documents                                       # poll until "status": "ready"
curl -s -F file=@sample-cgl-policy.pdf localhost:8000/documents        # 200, the same id: same bytes, no reprocessing
curl -s localhost:8000/documents/<id>/pages/2                          # the text a citation on page 2 points at
```

## Status

This section lists what works at the current commit.

Built:

- Compose stack: `api` on port 8000, `worker`, `beat`, `db` (pgvector 0.8.0 on Postgres 16), and `redis`.
- `docvault/schema.sql`. `db.migrate()` runs at every process start and applies the file only when its hash is not yet recorded in `schema_version`.
- `db.pipeline_steps()`: the per-document advisory lease and the `step_runs` log.
- `GET /health`.
- The sample documents and `samples/facts.yaml`.
- `docvault/pages.py`: format detection from file bytes, text extraction for PDF, DOCX, and TXT, the chunker with per-chunk page maps, and `locate_quote`. `find_on_page` is not built yet.
- Upload with content-hash dedupe: `POST /documents` returns 202 for new bytes and 200 with the existing document for bytes it has seen.
- Indexing in Celery (`documents.run_index`): parse, chunk, and embed with the fake provider. A rerun after a crash embeds only the chunks that are still pending and does not parse again.
- `GET /documents`, `GET /documents/{id}`, `GET /documents/{id}/pages/{n}`, `POST /documents/{id}/retry`, and `DELETE /documents/{id}`.
- `documents.reconcile` on Celery beat every 60 seconds. It re-enqueues documents that stalled for two minutes and re-embeds ready documents after the embedding model changes.
- `ai.AI.embed` with the fake embedder. Each provider call writes one `llm_calls` row.

Not built yet (the functions raise `NotImplementedError`):

- Real model providers and `ai.AI.complete`. Setting `OPENAI_API_KEY` makes the service fail at start until they exist.
- Insights (`GET /documents/{id}/insights`, `insights.run_analyze`). Indexing and `reconcile` do not enqueue the analyze task yet. Add those two calls with `run_analyze`: in `documents.run_index` after the parse checkpoint, and as a third rule in `documents.reconcile`.
- Chat, metrics, and evals.

Known limits:

- The upload size limit is enforced while the service copies the file to storage. The web framework has already received the whole request body by then.
- The embedding progress count in `GET /documents/{id}` counts chunks with any embedding, so it reads as complete during a re-embed after a model change.
