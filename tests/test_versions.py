"""Document versions: which upload joins which family, under which number, and how the API lists them.

Fake provider, real Postgres. The race test holds one upload's insert open on its own connection, so the collision
it checks happens on every run rather than when two threads happen to meet.
"""

from __future__ import annotations

import dataclasses
import io
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from docvault import db as dbmod
from docvault import documents
from docvault.domain import Created, Document, DocumentId, ErrorCode, Existing, FamilyId, VaultError, Version

from .conftest import scalar


def upload(ctx, text: str, name: str, replaces: DocumentId | None = None):
    return documents.upload(ctx, io.BytesIO(text.encode()), name, replaces=replaces)


def created(outcome) -> Document:
    assert isinstance(outcome, Created)
    return outcome.document


def test_new_bytes_under_a_stored_name_become_its_next_version(ctx):
    first = created(upload(ctx, "Premium: $1,000", "terms.txt"))
    second = created(upload(ctx, "Premium: $1,200", "TERMS.txt"))

    assert first.version == Version(FamilyId(first.id), 1, 1)
    assert second.version == Version(FamilyId(first.id), 2, 2)
    assert second.filename == "TERMS.txt"
    assert documents.get(ctx.db, first.id).version == Version(FamilyId(first.id), 1, 2)
    assert ctx.jobs.indexed == [first.id, second.id]


def test_the_same_bytes_again_are_the_existing_document_not_a_version(ctx):
    first = created(upload(ctx, "Premium: $1,000", "terms.txt"))
    again = upload(ctx, "Premium: $1,000", "terms.txt")

    assert isinstance(again, Existing)
    assert again.document.id == first.id
    assert again.document.version == Version(FamilyId(first.id), 1, 1)
    assert scalar(ctx.db, "SELECT count(*) FROM documents") == 1


def test_replaces_makes_a_file_with_another_name_the_next_version(ctx):
    policy = created(upload(ctx, "Policy period: 2025", "policy-2025.txt"))
    renewal = created(upload(ctx, "Policy period: 2026", "renewal.txt", replaces=policy.id))

    assert (renewal.filename, renewal.version) == ("renewal.txt", Version(FamilyId(policy.id), 2, 2))


def test_replaces_an_unknown_document_is_not_found_and_stores_nothing(ctx):
    with pytest.raises(VaultError) as raised:
        upload(ctx, "Policy period: 2026", "renewal.txt", replaces=DocumentId(uuid4()))

    assert raised.value.failure.code is ErrorCode.DOCUMENT_NOT_FOUND
    assert scalar(ctx.db, "SELECT count(*) FROM documents") == 0
    assert [p for p in ctx.blobs.root.rglob("*") if p.is_file()] == []


def test_bytes_stored_before_win_over_replaces(ctx):
    a = created(upload(ctx, "Limit: $1,000,000", "a.txt"))
    b = created(upload(ctx, "Limit: $2,000,000", "b.txt"))

    again = upload(ctx, "Limit: $1,000,000", "a-renamed.txt", replaces=b.id)

    assert isinstance(again, Existing) and again.document.id == a.id
    assert [d.version.number for d in documents.versions(ctx.db, b.id)] == [1]


def test_a_name_two_families_share_joins_the_family_of_the_newest(ctx):
    older = created(upload(ctx, "Deductible: $500", "schedule.txt"))
    newer = created(upload(ctx, "Deductible: $750", "other.txt"))
    with ctx.db.connection() as conn:   # as a database from before versions holds them: same name, two families
        conn.execute("UPDATE documents SET filename = 'schedule.txt' WHERE id = %s", (newer.id,))

    third = created(upload(ctx, "Deductible: $1,000", "Schedule.txt"))

    assert third.version == Version(FamilyId(newer.id), 2, 2)
    assert documents.get(ctx.db, older.id).version == Version(FamilyId(older.id), 1, 1)


