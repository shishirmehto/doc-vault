"""An optional Redis cache for embeddings and first answers. The app never depends on it.

Cache wraps one redis.Redis client, or none when CACHE_URL is unset. Every call
that fails with a RedisError is a miss or a dropped write. The first failure
logs one warning on docvault.cache and opens a 60 s window in which no call
touches Redis, so an outage costs one warning line a minute and no waiting.
Hit and miss counters live in one Redis hash and count from their first write,
so they survive API restarts but not a cache restart.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

log = logging.getLogger(__name__)

type CacheName = Literal["embedding", "answer"]

STATS_KEY = "docvault:cache:stats"
SKIP = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class Counts:
    hits: int
    misses: int


@dataclass(frozen=True, slots=True)
class CacheStats:
    embedding: Counts
    answer: Counts
    counted_since: datetime | None     # the first count's time; None before any


class Cache:
    """Never a dependency: lookups miss, and writes and counts are dropped, while Redis is off or unreachable."""

    def __init__(self, client: redis.Redis | None, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._client = client
        self._clock = clock
        self._lock = threading.Lock()
        self._skip_until: datetime | None = None

    @classmethod
    def off(cls) -> Cache:
        return cls(None)

    @classmethod
    def from_url(cls, url: str | None, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Cache:
        """A cache on `url`, or off when it is None. One attempt per call: redis-py's default ten retries would
        multiply the 0.5 s timeouts on every call that opens a skip window."""
        if url is None:
            return cls.off()
        return cls(redis.Redis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5,
                                        retry=Retry(NoBackoff(), 0)), clock)

    def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        """The value of each key in order, None for a miss."""
        found = self._call(lambda client: client.mget(keys)) if keys else None
        return [None] * len(keys) if found is None else found

    def set_many(self, items: Mapping[str, bytes], ttl_s: int) -> None:
        def write(client: redis.Redis) -> None:
            pipe = client.pipeline(transaction=False)
            for key, value in items.items():
                pipe.set(key, value, ex=ttl_s)
            pipe.execute()

        if items:
            self._call(write)

    def count(self, name: CacheName, hits: int, misses: int) -> None:
        def write(client: redis.Redis) -> None:
            pipe = client.pipeline(transaction=False)
            pipe.hsetnx(STATS_KEY, "since", self._clock().isoformat())
            pipe.hincrby(STATS_KEY, f"{name}:hits", hits)
            pipe.hincrby(STATS_KEY, f"{name}:misses", misses)
            pipe.execute()

        if hits or misses:
            self._call(write)

    def stats(self) -> CacheStats | None:
        """The counters since their first write, or None when the cache is off or unreachable."""
        fields = self._call(lambda client: client.hgetall(STATS_KEY))
        if fields is None:
            return None
        counts = {key.decode(): value.decode() for key, value in fields.items()}

        def of(name: CacheName) -> Counts:
            return Counts(int(counts.get(f"{name}:hits", 0)), int(counts.get(f"{name}:misses", 0)))

        since = counts.get("since")
        return CacheStats(of("embedding"), of("answer"), None if since is None else datetime.fromisoformat(since))

    def probe(self) -> Literal["ok", "unreachable", "off"]:
        """A direct ping for /health, outside the skip window."""
        if self._client is None:
            return "off"
        try:
            self._client.ping()
        except redis.RedisError:
            return "unreachable"
        return "ok"

    def _call[R](self, call: Callable[[redis.Redis], R]) -> R | None:
        """call(client), or None when the cache is off, inside a skip window, or failing (which opens one)."""
        if self._client is None:
            return None
        with self._lock:
            if self._skip_until is not None and self._clock() < self._skip_until:
                return None
        try:
            return call(self._client)
        except redis.RedisError as exc:
            with self._lock:
                now = self._clock()
                if self._skip_until is None or now >= self._skip_until:
                    self._skip_until = now + SKIP
                    log.warning("cache unreachable; skipping the cache for 60 s: %s", exc)
            return None
