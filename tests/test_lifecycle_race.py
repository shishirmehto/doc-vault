"""A run that ends in failure writes FAILED while its worker still holds the INDEX lease.

Worker B stands for a reconcile re-queue or a duplicate delivery of the same document.
If B can take the lease while FAILED is written, the user sees Failed while B is parsing.
"""

from __future__ import annotations

import pytest

from docvault import documents, tasks
from docvault.ai import ProviderError
from docvault.db import Lease, pipeline_steps
from docvault.domain import DocStatus, ErrorCode, Failed

from .conftest import QUOTE, SAMPLES
from .test_documents import FailingEmbedder, scanned_pdf, upload, with_embedder


@pytest.mark.xfail(strict=True, reason="F1")
@pytest.mark.parametrize(("source", "embed_error", "code"), [
    pytest.param("scan", None, ErrorCode.NO_EXTRACTABLE_TEXT, id="parse-no-text-layer"),
    pytest.param("quote", ProviderError("rate_limited", retryable=True), ErrorCode.AI_UNAVAILABLE,
                 id="embed-retries-used-up"),
    pytest.param("quote", RuntimeError("a bug"), ErrorCode.INTERNAL, id="embed-bug"),
])
def test_no_other_worker_can_take_the_lease_while_failed_is_written(ctx, tmp_path, monkeypatch, source, embed_error,
                                                                     code):
    path = scanned_pdf(tmp_path) if source == "scan" else SAMPLES / QUOTE
    worker_a = ctx if embed_error is None else with_embedder(ctx, FailingEmbedder(0, embed_error))
    monkeypatch.setattr(tasks, "_ctx", lambda: worker_a)
    doc_id = upload(ctx, path).document.id

    worker_b_took_the_lease = []
    move = documents._move

    def worker_b_tries_the_lease_as_failed_is_written(conn, doc, to, *rest):
        if to is DocStatus.FAILED:
            with pipeline_steps(ctx.db, doc_id, Lease.INDEX, 1) as worker_b:
                worker_b_took_the_lease.append(worker_b is not None)
        return move(conn, doc, to, *rest)

    monkeypatch.setattr(documents, "_move", worker_b_tries_the_lease_as_failed_is_written)

    tasks.index_document.apply(args=[str(doc_id)])

    assert worker_b_took_the_lease == [False]
    lifecycle = documents.get(ctx.db, doc_id).lifecycle
    assert isinstance(lifecycle, Failed) and lifecycle.failure.code is code
