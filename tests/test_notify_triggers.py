"""Every step ends at a marker notification: Postgres delivers notifications in commit order, so whatever a step sent arrives
before the marker sent after it, and a missing id is a real absence rather than a timeout.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import Connection


@dataclass
class Channel:
    listener: Connection
    writer: Connection

    def sent(self, sql: str, params: tuple = (), *, rollback: bool = False) -> list[str]:
        if rollback:
            with self.writer.transaction(force_rollback=True):
                self.writer.execute(sql, params)
        else:
            self.writer.execute(sql, params)
        marker = f"marker {uuid4()}"
        self.writer.execute("SELECT pg_notify('documents', %s)", (marker,))
        payloads: list[str] = []
        for note in self.listener.notifies(timeout=5):
            if note.payload == marker:
                return payloads
            payloads.append(note.payload)
        raise AssertionError("the marker notification never arrived")

    def counts(self, doc_id: UUID, sql: str, params: tuple = ()) -> tuple[int, int]:
        rolled_back = self.sent(sql, params, rollback=True).count(str(doc_id))
        return rolled_back, self.sent(sql, params).count(str(doc_id))

    def new_document(self, chunks: int = 0) -> UUID:
        doc_id = uuid4()
        with self.writer.transaction():
            self.writer.execute("INSERT INTO documents (id, sha256, filename, kind, size_bytes, family_id)"
                                " VALUES (%s, %s, 'notes.txt', 'txt', 1, %s)", (doc_id, os.urandom(32), doc_id))
            for ord in range(chunks):
                self.writer.execute("INSERT INTO chunks (document_id, ord, text, page_map)"
                                    " VALUES (%s, %s, 'text', '[[0, 1]]')", (doc_id, ord))
        self.sent("SELECT 1")
        return doc_id


@pytest.fixture
def channel(database_url: str) -> Iterator[Channel]:
    with (psycopg.connect(database_url, autocommit=True) as listener,
          psycopg.connect(database_url, autocommit=True) as writer):
        listener.execute("LISTEN documents")
        yield Channel(listener, writer)


def test_a_document_insert_update_and_delete_each_notify_on_commit_only(channel):
    doc_id = uuid4()

    inserted = channel.counts(doc_id, "INSERT INTO documents (id, sha256, filename, kind, size_bytes, family_id)"
                                      " VALUES (%s, %s, 'notes.txt', 'txt', 1, %s)", (doc_id, os.urandom(32), doc_id))
    updated = channel.counts(doc_id, "UPDATE documents SET status = 'parsing' WHERE id = %s", (doc_id,))
    deleted = channel.counts(doc_id, "DELETE FROM documents WHERE id = %s", (doc_id,))

    assert (inserted, updated, deleted) == ((0, 1), (0, 1), (0, 1))


def test_an_insights_insert_update_and_delete_each_notify_on_commit_only(channel):
    doc_id = channel.new_document()

    inserted = channel.counts(doc_id, "INSERT INTO document_insights (document_id, status, prompt_version, model,"
                                      " error_code, error_message, error_retryable)"
                                      " VALUES (%s, 'failed', 'v1', 'fake', 'internal_error', 'Failed.', true)",
                              (doc_id,))
    updated = channel.counts(doc_id, "UPDATE document_insights SET model = 'other' WHERE document_id = %s", (doc_id,))
    deleted = channel.counts(doc_id, "DELETE FROM document_insights WHERE document_id = %s", (doc_id,))
    channel.sent("DELETE FROM documents WHERE id = %s", (doc_id,))

    assert (inserted, updated, deleted) == ((0, 1), (0, 1), (0, 1))


def test_one_update_across_two_documents_chunks_notifies_each_id_once_on_commit(channel):
    first, second = channel.new_document(chunks=3), channel.new_document(chunks=4)
    update = "UPDATE chunks SET page_map = '[[0, 2]]' WHERE document_id = ANY(%s)"

    rolled_back = channel.sent(update, ([first, second],), rollback=True)
    committed = channel.sent(update, ([first, second],))
    channel.sent("DELETE FROM documents WHERE id = ANY(%s)", ([first, second],))

    assert [rolled_back.count(str(first)), rolled_back.count(str(second))] == [0, 0]
    assert [committed.count(str(first)), committed.count(str(second))] == [1, 1]


def test_closing_an_embed_step_ok_notifies_on_commit_and_closing_a_parse_step_does_not(channel):
    doc_id = uuid4()
    with channel.writer.transaction():
        for step in ("parse", "embed"):
            channel.writer.execute("INSERT INTO step_runs (document_id, step, attempt) VALUES (%s, %s, 1)",
                                   (doc_id, step))
    close = "UPDATE step_runs SET finished_at = now(), outcome = 'ok' WHERE document_id = %s AND step = %s"

    parse_closed = channel.sent(close, (doc_id, "parse")).count(str(doc_id))
    embed_closed = channel.counts(doc_id, close, (doc_id, "embed"))
    channel.writer.execute("DELETE FROM step_runs WHERE document_id = %s", (doc_id,))

    assert (parse_closed, embed_closed) == (0, (0, 1))


def test_a_version_inserted_or_deleted_also_notifies_the_other_versions_but_an_update_does_not(channel):
    first, second = channel.new_document(), uuid4()
    insert = ("INSERT INTO documents (id, sha256, filename, kind, size_bytes, family_id, version)"
              " VALUES (%s, %s, 'notes.txt', 'txt', 1, %s, 2)")

    on_insert = channel.sent(insert, (second, os.urandom(32), first))
    on_update = channel.sent("UPDATE documents SET status = 'parsing' WHERE id = %s", (second,))
    rolled_back = channel.sent("DELETE FROM documents WHERE id = %s", (second,), rollback=True)
    on_delete = channel.sent("DELETE FROM documents WHERE id = %s", (second,))
    channel.sent("DELETE FROM documents WHERE id = %s", (first,))

    assert sorted(on_insert) == sorted([str(first), str(second)])
    assert on_update == [str(second)]
    assert rolled_back == []
    assert sorted(on_delete) == sorted([str(first), str(second)])
