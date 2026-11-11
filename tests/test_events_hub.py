"""The hub behind GET /events/documents: coalesced reads fanned out to every feed. No database: the read is injected."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from docvault.domain import (
    DocKind,
    Document,
    DocumentId,
    ErrorCode,
    FamilyId,
    InsightsPending,
    Queued,
    VaultError,
    Version,
)
from docvault.events import Changed, Event, Feed, Hub, Resync

A, B, C, D = (DocumentId(uuid4()) for _ in range(4))


def document(doc_id: DocumentId) -> Document:
    return Document(id=doc_id, filename="notes.txt", kind=DocKind.TXT, size_bytes=1, lifecycle=Queued(None),
                    insights=InsightsPending(), created_at=datetime(2026, 11, 11, tzinfo=UTC), processing_ms=None,
                    version=Version(FamilyId(doc_id), 1, 1))


class Reads:
    """The injected read: records every id it is asked for, in order."""

    def __init__(self, fail: DocumentId | None = None) -> None:
        self.ids: list[DocumentId] = []
        self.fail = fail

    def __call__(self, doc_id: DocumentId) -> Changed:
        self.ids.append(doc_id)
        if doc_id == self.fail:
            raise RuntimeError("the database went away")
        return Changed(document(doc_id))


def drive[T](scenario: Callable[[Hub], Awaitable[T]], read: Callable, **hub_kwargs) -> T:
    """Runs the scenario against a hub whose consumer is running."""
    async def main() -> T:
        hub = Hub(read, **{"window_s": 0.05, "keepalive_s": 0.3, **hub_kwargs})
        consumer = asyncio.create_task(hub.run())
        try:
            return await scenario(hub)
        finally:
            consumer.cancel()
    return asyncio.run(main())


async def until_quiet(feed: Feed) -> list[Event | None]:
    """Every event the feed yields, ending with the first None (keepalive_s with nothing queued)."""
    seen: list[Event | None] = [await feed.next()]
    while seen[-1] is not None:
        seen.append(await feed.next())
    return seen


def test_a_burst_of_nudges_for_two_documents_reads_each_once_and_reaches_every_feed():
    reads = Reads()

    async def scenario(hub: Hub) -> list[list[Event | None]]:
        feeds = [hub.subscribe(), hub.subscribe()]
        for n in range(50):
            hub.nudge(str(A))
            if n < 30:
                hub.nudge(str(B))
        return [await until_quiet(feed) for feed in feeds]

    seen = drive(scenario, reads)

    assert reads.ids == [A, B]
    assert seen == [[Changed(document(A)), Changed(document(B)), None]] * 2


def test_a_nudge_while_its_document_is_being_read_reads_it_again_afterwards():
    started, release = threading.Event(), threading.Event()
    reads = Reads()

    def slow_read(doc_id: DocumentId) -> Changed:
        started.set()
        release.wait(5)
        return reads(doc_id)

    async def scenario(hub: Hub) -> list[Event | None]:
        feed = hub.subscribe()
        hub.nudge(str(A))
        assert await asyncio.to_thread(started.wait, 5), "the first read started"
        hub.nudge(str(A))
        release.set()
        return await until_quiet(feed)

    seen = drive(scenario, slow_read)

    assert reads.ids == [A, A]
    assert seen == [Changed(document(A)), Changed(document(A)), None]


def test_a_payload_that_is_not_a_document_id_is_ignored():
    reads = Reads()

    async def scenario(hub: Hub) -> list[Event | None]:
        feed = hub.subscribe()
        hub.nudge("not-a-uuid")
        hub.nudge(str(A))
        return await until_quiet(feed)

    seen = drive(scenario, reads)

    assert (reads.ids, seen) == ([A], [Changed(document(A)), None])


def test_a_read_that_raises_sends_resync_and_later_reads_still_run():
    reads = Reads(fail=A)

    async def scenario(hub: Hub) -> list[Event | None]:
        feed = hub.subscribe()
        hub.nudge(str(A))
        first = await feed.next()
        hub.nudge(str(B))
        return [first, *await until_quiet(feed)]

    assert drive(scenario, reads) == [Resync(), Changed(document(B)), None]


def test_the_cap_refuses_another_feed_until_one_closes_and_closing_twice_frees_one_slot():
    hub = Hub(Reads(), cap=2)
    first, _second = hub.subscribe(), hub.subscribe()

    with pytest.raises(VaultError) as refused:
        hub.subscribe()
    first.close()
    first.close()
    open_after_close = hub.open
    hub.subscribe()

    assert (refused.value.failure.code, refused.value.failure.retryable, refused.value.details) == (
        ErrorCode.TOO_MANY_STREAMS, True, {"retry_after_s": 5})
    assert (open_after_close, hub.open) == (1, 2)


def test_a_feed_whose_backlog_overflows_gets_one_resync_in_place_of_it():
    reads = Reads()

    async def scenario(hub: Hub) -> tuple[list[Event | None], list[Event | None]]:
        reader, idle = hub.subscribe(), hub.subscribe()
        for doc_id in (A, B, C, D):
            hub.nudge(str(doc_id))
        return await until_quiet(reader), await until_quiet(idle)

    reader_saw, idle_saw = drive(scenario, reads, backlog=3)

    assert reader_saw == [Changed(document(doc_id)) for doc_id in (A, B, C, D)] + [None]
    assert idle_saw == [Resync(), None]


def test_next_returns_none_after_keepalive_with_nothing_queued_and_the_event_once_one_is():
    async def scenario(hub: Hub) -> tuple[Event | None, float, Event | None]:
        feed = hub.subscribe()
        loop = asyncio.get_running_loop()
        start = loop.time()
        idle = await feed.next()
        waited = loop.time() - start
        hub.resync()
        return idle, waited, await feed.next()

    idle, waited, queued = drive(scenario, Reads(), keepalive_s=0.2)

    assert (idle, queued) == (None, Resync())
    assert 0.2 <= waited < 1, waited
