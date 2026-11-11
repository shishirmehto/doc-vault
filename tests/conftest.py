"""Tests run against the compose Postgres in their own database (vault_test)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from docvault import ai
from docvault import db as dbmod
from docvault.config import Settings
from docvault.context import BlobStore, Ctx
from docvault.domain import DocumentId

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
POLICY = "sample-cgl-policy.pdf"
QUOTE = "sample-renewal-quote.pdf"
ENDORSEMENT = "sample-endorsement-additional-insured.docx"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://vault:vault@localhost:54329/vault_test")


@pytest.fixture(scope="session")
def database_url() -> str:
    name = conninfo_to_dict(TEST_DATABASE_URL)["dbname"]
    with psycopg.connect(make_conninfo(TEST_DATABASE_URL, dbname="postgres"), autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            admin.execute(f'CREATE DATABASE "{name}"')
    dbmod.migrate(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def pool(database_url: str) -> Iterator[dbmod.Db]:
    pool = dbmod.open_pool(database_url, max_size=4)
    yield pool
    pool.close()


@pytest.fixture
def db(pool: dbmod.Db) -> dbmod.Db:
    with pool.connection() as conn:
        conn.execute("TRUNCATE documents, step_runs, llm_calls, chats CASCADE")
    return pool


def scalar(db: dbmod.Db, sql: str, params: tuple = ()) -> object:
    with db.connection() as conn:
        return next(iter(conn.execute(sql, params).fetchone().values()))


@dataclass
class RecordingJobs:
    """Stands in for Celery: records what was enqueued. Tests call the pipeline functions themselves."""

    indexed: list[DocumentId] = field(default_factory=list)
    analyzed: list[DocumentId] = field(default_factory=list)

    def index(self, doc_id: DocumentId) -> None:
        self.indexed.append(doc_id)

    def analyze(self, doc_id: DocumentId) -> None:
        self.analyzed.append(doc_id)


@pytest.fixture
def ctx(db: dbmod.Db, tmp_path: Path) -> Ctx:
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, storage_dir=tmp_path / "blobs",
                        chat_provider="fake", embed_provider="fake", embed_batch_size=2,
                        max_upload_bytes=1024 * 1024, ai_backoff_s=0)
    return Ctx(settings, db, ai.build(settings, db), BlobStore(settings.storage_dir), RecordingJobs())


class Crash(BaseException):
    """Stands in for a killed worker: nothing below Exception handlers sees it."""


def step_rows(db: dbmod.Db, doc_id: DocumentId) -> list[tuple]:
    with db.connection() as conn:
        return [(r["step"], r["attempt"], r["outcome"], r["error_code"]) for r in conn.execute(
            "SELECT step, attempt, outcome, error_code FROM step_runs WHERE document_id = %s ORDER BY id", (doc_id,))]


def advisory_locks(db: dbmod.Db) -> int:
    return scalar(db, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                      " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())")