@dataclasses.dataclass
class HeldPool:
    """The pool, except that the first connection taken runs one transaction and commits it only on `release`."""

    pool: dbmod.Db
    inserted: threading.Event = dataclasses.field(default_factory=threading.Event)
    release: threading.Event = dataclasses.field(default_factory=threading.Event)
    held: bool = False

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        with self.pool.connection() as conn:
            if self.held:
                yield conn
                return
            self.held = True
            conn.autocommit = False
            try:
                yield conn
                self.inserted.set()
                assert self.release.wait(10)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True


def blocked_inserts(db: dbmod.Db) -> int:
    return scalar(db, "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                      " AND wait_event_type = 'Lock' AND query LIKE '%%INSERT INTO documents%%'")


def test_two_concurrent_uploads_into_one_family_get_versions_2_and_3(ctx):
    first = created(upload(ctx, "Premium: $1,000", "terms.txt"))
    held = HeldPool(ctx.db)
    results: dict[str, object] = {}

    def run(key: str, worker_ctx, text: str) -> None:
        try:
            results[key] = upload(worker_ctx, text, "terms.txt")
        except BaseException as exc:   # the assertion below names it
            results[key] = exc

    a = threading.Thread(target=run, args=("a", dataclasses.replace(ctx, db=held), "Premium: $1,100"))
    a.start()
    assert held.inserted.wait(10), "upload A never inserted"
    b = threading.Thread(target=run, args=("b", ctx, "Premium: $1,200"))
    b.start()
    deadline = time.monotonic() + 10
    while blocked_inserts(ctx.db) == 0:   # B took version 2 too and waits on A's uncommitted row
        assert time.monotonic() < deadline, "upload B never waited on A's version"
        time.sleep(0.01)
    held.release.set()
    a.join(10)
    b.join(10)

    numbers = {key: created(outcome).version.number for key, outcome in results.items()}
    assert numbers == {"a": 2, "b": 3}
    assert [d.version for d in documents.versions(ctx.db, first.id)] == [
        Version(FamilyId(first.id), n, 3) for n in (1, 2, 3)]


def test_deleting_version_1_keeps_version_2s_number_and_the_next_upload_takes_3(ctx):
    first = created(upload(ctx, "Premium: $1,000", "terms.txt"))
    second = created(upload(ctx, "Premium: $1,200", "terms.txt"))

    documents.delete(ctx, first.id)

    assert documents.get(ctx.db, second.id).version == Version(FamilyId(first.id), 2, 2)
    assert [d.id for d in documents.versions(ctx.db, second.id)] == [second.id]
    third = created(upload(ctx, "Premium: $1,300", "terms.txt"))
    assert third.version == Version(FamilyId(first.id), 3, 3)


def post(client, text: str, name: str, **params):
    return client.post("/documents", params=params, files={"file": (name, text.encode())})


def test_the_api_lists_versions_and_only_the_latest_on_request(client):
    v1 = post(client, "Premium: $1,000", "terms.txt").json()
    other = post(client, "Limit: $1,000,000", "limits.txt").json()
    v2 = post(client, "Premium: $1,200", "terms.txt").json()

    assert {k: v2[k] for k in ("family_id", "version", "latest_version")} == {
        "family_id": v1["id"], "version": 2, "latest_version": 2}
    assert v2["links"]["versions"] == f"/documents/{v2['id']}/versions"
    every = client.get("/documents").json()["items"]
    latest = client.get("/documents", params={"latest": "true"}).json()["items"]
    assert [d["id"] for d in every] == [v2["id"], other["id"], v1["id"]]
    assert [d["id"] for d in latest] == [v2["id"], other["id"]]
    assert [(d["id"], d["version"], d["latest_version"]) for d in client.get(f"/documents/{v2['id']}/versions").json()[
        "items"]] == [(v1["id"], 1, 2), (v2["id"], 2, 2)]


def test_the_upload_route_takes_replaces(client):
    policy = post(client, "Policy period: 2025", "policy-2025.txt").json()

    renewal = post(client, "Policy period: 2026", "renewal.txt", replaces=policy["id"])
    unknown = post(client, "Policy period: 2027", "renewal.txt", replaces=str(uuid4()))
    malformed = post(client, "Policy period: 2027", "renewal.txt", replaces="not-an-id")

    assert renewal.status_code == 202
    assert (renewal.json()["family_id"], renewal.json()["version"]) == (policy["id"], 2)
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "document_not_found")
    assert (malformed.status_code, malformed.json()["error"]["code"]) == (422, "invalid_request")
    assert client.get(f"/documents/{uuid4()}/versions").json()["error"]["code"] == "document_not_found"


