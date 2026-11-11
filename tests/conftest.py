"""Tests run against the compose Postgres in their own database (vault_test)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from docvault import db as dbmod

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
