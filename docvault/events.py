"""Live document events: Postgres NOTIFY in, one read per changed document, fanned out to every open stream.

The *_notify triggers in schema.sql send a document id on channel `documents` after every commit that may change
that document's GET /documents/{id} body. listen() holds the one LISTEN connection of an API process and nudges
the Hub with each id. The Hub reads each nudged document once per burst and queues the result on every Feed;
api.py turns a Feed into server-sent events.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, NoReturn
from uuid import UUID

import psycopg

from . import documents
from .db import Db
from .domain import Document, DocumentId, ErrorCode, VaultError

log = logging.getLogger(__name__)

MAX_STREAMS = 100
PROBE_S = 15.0
BACKOFF_FIRST_S = 0.5
BACKOFF_MAX_S = 30.0


@dataclass(frozen=True, slots=True)
class Changed:
    doc: Document


@dataclass(frozen=True, slots=True)
class Deleted:
    doc_id: DocumentId


@dataclass(frozen=True, slots=True)
class Resync:
    """Events may have been missed: reload the whole list."""


type Event = Changed | Deleted | Resync


def read(db: Db, doc_id: DocumentId) -> Changed | Deleted:
    try:
        return Changed(documents.get(db, doc_id))
    except VaultError as exc:
        if exc.failure.code is not ErrorCode.DOCUMENT_NOT_FOUND:
            raise
        return Deleted(doc_id)


class Feed:
    """The events queued for one open stream, at most hub.backlog of them."""

    def __init__(self, hub: Hub) -> None:
        self._hub = hub
        self._events: deque[Event] = deque()
        self._queued = asyncio.Event()

    def _put(self, event: Event) -> None:
        if len(self._events) >= self._hub.backlog:
            self._events.clear()
            event = Resync()
        self._events.append(event)
        self._queued.set()

    async def next(self) -> Event | None:
        """The oldest queued event, or None once hub.keepalive_s passes with nothing queued."""
        if not self._events:
            self._queued.clear()
            try:
                async with asyncio.timeout(self._hub.keepalive_s):
                    await self._queued.wait()
            except TimeoutError:
                return None
        return self._events.popleft()

    def close(self) -> None:
        self._hub._feeds.discard(self)


class Hub:
    """The open feeds of one API process and the reads that fill them.

    A nudged id waits window_s, so a burst of commits for one document costs one read. The one consumer, run(),
    reads due ids in order and broadcasts each result, so frames for one document stay in commit order and the
    process holds at most one pooled connection for events however many streams are open.

    Every method runs on the event loop thread, and only the read itself runs in a worker thread, so the hub's
    state needs no locks.
    """

    def __init__(self, read: Callable[[DocumentId], Changed | Deleted], *, cap: int = MAX_STREAMS,
                 window_s: float = 0.1, keepalive_s: float = 15.0, backlog: int = 1000) -> None:
        self._read = read
        self._cap = cap
        self._window_s = window_s
        self.keepalive_s = keepalive_s
        self.backlog = backlog
        self._feeds: set[Feed] = set()
        self._pending: set[DocumentId] = set()
        self._due: asyncio.Queue[DocumentId] = asyncio.Queue()

    @property
    def open(self) -> int:
        return len(self._feeds)

    def nudge(self, payload: str) -> None:
        try:
            doc_id = DocumentId(UUID(payload))
        except ValueError:
            return
        if doc_id in self._pending:
            return
        self._pending.add(doc_id)
        asyncio.get_running_loop().call_later(self._window_s, self._due.put_nowait, doc_id)

    def resync(self) -> None:
        self._broadcast(Resync())

    def subscribe(self) -> Feed:
        if len(self._feeds) >= self._cap:
            raise VaultError(ErrorCode.TOO_MANY_STREAMS, "Too many live document streams are open. Try again shortly.",
                             retryable=True, details={"retry_after_s": 5})
        feed = Feed(self)
        self._feeds.add(feed)
        return feed

    async def run(self) -> NoReturn:
        while True:
            doc_id = await self._due.get()
            self._pending.discard(doc_id)
            try:
                event: Event = await asyncio.to_thread(self._read, doc_id)
            except Exception:
                log.exception("Reading document %s for its live event failed", doc_id)
                event = Resync()
            self._broadcast(event)

    def _broadcast(self, event: Event) -> None:
        for feed in self._feeds:
            feed._put(event)


async def listen(url: str, hub: Hub) -> NoReturn:
    """LISTEN documents and nudge the hub with every payload, reconnecting for as long as the process runs."""
    backoff = BACKOFF_FIRST_S
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(url, autocommit=True,
                                                             application_name="docvault-events") as conn:
                await conn.execute("LISTEN documents")
                backoff = BACKOFF_FIRST_S
                # Notifications sent while no connection listened are lost, so every client reloads its list.
                hub.resync()
                while True:
                    async for note in conn.notifies(timeout=PROBE_S):
                        hub.nudge(note.payload)
                    # On a half-open TCP connection the probe would otherwise wait for the kernel's timeout.
                    async with asyncio.timeout(PROBE_S):
                        await conn.execute("SELECT 1")
        except (psycopg.Error, TimeoutError):
            log.warning("The live events connection to Postgres failed; reconnecting in %.1f s", backoff,
                        exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)


def _report_stop(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("%s stopped; live document events are off until the API restarts", task.get_name(),
                  exc_info=task.exception())


@asynccontextmanager
async def running(url: str, db: Db, **hub_kwargs: Any) -> AsyncIterator[Hub]:
    """A hub with its listener and reader running for the duration of the block."""
    hub = Hub(partial(read, db), **hub_kwargs)
    tasks = [asyncio.create_task(listen(url, hub), name="events.listen"),
             asyncio.create_task(hub.run(), name="events.Hub.run")]
    for task in tasks:
        task.add_done_callback(_report_stop)
    try:
        yield hub
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
