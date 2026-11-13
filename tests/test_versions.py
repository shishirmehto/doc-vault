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

from docvault import db as dbmod
from docvault import documents
from docvault.context import BlobStore
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


def test_replaces_a_document_deleted_while_the_upload_is_stored_is_not_found_and_stores_nothing(ctx):
    target = created(upload(ctx, "Policy period: 2025", "policy-2025.txt"))

    class DeletedDuringPut(BlobStore):
        def put(self, stream, *, max_bytes):
            documents.delete(ctx, target.id)
            return super().put(stream, max_bytes=max_bytes)

    racing = dataclasses.replace(ctx, blobs=DeletedDuringPut(ctx.blobs.root))
    with pytest.raises(VaultError) as raised:
        upload(racing, "Policy period: 2026", "renewal.txt", replaces=target.id)

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
    with ctx.db.connection() as conn:
        conn.execute("UPDATE documents SET filename = 'schedule.txt' WHERE id = %s", (newer.id,))

    third = created(upload(ctx, "Deductible: $1,000", "Schedule.txt"))

    assert third.version == Version(FamilyId(newer.id), 2, 2)
    assert documents.get(ctx.db, older.id).version == Version(FamilyId(older.id), 1, 1)


@dataclasses.dataclass
class HeldPool:
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


def race_for_version_2(ctx) -> dict[str, object]:
    """Upload A takes version 2 and holds its transaction open; upload B takes version 2 too, waits on A's row, and
    finds the number taken once A commits."""
    held = HeldPool(ctx.db)
    results: dict[str, object] = {}

    def run(key: str, worker_ctx, text: str) -> None:
        try:
            results[key] = upload(worker_ctx, text, "terms.txt")
        except BaseException as exc:
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
    return results


def stored_blobs(ctx) -> set[str]:
    return {path.name for path in ctx.blobs.root.rglob("*") if path.is_file()}


def stored_shas(ctx) -> set[str]:
    with ctx.db.connection() as conn:
        return {row["sha"] for row in conn.execute("SELECT encode(sha256, 'hex') AS sha FROM documents")}


def test_two_concurrent_uploads_into_one_family_get_versions_2_and_3(ctx):
    first = created(upload(ctx, "Premium: $1,000", "terms.txt"))

    results = race_for_version_2(ctx)

    numbers = {key: created(outcome).version.number for key, outcome in results.items()}
    assert numbers == {"a": 2, "b": 3}
    assert [d.version for d in documents.versions(ctx.db, first.id)] == [
        Version(FamilyId(first.id), n, 3) for n in (1, 2, 3)]


def test_an_upload_that_loses_every_version_race_is_a_retryable_conflict_and_keeps_no_blob(ctx, monkeypatch):
    monkeypatch.setattr(documents, "VERSION_ATTEMPTS", 1)
    first = created(upload(ctx, "Premium: $1,000", "terms.txt"))

    results = race_for_version_2(ctx)

    assert created(results["a"]).version.number == 2
    lost = results["b"]
    assert isinstance(lost, VaultError)
    assert (lost.failure.code, lost.failure.retryable) == (ErrorCode.VERSION_CONFLICT, True)
    assert [d.version.number for d in documents.versions(ctx.db, first.id)] == [1, 2]
    assert stored_blobs(ctx) == stored_shas(ctx)
    assert len(stored_blobs(ctx)) == 2


def test_the_upload_route_answers_a_lost_version_race_with_a_retryable_409(client, ctx, monkeypatch):
    post(client, "Premium: $1,000", "terms.txt")
    monkeypatch.setattr(documents, "VERSION_ATTEMPTS", 0)

    lost = post(client, "Premium: $1,200", "terms.txt")

    error = lost.json()["error"]
    assert (lost.status_code, error["code"], error["retryable"]) == (409, "version_conflict", True)
    assert stored_blobs(ctx) == stored_shas(ctx)
    assert len(stored_blobs(ctx)) == 1


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
    with ctx.db.connection() as conn:
        conn.execute("UPDATE comparisons SET documents = (SELECT jsonb_agg(doc - 'version') FROM"
                     " jsonb_array_elements(documents) AS doc) WHERE id = %s", (body["id"],))
    older = client.get(f"/comparisons/{body['id']}").json()

    assert [(d["filename"], d["version"]) for d in body["documents"]] == [("terms.txt", 1), ("terms.txt", 2)]
    assert [(row["field"], [c["value"] for c in row["cells"]]) for row in body["rows"] if row["differs"]] == [
        ("Property Damage Deductible", ["$2,500 per occurrence", "$5,000 per occurrence"])]
    assert [d["version"] for d in older["documents"]] == [None, None]
