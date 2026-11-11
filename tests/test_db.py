from __future__ import annotations

from uuid import uuid4

import pytest

from docvault.db import Lease, pipeline_steps
from docvault.domain import DocumentId, ErrorCode, Step, VaultError

from .conftest import scalar


class Crash(BaseException):
    """Stands in for a killed worker: nothing below Exception handlers sees it."""


def step_rows(db, doc_id):
    with db.connection() as conn:
        return [(r["step"], r["attempt"], r["outcome"], r["error_code"]) for r in conn.execute(
            "SELECT step, attempt, outcome, error_code FROM step_runs WHERE document_id = %s ORDER BY id", (doc_id,))]


def advisory_locks(db) -> int:
    return scalar(db, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")


def test_second_holder_of_the_same_lease_gets_none(db):
    doc_id = DocumentId(uuid4())
    with pipeline_steps(db, doc_id, Lease.INDEX, 1) as first:
        assert first is not None
        with pipeline_steps(db, doc_id, Lease.INDEX, 1) as duplicate:
            assert duplicate is None
        with pipeline_steps(db, doc_id, Lease.ANALYZE, 1) as other_lease:
            assert other_lease is not None
        with pipeline_steps(db, DocumentId(uuid4()), Lease.INDEX, 1) as other_doc:
            assert other_doc is not None
    assert advisory_locks(db) == 0
    with pipeline_steps(db, doc_id, Lease.INDEX, 1) as again:
        assert again is not None


def test_steps_record_ok_failed_and_internal(db):
    doc_id = DocumentId(uuid4())
    with pipeline_steps(db, doc_id, Lease.INDEX, 2) as steps:
        with steps.record(Step.PARSE):
            pass
        with pytest.raises(VaultError), steps.record(Step.EMBED):
            raise VaultError(ErrorCode.AI_UNAVAILABLE, "provider timed out", retryable=True)
        with pytest.raises(KeyError), steps.record(Step.EMBED):
            raise KeyError("a bug")
    assert step_rows(db, doc_id) == [
        ("parse", 2, "ok", None),
        ("embed", 2, "failed", "ai_unavailable"),
        ("embed", 2, "failed", "internal_error"),
    ]


def test_crash_leaves_the_row_open_and_the_next_holder_closes_it(db):
    doc_id = DocumentId(uuid4())
    with pytest.raises(Crash), pipeline_steps(db, doc_id, Lease.INDEX, 1) as steps, steps.record(Step.EMBED):
        raise Crash
    assert step_rows(db, doc_id) == [("embed", 1, None, None)]
    assert advisory_locks(db) == 0

    with pipeline_steps(db, doc_id, Lease.ANALYZE, 1):
        assert step_rows(db, doc_id) == [("embed", 1, None, None)]
    with pipeline_steps(db, doc_id, Lease.INDEX, 1):
        assert step_rows(db, doc_id) == [("embed", 1, "abandoned", None)]
