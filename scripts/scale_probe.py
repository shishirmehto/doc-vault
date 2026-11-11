"""Time the read paths at vault scale on synthetic rows, in a scratch database that is dropped afterwards.

    docker compose up -d db
    uv run python scripts/scale_probe.py 50000

The scratch database is <PROBE_DATABASE_URL name>_scale on the compose Postgres. Every tenth document is
queued and stale; the rest are ready with parse, embed and analyze steps, three ledger rows each, and one
chat turn per document, spread over seven days. 300 ready documents get ten embedded chunks each.
"""

from __future__ import annotations

import os
import random
import statistics
import sys
import time
from datetime import timedelta

import psycopg
from pgvector import Vector
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from docvault import chat, db, documents, metrics
from docvault.domain import DocRef, DocumentId, PageBasis

TARGET = os.environ.get("PROBE_DATABASE_URL", "postgresql://vault:vault@localhost:54329/vault")
BASE = make_conninfo(TARGET, dbname="postgres")
NAME = f"{conninfo_to_dict(TARGET)['dbname']}_scale"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
CHUNKS_PER_DOC = 10
DIM = 1536


def timed(label, fn, runs=5):
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    print(f"{label}: median {statistics.median(samples):.1f} ms over {runs} runs (min {min(samples):.1f})")


with psycopg.connect(BASE, autocommit=True) as admin:
    admin.execute(f"DROP DATABASE IF EXISTS {NAME} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {NAME}")
url = make_conninfo(BASE, dbname=NAME)
db.migrate(url)
pool = db.open_pool(url, max_size=2)
try:
    started = time.perf_counter()
    with pool.connection() as conn:
        conn.execute("""
          INSERT INTO documents (sha256, filename, kind, size_bytes, status, page_basis, page_count, chunk_count,
                                 empty_pages, created_at, updated_at)
          SELECT sha256(i::text::bytea), 'doc-' || i || '.pdf', 'pdf', 50000,
                 CASE WHEN i %% 10 = 0 THEN 'queued' ELSE 'ready' END,
                 CASE WHEN i %% 10 = 0 THEN NULL ELSE 'physical' END,
                 CASE WHEN i %% 10 = 0 THEN NULL ELSE 12 END,
                 CASE WHEN i %% 10 = 0 THEN NULL ELSE %(chunks)s END,
                 CASE WHEN i %% 10 = 0 THEN NULL ELSE '{}'::int[] END,
                 now() - (i %% 168) * interval '1 hour', now() - interval '10 minutes'
          FROM generate_series(1, %(n)s) i""", {"n": N, "chunks": CHUNKS_PER_DOC})
        conn.execute("""
          INSERT INTO step_runs (document_id, step, attempt, started_at, finished_at, outcome)
          SELECT d.id, s.step, 1, d.created_at + interval '1 second', d.created_at + interval '3 seconds', 'ok'
          FROM documents d CROSS JOIN (VALUES ('parse'), ('embed'), ('analyze')) s(step) WHERE d.status = 'ready'""")
        conn.execute("""
          INSERT INTO llm_calls (at, purpose, provider, model, document_id, input_tokens, output_tokens, cost_usd,
                                 latency_ms, ok)
          SELECT d.created_at, p.purpose, 'openai', 'gpt-6.1-sol', d.id, 2000, 100, 0.005, 800 + (random() * 400)::int, true
          FROM documents d CROSS JOIN (VALUES ('embed_chunks'), ('embed_chunks'), ('insights')) p(purpose)""")
        conn.execute("INSERT INTO chats (id) SELECT gen_random_uuid() FROM generate_series(1, %s)", (N // 5,))
        conn.execute("""
          INSERT INTO chat_turns (id, chat_id, question, standalone_query, outcome, answer, started_at, finished_at)
          SELECT gen_random_uuid(), c.id, 'q', 'q', 'no_evidence', 'a', now() - interval '2 hours',
                 now() - interval '2 hours' + interval '900 milliseconds'
          FROM chats c CROSS JOIN generate_series(1, 5)""")
        ready = [row["id"] for row in conn.execute("SELECT id FROM documents WHERE status = 'ready' LIMIT 300")]
        rng = random.Random(1)
        with conn.cursor() as cur, cur.copy("COPY chunks (document_id, ord, text, page_map, embedding, embedding_model)"
                                            " FROM STDIN") as copy:
            for doc_id in ready:
                for ord_ in range(CHUNKS_PER_DOC):
                    vec = [rng.gauss(0, 1) for _ in range(DIM)]
                    copy.write_row((doc_id, ord_, f"limit deductible premium chunk {ord_}", "[[0, 1]]",
                                    str(Vector(vec).to_list()), "fake-hash-1536"))
        conn.execute("ANALYZE")
        counts = conn.execute("SELECT (SELECT count(*) FROM documents) d, (SELECT count(*) FROM step_runs) s,"
                              " (SELECT count(*) FROM llm_calls) l, (SELECT count(*) FROM chat_turns) t,"
                              " (SELECT count(*) FROM chunks) c").fetchone()
    print(f"seeded in {time.perf_counter() - started:.1f} s: {counts}")

    timed("GET /metrics/documents", lambda: metrics.document_stats(pool))
    timed("GET /metrics/processing?window=24h", lambda: metrics.processing_stats(pool, timedelta(hours=24)))
    timed("GET /metrics/processing?window=7d", lambda: metrics.processing_stats(pool, timedelta(days=7)))
    timed("documents.list_documents (first page of 20)",
          lambda: documents.list_documents(pool, status=None, limit=20, cursor=None))

    queued = N // 10
    with pool.connection() as conn:
        pick = conn.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON) SELECT id FROM documents WHERE status = ANY(%(f)s)"
            " AND updated_at < now() - interval '2 minutes'"
            " AND (requeued_at IS NULL OR requeued_at < now() - interval '2 minutes')"
            " ORDER BY requeued_at NULLS FIRST, updated_at LIMIT 50",
            {"f": ["queued", "parsing", "embedding"]}).fetchone()
    plan = pick["QUERY PLAN"][0]
    print(f"reconcile rule 1 pick over {queued} stale queued documents: {plan['Execution Time']:.1f} ms, 50 rows")

    scope = [DocRef(DocumentId(doc_id), f"d{i}", PageBasis.PHYSICAL) for i, doc_id in enumerate(ready[:3])]
    q_vec = [random.Random(2).gauss(0, 1) for _ in range(DIM)]

    def retrieve():
        with pool.connection() as conn:
            chat.retrieve(conn, scope, q_vec, "what is the deductible", "fake-hash-1536", per_doc=2)

    timed(f"chat.retrieve over 3 documents ({3 * CHUNKS_PER_DOC} chunks) in a vault of {len(ready) * CHUNKS_PER_DOC}"
          " embedded chunks", retrieve)
finally:
    pool.close()
    with psycopg.connect(BASE, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {NAME} WITH (FORCE)")
