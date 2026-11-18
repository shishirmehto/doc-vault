"""Upload, the lifecycle and the indexing pipeline. Fake provider, real Postgres, no Celery."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from docvault import documents, insights, tasks
from docvault.ai import ProviderError
from docvault.db import Lease, pipeline_steps
from docvault.domain import (
    Created,
    DocStatus,
    Embedding,
    ErrorCode,
    Existing,
    Failed,
    Failure,
    InsightsPending,
    InsightsReady,
    InsightsUnavailable,
    PageBasis,
    Parsed,
    Queued,
    Ready,
    VaultError,
)
from docvault.fake_ai import FakeEmbedder

from . import conftest
from .conftest import SAMPLES, Crash, advisory_locks, scalar, step_rows
from .test_pages import bookmarked_pdf

POLICY = SAMPLES / conftest.POLICY
QUOTE = SAMPLES / conftest.QUOTE


class FailingEmbedder(FakeEmbedder):
    """Works for the first `good_calls` batches, then raises."""

    def __init__(self, good_calls: int, error: BaseException) -> None:
        super().__init__()
        self.good_calls, self.error = good_calls, error

    def embed(self, texts, *, timeout_s):
        if self.good_calls == 0:
            raise self.error
        self.good_calls -= 1
        return super().embed(texts, timeout_s=timeout_s)


def with_embedder(ctx, embedder):
    return dataclasses.replace(ctx, ai=dataclasses.replace(ctx.ai, embedder=embedder))


def upload(ctx, path: Path, name: str | None = None):
    with path.open("rb") as stream:
        return documents.upload(ctx, stream, name or path.name)


def steps(ctx, doc_id):
    return [(step, outcome, error) for step, _attempt, outcome, error in step_rows(ctx.db, doc_id)]


def scanned_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "scan.pdf"
    canvas = Canvas(str(path), pagesize=letter)
    canvas.rect(72, 72, 400, 600, fill=1)
    canvas.save()
    return path


def test_upload_then_index_reaches_ready_with_every_chunk_embedded(ctx):
    outcome = upload(ctx, POLICY)
    assert isinstance(outcome, Created)
    doc = outcome.document
    assert (doc.filename, doc.kind, doc.size_bytes, doc.lifecycle) == ("sample-cgl-policy.pdf", "pdf", 11193, Queued(None))
    assert ctx.jobs.indexed == [doc.id]

    documents.run_index(ctx, doc.id)

    indexed = documents.get(ctx.db, doc.id)
    assert indexed.lifecycle == Ready(Parsed(PageBasis.PHYSICAL, page_count=7, chunk_count=6, empty_pages=()))
    assert indexed.insights == InsightsPending()
    assert indexed.processing_ms >= 0
    assert steps(ctx, doc.id) == [("parse", "ok", None), ("embed", "ok", None)]
    assert scalar(ctx.db, "SELECT count(*) FROM chunks WHERE embedding_model = 'fake-hash-1536'") == 6
    with ctx.db.connection() as conn:
        ledger = conn.execute("SELECT purpose, provider, document_id, ok, cost_usd, input_tokens > 0 AS billed"
                              " FROM llm_calls").fetchall()
    assert ledger == [{"purpose": "embed_chunks", "provider": "fake", "document_id": doc.id, "ok": True,
                       "cost_usd": 0, "billed": True}] * 3   # 6 chunks in batches of 2


class RecordingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []

    def embed(self, texts, *, timeout_s):
        self.batches.append(list(texts))
        return super().embed(texts, timeout_s=timeout_s)


def test_parse_stores_the_outline_and_each_chunk_is_embedded_as_its_filename_and_text(ctx, tmp_path):
    path = bookmarked_pdf(tmp_path, [["Contents of this manual."], ["Part 9: Options", "Option text one."],
                                     ["903 Land Option", "Land text."]],
                          [("Part 9: Options", 1, None), ("903 Land Option", 2, 0)])
    embedder = RecordingEmbedder()
    small = dataclasses.replace(with_embedder(ctx, embedder), settings=ctx.settings.model_copy(
        update={"chunk_max_chars": 40, "chunk_overlap_chars": 0}))
    doc_id = upload(small, path).document.id

    documents.run_index(small, doc_id)

    with ctx.db.connection() as conn:
        outline = conn.execute("SELECT outline FROM documents WHERE id = %s", (doc_id,)).fetchone()["outline"]
    assert outline == [{"title": "Part 9: Options", "level": 0, "page": 2},
                       {"title": "903 Land Option", "level": 1, "page": 3}]
    assert [text for batch in embedder.batches for text in batch] == [
        "bookmarked.pdf\nContents of this manual.", "bookmarked.pdf\nPart 9: Options\nOption text one.",
        "bookmarked.pdf\n903 Land Option\nLand text."]


def test_same_bytes_twice_is_one_document(ctx):
    first = upload(ctx, POLICY)
    second = upload(ctx, POLICY, "copy-of-policy.pdf")
    assert isinstance(first, Created) and isinstance(second, Existing)
    assert second.document.id == first.document.id
    assert second.document.filename == "sample-cgl-policy.pdf"
    assert ctx.jobs.indexed == [first.document.id]
    assert scalar(ctx.db, "SELECT count(*) FROM documents") == 1


def test_index_converges_after_a_crash_between_embed_batches(ctx):
    doc_id = upload(ctx, POLICY).document.id
    with pytest.raises(Crash):
        documents.run_index(with_embedder(ctx, FailingEmbedder(1, Crash())), doc_id)

    crashed = documents.get(ctx.db, doc_id)
    assert isinstance(crashed.lifecycle, Embedding) and crashed.lifecycle.embedded_chunks == 2
    assert steps(ctx, doc_id) == [("parse", "ok", None), ("embed", None, None)]
    assert advisory_locks(ctx.db) == 0

    documents.run_index(ctx, doc_id)
    documents.run_index(ctx, doc_id)   # a duplicate delivery after the work is done

    assert isinstance(documents.get(ctx.db, doc_id).lifecycle, Ready)
    assert steps(ctx, doc_id) == [("parse", "ok", None), ("embed", "abandoned", None), ("embed", "ok", None)]
    assert scalar(ctx.db, "SELECT count(*) FROM chunks WHERE embedding IS NULL") == 0
    assert scalar(ctx.db, "SELECT count(*) FROM llm_calls") == 3   # the two chunks embedded before the crash were not paid for twice


def test_delivery_while_another_worker_holds_the_document_does_nothing(ctx):
    doc_id = upload(ctx, POLICY).document.id
    with pipeline_steps(ctx.db, doc_id, Lease.INDEX, 1):
        documents.run_index(ctx, doc_id)
    assert documents.get(ctx.db, doc_id).lifecycle == Queued(None)
    assert steps(ctx, doc_id) == []


def test_scanned_pdf_fails_with_a_reason_and_can_be_retried(ctx, tmp_path):
    doc_id = upload(ctx, scanned_pdf(tmp_path)).document.id
    documents.run_index(ctx, doc_id)

    failed = documents.get(ctx.db, doc_id)
    assert isinstance(failed.lifecycle, Failed)
    assert (failed.lifecycle.failure.code, failed.lifecycle.failure.retryable, failed.lifecycle.parsed) == (
        ErrorCode.NO_EXTRACTABLE_TEXT, False, None)
    assert failed.insights == InsightsUnavailable()
    assert steps(ctx, doc_id) == [("parse", "failed", "no_extractable_text")]
    documents.run_index(ctx, doc_id)   # a redelivery must not revive a failed document
    assert steps(ctx, doc_id) == [("parse", "failed", "no_extractable_text")]

    assert documents.retry(ctx, doc_id).lifecycle == Queued(None)
    assert ctx.jobs.indexed == [doc_id, doc_id]


def test_a_file_the_parser_cannot_read_logs_the_parsers_own_error(ctx, tmp_path, caplog):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\nnot really a pdf")
    doc_id = upload(ctx, broken).document.id
    documents.run_index(ctx, doc_id)

    assert documents.get(ctx.db, doc_id).lifecycle.failure.code == ErrorCode.UNREADABLE_FILE
    [record] = [r for r in caplog.records if r.name == "docvault.documents"]
    assert (record.levelname, record.getMessage()) == ("WARNING", f"Indexing document {doc_id} failed: unreadable_file")
    assert record.exc_info[0].__module__.startswith("pypdf")


def test_retry_after_a_provider_failure_resumes_at_embedding_without_parsing_again(ctx):
    doc_id = upload(ctx, POLICY).document.id
    down = with_embedder(ctx, FailingEmbedder(1, ProviderError("timeout", retryable=True)))
    documents.run_index(down, doc_id, attempt=documents.MAX_RETRIES + 1)
    failed = documents.get(ctx.db, doc_id).lifecycle
    assert isinstance(failed, Failed) and failed.parsed.chunk_count == 6
    assert (failed.failure.code, failed.failure.retryable) == (ErrorCode.AI_UNAVAILABLE, True)

    assert documents.retry(ctx, doc_id).lifecycle == Queued(Parsed(PageBasis.PHYSICAL, page_count=7, chunk_count=6, empty_pages=()))
    documents.run_index(ctx, doc_id)

    assert isinstance(documents.get(ctx.db, doc_id).lifecycle, Ready)
    assert steps(ctx, doc_id) == [("parse", "ok", None), ("embed", "failed", "ai_unavailable"), ("embed", "ok", None)]
    with ctx.db.connection() as conn:
        calls = [(r["ok"], r["error_code"]) for r in conn.execute("SELECT ok, error_code FROM llm_calls ORDER BY id")]
    assert calls == [(True, None), (False, "timeout"), (True, None), (True, None)]   # Celery retries, not the gateway


def test_retry_refuses_a_document_that_did_not_fail(ctx):
    doc_id = upload(ctx, POLICY).document.id
    with pytest.raises(VaultError) as raised:
        documents.retry(ctx, doc_id)
    assert raised.value.failure.code is ErrorCode.NOTHING_TO_RETRY
    assert ctx.jobs.indexed == [doc_id]


def test_reconcile_requeues_stalled_documents_only(ctx):
    stalled = upload(ctx, POLICY).document.id
    fresh = upload(ctx, QUOTE).document.id
    with ctx.db.connection() as conn:
        conn.execute("UPDATE documents SET updated_at = now() - interval '5 minutes' WHERE id = %s", (stalled,))
    ctx.jobs.indexed.clear()

    assert documents.reconcile(ctx) == 1
    assert ctx.jobs.indexed == [stalled]
    assert documents.get(ctx.db, fresh).lifecycle == Queued(None)


def test_reconcile_re_enqueues_a_stale_backlog_a_batch_at_a_time_and_reaches_every_document(ctx, monkeypatch):
    monkeypatch.setattr(documents, "INDEX_BATCH", 2)
    oldest, older, old = (upload(ctx, path).document.id for path in (POLICY, QUOTE, SAMPLES / conftest.ENDORSEMENT))
    with ctx.db.connection() as conn:
        for doc_id, minutes in ((oldest, 9), (older, 8), (old, 7)):
            conn.execute("UPDATE documents SET updated_at = now() - %s * interval '1 minute' WHERE id = %s",
                         (minutes, doc_id))
    ctx.jobs.indexed.clear()

    ticks = []
    for _ in range(3):
        documents.reconcile(ctx)
        ticks.append(set(ctx.jobs.indexed))
        ctx.jobs.indexed.clear()
    assert ticks == [{oldest, older}, {old}, set()]

    with ctx.db.connection() as conn:   # two more minutes pass
        conn.execute("UPDATE documents SET requeued_at = requeued_at - interval '3 minutes'")
    assert documents.reconcile(ctx) == 2
    assert set(ctx.jobs.indexed) == {oldest, older}


def test_reconcile_re_embeds_a_ready_document_after_the_embedding_model_changes(ctx):
    doc_id = upload(ctx, QUOTE).document.id
    documents.run_index(ctx, doc_id)
    ctx.jobs.indexed.clear()
    assert documents.reconcile(ctx) == 0

    class NewModel(FakeEmbedder):
        model = "fake-hash-v2"

    switched = with_embedder(ctx, NewModel())
    assert documents.reconcile(switched) == 1
    assert ctx.jobs.indexed == [doc_id]
    assert isinstance(documents.get(ctx.db, doc_id).lifecycle, Embedding)

    documents.run_index(switched, doc_id)
    assert isinstance(documents.get(ctx.db, doc_id).lifecycle, Ready)
    with ctx.db.connection() as conn:
        models = [r["embedding_model"] for r in conn.execute("SELECT DISTINCT embedding_model FROM chunks")]
    assert models == ["fake-hash-v2-1536"]
    assert steps(ctx, doc_id) == [("parse", "ok", None), ("embed", "ok", None), ("embed", "ok", None)]


def chunk_rows(ctx, doc_id):
    with ctx.db.connection() as conn:
        return conn.execute("SELECT id, text, embedding_model FROM chunks WHERE document_id = %s ORDER BY ord",
                            (doc_id,)).fetchall()


def test_reconcile_cuts_a_ready_document_again_after_the_chunk_settings_change(ctx):
    doc_id = upload(ctx, POLICY).document.id
    documents.run_index(ctx, doc_id)
    ctx.jobs.indexed.clear()
    assert documents.reconcile(ctx) == 0
    before = chunk_rows(ctx, doc_id)
    with ctx.db.connection() as conn:
        pages_before = conn.execute("SELECT number, text FROM pages WHERE document_id = %s ORDER BY number",
                                    (doc_id,)).fetchall()

    small = dataclasses.replace(ctx, settings=ctx.settings.model_copy(
        update={"chunk_max_chars": 600, "chunk_overlap_chars": 100}))
    assert documents.reconcile(small) == 1
    assert ctx.jobs.indexed == [doc_id]
    assert isinstance(documents.get(ctx.db, doc_id).lifecycle, Embedding)
    after = chunk_rows(ctx, doc_id)
    assert len(after) > len(before)
    assert all(len(row["text"]) <= 600 and row["embedding_model"] is None for row in after)

    documents.run_index(small, doc_id)
    assert isinstance(documents.get(ctx.db, doc_id).lifecycle, Ready)
    assert scalar(ctx.db, "SELECT chunk_count FROM documents WHERE id = %s", (doc_id,)) == len(after)
    assert all(row["embedding_model"] is not None for row in chunk_rows(ctx, doc_id))
    with ctx.db.connection() as conn:
        assert conn.execute("SELECT number, text FROM pages WHERE document_id = %s ORDER BY number",
                            (doc_id,)).fetchall() == pages_before
    assert scalar(ctx.db, "SELECT chunking FROM documents WHERE id = %s", (doc_id,)) == documents.chunking(small.settings)
    ctx.jobs.indexed.clear()
    assert documents.reconcile(small) == 0


def test_reconcile_stamps_a_document_cut_before_chunking_was_recorded_without_cutting_it_again(ctx):
    doc_id = upload(ctx, QUOTE).document.id
    documents.run_index(ctx, doc_id)
    before = chunk_rows(ctx, doc_id)
    with ctx.db.connection() as conn:
        conn.execute("UPDATE documents SET chunking = NULL WHERE id = %s", (doc_id,))
    ctx.jobs.indexed.clear()

    assert documents.reconcile(ctx) == 0
    assert ctx.jobs.indexed == []
    assert isinstance(documents.get(ctx.db, doc_id).lifecycle, Ready)
    assert chunk_rows(ctx, doc_id) == before
    assert scalar(ctx.db, "SELECT chunking FROM documents WHERE id = %s", (doc_id,)) == documents.chunking(ctx.settings)


def test_rechunk_leaves_a_document_that_is_not_ready(ctx):
    doc_id = upload(ctx, QUOTE).document.id
    small = ctx.settings.model_copy(update={"chunk_max_chars": 600, "chunk_overlap_chars": 100})
    with ctx.db.connection() as conn:
        assert documents.rechunk(conn, doc_id, small) is False
    assert isinstance(documents.get(ctx.db, doc_id).lifecycle, Queued)


def test_processing_time_is_the_first_indexing_and_a_later_re_embed_leaves_it(ctx):
    doc_id = upload(ctx, QUOTE).document.id
    documents.run_index(ctx, doc_id)
    first = documents.get(ctx.db, doc_id).processing_ms
    with ctx.db.connection() as conn:   # the provider key arrives an hour later
        conn.execute("UPDATE documents SET created_at = created_at - interval '1 hour' WHERE id = %s", (doc_id,))
        conn.execute("UPDATE step_runs SET started_at = started_at - interval '1 hour',"
                     " finished_at = finished_at - interval '1 hour' WHERE document_id = %s", (doc_id,))

    class NewModel(FakeEmbedder):
        model = "fake-hash-v2"

    switched = with_embedder(ctx, NewModel())
    documents.reconcile(switched)
    documents.run_index(switched, doc_id)
    assert documents.get(ctx.db, doc_id).processing_ms == first


def test_delete_removes_the_document_and_its_blob_but_keeps_the_history(ctx):
    doc_id = upload(ctx, QUOTE).document.id
    documents.run_index(ctx, doc_id)
    [blob] = [p for p in ctx.blobs.root.rglob("*") if p.is_file()]

    documents.delete(ctx, doc_id)

    assert not blob.exists()
    with pytest.raises(VaultError) as raised:
        documents.get(ctx.db, doc_id)
    assert raised.value.failure.code is ErrorCode.DOCUMENT_NOT_FOUND
    assert scalar(ctx.db, "SELECT count(*) FROM chunks") == 0
    assert steps(ctx, doc_id) == [("parse", "ok", None), ("embed", "ok", None)]
    assert scalar(ctx.db, "SELECT count(*) FROM llm_calls WHERE document_id = %s", (doc_id,)) == 1


def test_every_status_is_reachable_and_only_ready_is_terminal_without_retry():
    reachable = {DocStatus.QUEUED} | {t for targets in documents.TRANSITIONS.values() for t in targets}
    assert reachable == set(DocStatus)
    assert documents.sources_of(DocStatus.FAILED) == [DocStatus.QUEUED, DocStatus.PARSING, DocStatus.EMBEDDING]


def test_task_marks_a_bad_file_failed(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "_ctx", lambda: ctx)
    doc_id = upload(ctx, scanned_pdf(tmp_path)).document.id

    tasks.index_document.apply(args=[str(doc_id)])

    lifecycle = documents.get(ctx.db, doc_id).lifecycle
    assert isinstance(lifecycle, Failed) and lifecycle.failure.code is ErrorCode.NO_EXTRACTABLE_TEXT


def test_task_retries_a_provider_outage_then_fails_the_document(ctx, monkeypatch):
    down = with_embedder(ctx, FailingEmbedder(0, ProviderError("rate_limited", retryable=True)))
    monkeypatch.setattr(tasks, "_ctx", lambda: down)
    doc_id = upload(ctx, QUOTE).document.id

    tasks.index_document.apply(args=[str(doc_id)])

    lifecycle = documents.get(ctx.db, doc_id).lifecycle
    assert isinstance(lifecycle, Failed)
    assert (lifecycle.failure.code, lifecycle.failure.retryable) == (ErrorCode.AI_UNAVAILABLE, True)
    assert [attempt for step, attempt, _, _ in step_rows(ctx.db, doc_id) if step == "embed"] == [1, 2, 3, 4, 5, 6]


def test_task_turns_a_bug_into_a_retryable_internal_error(ctx, monkeypatch):
    monkeypatch.setattr(tasks, "_ctx", lambda: with_embedder(ctx, FailingEmbedder(0, RuntimeError("a bug"))))
    doc_id = upload(ctx, QUOTE).document.id

    tasks.index_document.apply(args=[str(doc_id)])

    lifecycle = documents.get(ctx.db, doc_id).lifecycle
    assert isinstance(lifecycle, Failed)
    assert (lifecycle.failure.code, lifecycle.failure.retryable) == (ErrorCode.INTERNAL, True)
    assert steps(ctx, doc_id) == [("parse", "ok", None), ("embed", "failed", "internal_error")]


def test_a_nul_byte_past_the_sniffed_head_is_dropped_and_the_text_still_indexes(ctx, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"Clean text. " * 700 + b"Each Occurrence\x00 Limit $500.\n")   # 8,400 clean bytes, then a NUL
    doc_id = upload(ctx, path).document.id

    documents.run_index(ctx, doc_id)

    ready = documents.get(ctx.db, doc_id).lifecycle
    assert isinstance(ready, Ready)
    assert documents.page(ctx.db, doc_id, ready.parsed.page_count)[1].text.endswith("Each Occurrence Limit $500.")


def test_one_retry_of_a_failed_document_also_reruns_its_failed_insights(ctx):
    doc_id = upload(ctx, POLICY).document.id
    documents.run_index(with_embedder(ctx, FailingEmbedder(0, ProviderError("timeout", retryable=True))), doc_id,
                        attempt=documents.MAX_RETRIES + 1)
    insights.fail(ctx, doc_id, Failure(ErrorCode.AI_UNAVAILABLE, "The provider timed out.", retryable=True))
    ctx.jobs.analyzed.clear()

    retried = documents.retry(ctx, doc_id)

    assert (type(retried.lifecycle), retried.insights, ctx.jobs.analyzed) == (Queued, InsightsPending(), [doc_id])
    documents.run_index(ctx, doc_id)
    insights.run_analyze(ctx, doc_id)
    assert (type(documents.get(ctx.db, doc_id).lifecycle), type(documents.get(ctx.db, doc_id).insights)) == (
        Ready, InsightsReady)
