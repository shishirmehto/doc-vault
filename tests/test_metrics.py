"""GET /metrics/documents and /metrics/processing over rows written by the real pipeline, insights and chat.

The pipeline writes every row. The test then pins the only inputs that vary
between runs, timestamps and provider latency, so each percentile is a literal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from docvault import chat, documents, insights
from docvault.ai import ProviderError
from docvault.domain import DocumentId, VaultError
from docvault.fake_ai import FakeChat

from .conftest import ENDORSEMENT, POLICY, QUOTE, SAMPLES, Crash
from .test_documents import FailingEmbedder, scanned_pdf, upload, with_embedder
from .test_insights import provider_down, with_chat


class PricedChat(FakeChat):
    models = {"main": "gpt-6.1-sol", "small": "gpt-6-luna"}


def at(base: datetime, ms: int) -> datetime:
    return base + timedelta(milliseconds=ms)


def pin_step(db, doc_id: DocumentId, step: str, nth: int, start: datetime, end: datetime) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE step_runs SET started_at = %s, finished_at = %s WHERE id = (SELECT id FROM step_runs"
                     " WHERE document_id = %s AND step = %s ORDER BY id OFFSET %s LIMIT 1)",
                     (start, end, doc_id, step, nth))


@pytest.fixture
def seeded(ctx, tmp_path):
    """Three ready documents, one failed, one queued; insights in all four states; three chat turns."""
    priced = with_chat(ctx, PricedChat())
    policy = upload(priced, SAMPLES / POLICY).document.id
    quote = upload(priced, SAMPLES / QUOTE).document.id
    endorsement = upload(priced, SAMPLES / ENDORSEMENT).document.id
    scanned = upload(priced, scanned_pdf(tmp_path)).document.id
    notes = tmp_path / "notes.txt"
    notes.write_text("Renewal call notes. Ask about the silica exclusion.")
    upload(priced, notes)

    documents.run_index(priced, policy)
    with pytest.raises(VaultError):   # a retryable provider failure on attempt 1: Celery would retry
        documents.run_index(with_embedder(priced, FailingEmbedder(0, ProviderError("timeout", retryable=True))), quote)
    documents.run_index(priced, quote, attempt=2)
    with pytest.raises(Crash):
        documents.run_index(with_embedder(priced, FailingEmbedder(0, Crash())), endorsement)
    documents.run_index(priced, endorsement)
    documents.run_index(priced, scanned)

    insights.run_analyze(priced, policy)
    down = with_chat(priced, provider_down("invalid_request", retryable=False))
    with pytest.raises(VaultError) as raised:
        insights.run_analyze(down, quote)
    insights.fail(priced, quote, raised.value.failure)

    chat_id = chat.start(priced.db, [policy]).id
    turns = [chat.ask(priced, chat_id, question).id
             for question in ("What is the each occurrence limit?", "Is flood damage covered?", "Roofing?")]

    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    old = t0 - timedelta(days=3)
    with ctx.db.connection() as conn:
        conn.execute("UPDATE documents SET created_at = %s", (t0,))
        conn.execute("UPDATE documents SET created_at = %s WHERE id = %s", (old, scanned))
        # Provider latency 10, 20, 30 ... ms in call order within each purpose and model.
        conn.execute("UPDATE llm_calls l SET latency_ms = 10 * n.rn FROM (SELECT id, row_number() OVER"
                     " (PARTITION BY purpose, model ORDER BY id) AS rn FROM llm_calls) n WHERE n.id = l.id")
        for turn_id, ms in zip(turns, (120, 30, 90), strict=True):
            conn.execute("UPDATE chat_turns SET started_at = %s, finished_at = %s WHERE id = %s",
                         (at(t0, 10_000), at(t0, 10_000 + ms), turn_id))
    for doc_id, step, nth, start, end in [
        (policy, "parse", 0, 100, 500), (policy, "embed", 0, 500, 1500),
        (quote, "parse", 0, 200, 800), (quote, "embed", 0, 800, 900), (quote, "embed", 1, 5000, 8000),
        (endorsement, "parse", 0, 300, 500), (endorsement, "embed", 0, 500, 600),
        (endorsement, "embed", 1, 1000, 3000),
        (policy, "analyze", 0, 2000, 7000), (quote, "analyze", 0, 2000, 2100),
    ]:
        pin_step(ctx.db, doc_id, step, nth, at(t0, start), at(t0, end))
    pin_step(ctx.db, scanned, "parse", 0, at(old, 100), at(old, 150))


def test_document_metrics_count_every_status_kind_and_insights_state(client, seeded):
    assert client.get("/metrics/documents").json() == {
        "total": 5,
        "by_status": {"queued": 1, "parsing": 0, "embedding": 0, "ready": 3, "failed": 1},
        "by_kind": {"pdf": 3, "docx": 1, "txt": 1},
        "insights": {"pending": 2, "ready": 1, "failed": 1, "unavailable": 1},
        "pages": 12, "chunks": 11, "bytes": 55568,
        "failures_by_code": {"no_extractable_text": 1},
    }


def usage(purpose, model, calls, errors, input_tokens, output_tokens, cost_usd, *, p50, p95) -> dict:
    return {"purpose": purpose, "model": model, "calls": calls, "errors": errors, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "cost_usd": cost_usd, "unpriced_calls": 0,
            "latency_ms": {"p50": p50, "p95": p95}}


def test_a_window_outside_1h_to_9999d_is_refused(client, ctx):
    assert [client.get("/metrics/processing", params={"window": w}).status_code
            for w in ("0h", "24", "1w", "10000d", "7d")] == [422, 422, 422, 422, 200]


def test_processing_metrics_over_a_day_and_a_week(client, seeded):
    day = client.get("/metrics/processing", params={"window": "24h"}).json()
    since = datetime.fromisoformat(day.pop("since"))
    assert timedelta(hours=23, minutes=59) < datetime.now(UTC) - since < timedelta(hours=24, minutes=1)
    no_failures = {"failed": 0, "abandoned": 0, "retries": 0, "failures_by_code": {}}
    assert day == {
        "window": "24h",
        "uploaded": 4,                                       # the scanned PDF was uploaded three days ago
        "indexed": 3,
        "time_to_ready_ms": {"p50": 3000, "p95": 7500},      # 1500, 3000 and 8000 ms
        "queue_wait_ms": {"p50": 200, "p95": 290},           # 100, 200 and 300 ms
        "steps": {
            "parse": {"runs": 3, "ok": 3, **no_failures, "duration_ms": {"p50": 400, "p95": 580}},
            "embed": {"runs": 5, "ok": 3, "failed": 1, "abandoned": 1, "retries": 1,
                      "duration_ms": {"p50": 2000, "p95": 2900}, "failures_by_code": {"ai_unavailable": 1}},
            "analyze": {"runs": 2, "ok": 1, "failed": 1, "abandoned": 0, "retries": 0,
                        "duration_ms": {"p50": 5000, "p95": 5000}, "failures_by_code": {"ai_unavailable": 1}},
        },
        "ai": {
            "calls": 16, "errors": 2, "input_tokens": 9032, "output_tokens": 164, "cost_usd": 0.013768,
            "unpriced_calls": 0,
            "by_purpose_and_model": [
                usage("answer", "gpt-6.1-sol", 2, 0, 4405, 41, 0.00922, p50=15, p95=20),
                usage("condense", "gpt-6-luna", 2, 0, 213, 5, 0.000024, p50=15, p95=20),
                usage("embed_chunks", "fake-hash", 7, 1, 2731, 0, 0.0, p50=40, p95=67),
                usage("embed_query", "fake-hash", 3, 0, 11, 0, 0.0, p50=20, p95=29),
                usage("insights", "fake-chat", 1, 1, 0, 0, 0.0, p50=10, p95=10),
                usage("insights", "gpt-6.1-sol", 1, 0, 1672, 118, 0.004524, p50=10, p95=10),
            ],
        },
        "chat": {"turns": 3, "outcomes": {"answered": 1, "no_evidence": 1, "model_declined": 1, "unverified": 0},
                 "latency_ms": {"p50": 90, "p95": 117}},             # 30, 90 and 120 ms
        "cache": None,                                       # no CACHE_URL
    }

    week = client.get("/metrics/processing", params={"window": "7d"}).json()
    assert (week["uploaded"], week["queue_wait_ms"], week["steps"]["parse"]) == (
        5, {"p50": 150, "p95": 285},
        {"runs": 4, "ok": 3, "failed": 1, "abandoned": 0, "retries": 0, "duration_ms": {"p50": 400, "p95": 580},
         "failures_by_code": {"no_extractable_text": 1}})


def test_an_empty_vault_reports_zeros_and_no_percentiles(client, ctx):
    empty = {"p50": None, "p95": None}
    body = client.get("/metrics/processing").json()
    assert (body["window"], body["time_to_ready_ms"], body["steps"]["embed"], body["ai"], body["chat"]) == (
        "24h", empty,
        {"runs": 0, "ok": 0, "failed": 0, "abandoned": 0, "retries": 0, "duration_ms": empty, "failures_by_code": {}},
        {"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "unpriced_calls": 0,
         "by_purpose_and_model": []},
        {"turns": 0, "outcomes": {"answered": 0, "no_evidence": 0, "model_declined": 0, "unverified": 0},
         "latency_ms": empty})
    assert client.get("/metrics/documents").json()["by_status"] == {
        "queued": 0, "parsing": 0, "embedding": 0, "ready": 0, "failed": 0}

