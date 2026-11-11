"""Per-client request budgets for the two routes that cost money or worker time: upload and ask.

A token bucket per (client, action) in this process's memory. Each bucket holds
one minute's allowance, refills continuously, and a request spends one token.
With several API processes each keeps its own buckets, so the effective limit
is the per-process limit times the process count; a shared limit moves the
buckets to Redis with the same take() contract.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Literal

type Action = Literal["upload", "ask"]

# Past this many buckets, buckets idle for a minute (full again, so equal to a fresh one) are dropped.
MAX_BUCKETS = 10_000


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()   # sync routes run on a thread pool
        self._buckets: dict[tuple[str, Action], tuple[float, float]] = {}   # tokens, last refill

    def take(self, client: str, action: Action, per_minute: int) -> int | None:
        """Spend one token. None when the request may go ahead, otherwise whole seconds until a token is back."""
        now = self._clock()
        with self._lock:
            if len(self._buckets) > MAX_BUCKETS:
                self._buckets = {key: bucket for key, bucket in self._buckets.items() if now - bucket[1] < 60}
            tokens, last = self._buckets.get((client, action), (per_minute, now))
            tokens = min(per_minute, tokens + (now - last) * per_minute / 60)
            if tokens >= 1:
                self._buckets[(client, action)] = (tokens - 1, now)
                return None
            self._buckets[(client, action)] = (tokens, now)
            return math.ceil(round((1 - tokens) * 60 / per_minute, 3))   # to the ms first: refill sums are inexact