TERMS = ("Named Insured:\nExample Contractors LLC\n\nPolicy Number:\nTERMS-0001\n\n"
         "Property Damage Deductible:\n{}\n")


def test_a_comparison_of_two_versions_names_each_version_and_marks_the_changed_field(client, ctx):
    v1 = created(upload(ctx, TERMS.format("$2,500 per occurrence"), "terms.txt"))
    v2 = created(upload(ctx, TERMS.format("$5,000 per occurrence"), "terms.txt"))
    for doc in (v1, v2):
        documents.run_index(ctx, doc.id)

    body = client.post("/comparisons", json={"document_ids": [str(v1.id), str(v2.id)]}).json()
    with ctx.db.connection() as conn:   # as a comparison stored before versions holds its columns
        conn.execute("UPDATE comparisons SET documents = (SELECT jsonb_agg(doc - 'version') FROM"
                     " jsonb_array_elements(documents) AS doc) WHERE id = %s", (body["id"],))
    older = client.get(f"/comparisons/{body['id']}").json()

    assert [(d["filename"], d["version"]) for d in body["documents"]] == [("terms.txt", 1), ("terms.txt", 2)]
    assert [(row["field"], [c["value"] for c in row["cells"]]) for row in body["rows"] if row["differs"]] == [
        ("Property Damage Deductible", ["$2,500 per occurrence", "$5,000 per occurrence"])]
    assert [d["version"] for d in older["documents"]] == [None, None]


@pytest.fixture
def scratch_url(database_url: str) -> Iterator[str]:
    name = conninfo_to_dict(database_url)["dbname"] + "_versions_upgrade"
    admin_url = make_conninfo(database_url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_conninfo(database_url, dbname=name)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_migrate_makes_each_existing_document_its_own_family_at_version_1(scratch_url):
    dbmod.migrate(scratch_url)
    with psycopg.connect(scratch_url, autocommit=True) as conn:
        # Back to the documents table as it was before versions, then rows written by that code.
        conn.execute("DROP TRIGGER documents_notify ON documents")
        conn.execute("DROP INDEX documents_family_version, documents_filename")
        conn.execute("ALTER TABLE documents DROP COLUMN family_id, DROP COLUMN version")
        conn.execute("DELETE FROM schema_version")
        ids = [row[0] for row in conn.execute(
            "INSERT INTO documents (sha256, filename, kind, size_bytes) SELECT sha256(i::text::bytea), 'terms.txt',"
            " 'txt', 10 FROM generate_series(1, 3) AS i RETURNING id")]

    dbmod.migrate(scratch_url)

    with psycopg.connect(scratch_url, autocommit=True) as conn:
        rows = conn.execute("SELECT id, family_id, version FROM documents").fetchall()
        assert sorted(rows) == sorted((doc_id, doc_id, 1) for doc_id in ids)
        assert conn.execute("SELECT is_nullable FROM information_schema.columns"
                            " WHERE table_name = 'documents' AND column_name = 'family_id'").fetchone() == ("NO",)
        with pytest.raises(psycopg.errors.UniqueViolation) as raised:
            conn.execute("INSERT INTO documents (sha256, filename, kind, size_bytes, family_id)"
                         " VALUES ('\\x01', 'terms.txt', 'txt', 1, %s)", (ids[0],))
        assert raised.value.diag.constraint_name == "documents_family_version"
