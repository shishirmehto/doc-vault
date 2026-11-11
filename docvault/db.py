"""Connection pool, schema bootstrap, and the per-document lease with its step log.

All SQL lives in the module that owns the table (see schema.sql header).
This file only holds what every module needs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

import psycopg
from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .domain import DocumentId, ErrorCode, Step, StepOutcome, VaultError

type Db = ConnectionPool

SCHEMA = Path(__file__).with_name("schema.sql")
MIGRATE_LOCK = 0x646F637661756C74   # "docvault"; any constant shared by every process works


class Lease(StrEnum):
    """One unit of exclusive work per document. Indexing and insights hold
    separate leases, so a slow summary never blocks a parse."""

    INDEX = "index"
    ANALYZE = "analyze"


LEASE_STEPS: dict[Lease, tuple[Step, ...]] = {
    Lease.INDEX: (Step.PARSE, Step.EMBED),
    Lease.ANALYZE: (Step.ANALYZE,),
}


def migrate(url: str) -> None:
    """Apply schema.sql unless this exact file was applied before. The API and
    every worker process call it at start.

    The advisory lock serialises processes that start together. The recorded
    hash matters as much: CREATE ... IF NOT EXISTS still locks the tables it
    names, so re-running the file beside a worker that is writing a document
    deadlocks with that worker.
    """
    schema = SCHEMA.read_text()
    sha256 = hashlib.sha256(schema.encode()).hexdigest()
    with psycopg.connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATE_LOCK,))
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version"
                     " (sha256 text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        if conn.execute("SELECT 1 FROM schema_version WHERE sha256 = %s", (sha256,)).fetchone():
            return
        conn.execute(schema)
        conn.execute("INSERT INTO schema_version (sha256) VALUES (%s)", (sha256,))


def open_pool(url: str, *, min_size: int = 1, max_size: int = 10) -> Db:
    """Pool whose connections run in autocommit with dict rows and pgvector registered.

    Autocommit is deliberate. Multi-statement checkpoints open an explicit
    `with conn.transaction()`, and nothing ever holds a transaction open
    across a model call. Call migrate() first: registering the vector type
    needs the extension.
    """
    return ConnectionPool(url, min_size=min_size, max_size=max_size, open=True, configure=register_vector,
                          kwargs={"autocommit": True, "row_factory": dict_row})


def encode_cursor(created_at: datetime, row_id: UUID) -> str:
    """The keyset position after (created_at, id) in a newest-first listing. Opaque to clients."""
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{row_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Raises VaultError(INVALID_CURSOR) for anything encode_cursor did not produce."""
    try:
        created_at, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(created_at), UUID(row_id)
    except (ValueError, binascii.Error) as exc:
        raise VaultError(ErrorCode.INVALID_CURSOR, "The cursor is not one this API returned.") from exc


@dataclass(frozen=True, slots=True)
class PipelineSteps:
    """Handed to the lease holder. conn is the lease's own connection; all work
    under the lease runs on it."""

    conn: Connection
    doc_id: DocumentId
    attempt: int

    @contextmanager
    def record(self, name: Step) -> Iterator[None]:
        """Record one attempt of a step in step_runs.

        A normal exit closes the row 'ok'. An Exception closes it 'failed' with
        the VaultError code (INTERNAL for anything else) and re-raises. A
        BaseException (process shutdown, a test's simulated crash) leaves the
        row open, which is what a real crash does; the next lease holder closes
        it 'abandoned'.
        """
        row = self.conn.execute(
            "INSERT INTO step_runs (document_id, step, attempt) VALUES (%s, %s, %s) RETURNING id",
            (self.doc_id, name, self.attempt)).fetchone()
        try:
            yield
        except Exception as exc:
            code = exc.failure.code if isinstance(exc, VaultError) else ErrorCode.INTERNAL
            self._close(row["id"], StepOutcome.FAILED, code)
            raise
        self._close(row["id"], StepOutcome.OK, None)

    def _close(self, row_id: int, outcome: StepOutcome, error_code: ErrorCode | None) -> None:
        self.conn.execute("UPDATE step_runs SET finished_at = now(), outcome = %s, error_code = %s WHERE id = %s",
                          (outcome, error_code, row_id))


@contextmanager
def pipeline_steps(db: Db, doc_id: DocumentId, lease: Lease, attempt: int) -> Iterator[PipelineSteps | None]:
    """Exclusive, crash-safe ownership of one document's indexing or insights.

    Opens a dedicated connection and calls pg_try_advisory_lock on
    "<lease>:<doc_id>". Yields None when another worker holds the lock: the
    caller returns at once and the duplicate delivery is a no-op. Otherwise
    closes the step rows a crashed holder left open as 'abandoned' (holding
    the lock proves that holder's connection is gone) and yields the recorder.

    The lock is session-scoped. It is released on exit, and Postgres releases
    it when a killed worker's connection drops. There is no expiry to tune.
    """
    with psycopg.connect(db.conninfo, autocommit=True, row_factory=dict_row) as conn:
        register_vector(conn)
        locked = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked",
                              (f"{lease}:{doc_id}",)).fetchone()["locked"]
        if not locked:
            yield None
            return
        conn.execute(
            "UPDATE step_runs SET finished_at = now(), outcome = %s "
            "WHERE document_id = %s AND step = ANY(%s) AND finished_at IS NULL",
            (StepOutcome.ABANDONED, doc_id, list(LEASE_STEPS[lease])))
        try:
            yield PipelineSteps(conn, doc_id, attempt)
        finally:
            # Closing the connection also releases the lock, but only once the server notices.
            # Unlocking first means the next delivery never loses a race against that cleanup.
            if not conn.broken:
                conn.execute("SELECT pg_advisory_unlock_all()")
