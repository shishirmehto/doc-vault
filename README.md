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
- `docvault/schema.sql`. `db.migrate()` runs at every process start and applies the file only when its hash is not yet recorded in `schema_version`. `chunks.embedding` has no fixed dimension: `chunks.embedding_model` names the model and the dimension, and every read filters on it.
- `db.pipeline_steps()`: the per-document advisory lease and the `step_runs` log.
- `GET /health`.
- The sample documents and `samples/facts.yaml`.
- `docvault/pages.py`: format detection from file bytes, text extraction for PDF, DOCX, and TXT, the chunker with per-chunk page maps, `locate_quote`, and `find_on_page`.
- Upload with content-hash dedupe: `POST /documents` returns 202 for new bytes and 200 with the existing document for bytes it has seen.
- Indexing in Celery (`documents.run_index`): parse, chunk, and embed with the configured provider. A rerun after a crash embeds only the chunks that are still pending and does not parse again.
- `GET /documents`, `GET /documents/{id}`, `GET /documents/{id}/pages/{n}`, `POST /documents/{id}/retry`, and `DELETE /documents/{id}`.
- `documents.reconcile` on Celery beat every 60 seconds. It re-enqueues documents that stalled for two minutes, re-embeds ready documents after the embedding model changes, and enqueues insights for parsed documents that have none.
- The AI gateway (`docvault/ai.py`): `embed()` and `complete()`, with OpenAI (Chat Completions with strict JSON-schema output, and embeddings) and the deterministic fake. Every provider attempt, retries and failures included, writes one `llm_calls` row priced from `ai.PRICES` (cached input at its own rate). Chat calls retry 429, 5xx and timeouts in the gateway with doubling backoff. Background embedding and insights calls get one attempt each, and Celery retries the task, which frees the worker and the document lease between tries. Output that fails validation gets one repair turn. Model ids, reasoning effort and the embedding dimension come from the environment (`.env.example`). `GET /health` reports `ai_mode` as `fake`, `live` or `mixed`.
- Insights (`insights.run_analyze` on the `llm` queue, `GET /documents/{id}/insights`). Analysis starts after the parse checkpoint, beside embedding. It returns a title, a document type, a summary, tags, and key facts, each with a page and a quote. A key fact is kept only when its quote is on the page it cites; the rest are dropped and counted (`dropped_key_facts`). `coverage` reports the pages read when a document exceeds the prompt budget. The endpoint reads `pending`, `ready`, `failed` or `unavailable`. An insights failure leaves the document searchable, and `POST /documents/{id}/retry` reruns only the insights. The insights prompt in `docvault/prompts.py` carries a note on why it is worded the way it is.

Not built yet (the functions raise `NotImplementedError`):

- Chat, metrics, and evals.

Known limits:

- Insights read a page prefix that fits the prompt budget (240,000 characters, roughly 60,000 tokens). Longer documents report partial coverage; map-reduce over the rest is not built.
- A key fact is checked only for its quote being on the cited page, not for its value appearing in the quote.
- The OpenAI backends are tested against canned HTTP responses only. They have not run against the live API yet.
- Gateway retries have no jitter, so requests that hit a rate limit together retry together.
- Celery's retry countdown (2 to 32 seconds) ignores the provider's Retry-After header.
- The upload size limit is enforced while the service copies the file to storage. The web framework has already received the whole request body by then.
- The embedding progress count in `GET /documents/{id}` counts chunks with any embedding, so it reads as complete during a re-embed after a model change.
