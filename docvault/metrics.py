"""Metrics, derived at read time. Writes nothing; there are no counters to drift.

Each figure comes from exactly one recorded source:

  document counts, kinds, pages, bytes, failures  <- documents (current state)
  insights states                                 <- document_insights LEFT JOIN documents
  step durations, failures, retries, crashes      <- step_runs (append-only)
  time to ready, queue wait                       <- step_runs joined to documents.created_at
  tokens, cost, provider latency and errors       <- llm_calls (append-only)
  chat outcomes and end-to-end latency            <- chat_turns

At thousands of documents these aggregates scan tens of thousands of rows
and run in milliseconds. Past a few million step_runs or llm_calls rows,
the same queries become materialized views refreshed by beat (Tradeoffs).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .db import Db
from .domain import DocKind, DocStatus, ErrorCode, Purpose, Step, TurnOutcome


@dataclass(frozen=True, slots=True)
class Percentiles:
    p50: int | None
    p95: int | None


@dataclass(frozen=True, slots=True)
class DocumentStats:
    total: int
    by_status: dict[DocStatus, int]
    by_kind: dict[DocKind, int]
    insights: dict[str, int]                 # pending | ready | failed | unavailable
    pages_total: int
    chunks_total: int
    bytes_total: int
    failures_by_code: dict[ErrorCode, int]
    uploads_per_day: list[tuple[str, int]]   # last 7 days, zero-filled


@dataclass(frozen=True, slots=True)
class StepStats:
    runs: int
    ok: int
    failed: int
    abandoned: int
    duration_ms: Percentiles


@dataclass(frozen=True, slots=True)
class AIStats:
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    unpriced_calls: int                      # cost_usd excludes these; never estimated
    by_purpose: dict[Purpose, tuple[int, int, Decimal, Percentiles]]   # calls, tokens, cost, latency


@dataclass(frozen=True, slots=True)
class ProcessingStats:
    window: timedelta
    completed: int
    failed: int
    in_flight: int
    time_to_ready_ms: Percentiles
    queue_wait_ms: Percentiles
    steps: dict[Step, StepStats]
    retries: int                             # step_runs rows with attempt > 1
    ai: AIStats
    chat_turns: dict[TurnOutcome, int]
    chat_latency_ms: Percentiles


STEP_SQL = """
SELECT step,
       count(*)                                         AS runs,
       count(*) FILTER (WHERE outcome = 'ok')           AS ok,
       count(*) FILTER (WHERE outcome = 'failed')       AS failed,
       count(*) FILTER (WHERE outcome = 'abandoned')    AS abandoned,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at) * 1000)
                             FILTER (WHERE outcome = 'ok') AS p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at) * 1000)
                             FILTER (WHERE outcome = 'ok') AS p95_ms,
       count(*) FILTER (WHERE attempt > 1)              AS retries
FROM step_runs
WHERE started_at >= now() - %(window)s
GROUP BY step
"""

# Time to ready = last successful embed finish - upload time. Queue wait =
# first parse start - upload time. Both from step_runs; no ready_at column to keep in sync.
LATENCY_SQL = """
WITH per_doc AS (
  SELECT d.id, d.created_at,
         min(s.started_at)  FILTER (WHERE s.step = 'parse')                    AS first_parse,
         max(s.finished_at) FILTER (WHERE s.step = 'embed' AND s.outcome = 'ok') AS embedded
  FROM documents d JOIN step_runs s ON s.document_id = d.id
  WHERE d.created_at >= now() - %(window)s
  GROUP BY d.id, d.created_at
)
SELECT percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM embedded - created_at) * 1000) AS ready_p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM embedded - created_at) * 1000) AS ready_p95,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM first_parse - created_at) * 1000) AS wait_p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM first_parse - created_at) * 1000) AS wait_p95
FROM per_doc
"""

AI_SQL = """
SELECT purpose,
       count(*)                          AS calls,
       count(*) FILTER (WHERE NOT ok)    AS errors,
       sum(input_tokens)                 AS input_tokens,
       sum(output_tokens)                AS output_tokens,
       coalesce(sum(cost_usd), 0)        AS cost_usd,
       count(*) FILTER (WHERE cost_usd IS NULL) AS unpriced,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM llm_calls
WHERE at >= now() - %(window)s
GROUP BY purpose
"""


def document_stats(db: Db) -> DocumentStats:
    """GET /metrics/documents. Whole vault, no window: these describe current state."""
    raise NotImplementedError


def processing_stats(db: Db, window: timedelta) -> ProcessingStats:
    """GET /metrics/processing?window=24h. Runs STEP_SQL, LATENCY_SQL, AI_SQL and a
    chat_turns aggregate and assembles them."""
    raise NotImplementedError
