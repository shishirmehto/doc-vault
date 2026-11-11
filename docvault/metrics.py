"""Metrics, derived at read time. Writes nothing; there are no counters to drift.

Each figure comes from exactly one recorded source:

  document counts, kinds, pages, chunks, bytes, failures  <- documents (current state)
  insights states                                         <- document_insights LEFT JOIN documents
  step durations, failures, retries, crashes              <- step_runs (append-only)
  time to ready, queue wait                               <- step_runs joined to documents.created_at
  tokens, cost, provider latency and errors               <- llm_calls (append-only)
  chat outcomes and end-to-end latency                    <- chat_turns

Every query in a processing read takes the same `since`, so the blocks
describe one window. At thousands of documents these aggregates scan tens of
thousands of rows and run in milliseconds. Past a few million step_runs or
llm_calls rows, the same queries become materialized views refreshed by beat.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from .db import Db
from .documents import INSIGHTS_STATUS_SQL
from .domain import DocKind, DocStatus, ErrorCode, InsightsStatus, Purpose, Step, TurnOutcome


@dataclass(frozen=True, slots=True)
class Percentiles:
    p50: int | None     # None when the window holds no sample
    p95: int | None


@dataclass(frozen=True, slots=True)
class DocumentStats:
    total: int
    by_status: dict[DocStatus, int]             # every status, zero-filled
    by_kind: dict[DocKind, int]                 # every kind, zero-filled
    insights: dict[InsightsStatus, int]         # every status, zero-filled
    pages: int
    chunks: int
    bytes: int
    failures_by_code: dict[ErrorCode, int]      # documents failed now


@dataclass(frozen=True, slots=True)
class StepStats:
    runs: int
    ok: int
    failed: int
    abandoned: int                              # crashed runs, closed by the next lease holder
    retries: int                                # runs with attempt > 1
    duration_ms: Percentiles                    # ok runs only
    failures_by_code: dict[ErrorCode, int]


@dataclass(frozen=True, slots=True)
class AIUsage:
    purpose: Purpose
    model: str
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal                           # priced calls only; never estimated
    unpriced_calls: int
    latency_ms: Percentiles


@dataclass(frozen=True, slots=True)
class AIStats:
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    unpriced_calls: int
    by_purpose_and_model: list[AIUsage]


@dataclass(frozen=True, slots=True)
class ChatStats:
    turns: int
    outcomes: dict[TurnOutcome, int]            # every outcome, zero-filled
    latency_ms: Percentiles


@dataclass(frozen=True, slots=True)
class ProcessingStats:
    since: datetime
    uploaded: int                               # documents created in the window that still exist
    indexed: int                                # documents with a successful embed in the window
    time_to_ready_ms: Percentiles               # upload to first successful embed
    queue_wait_ms: Percentiles                  # upload to first parse start
    steps: dict[Step, StepStats]                # every step, zero-filled
    ai: AIStats
    chat: ChatStats


DOCUMENTS_SQL = """
SELECT status, kind, error_code, count(*) AS n, coalesce(sum(page_count), 0) AS pages,
       coalesce(sum(chunk_count), 0) AS chunks, sum(size_bytes) AS bytes
FROM documents GROUP BY status, kind, error_code
"""

INSIGHTS_SQL = f"""
SELECT {INSIGHTS_STATUS_SQL} AS status, count(*) AS n
FROM documents d LEFT JOIN document_insights i ON i.document_id = d.id
GROUP BY 1
"""

STEP_SQL = """
SELECT step,
       count(*)                                         AS runs,
       count(*) FILTER (WHERE outcome = 'ok')           AS ok,
       count(*) FILTER (WHERE outcome = 'failed')       AS failed,
       count(*) FILTER (WHERE outcome = 'abandoned')    AS abandoned,
       count(*) FILTER (WHERE attempt > 1)              AS retries,
       round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at) * 1000)
             FILTER (WHERE outcome = 'ok'))::int        AS p50,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at) * 1000)
             FILTER (WHERE outcome = 'ok'))::int        AS p95
FROM step_runs
WHERE started_at >= %(since)s
GROUP BY step
"""

STEP_FAILURES_SQL = """
SELECT step, error_code, count(*) AS n FROM step_runs
WHERE started_at >= %(since)s AND outcome = 'failed'
GROUP BY step, error_code
"""

# Time to ready = first successful embed finish - upload time. Queue wait = first parse start - upload time.
# Both come from step_runs, so no ready_at column has to be kept in sync. The first embed, not the last:
# a re-embed after a model change must not stretch a document's time to ready.
LATENCY_SQL = """
WITH per_doc AS (
  SELECT d.created_at,
         min(s.started_at)  FILTER (WHERE s.step = 'parse')                      AS first_parse,
         min(s.finished_at) FILTER (WHERE s.step = 'embed' AND s.outcome = 'ok') AS first_ready
  FROM documents d JOIN step_runs s ON s.document_id = d.id AND s.step IN ('parse', 'embed')
  WHERE d.created_at >= %(since)s
  GROUP BY d.id, d.created_at
)
SELECT round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM first_ready - created_at) * 1000))::int AS ready_p50,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM first_ready - created_at) * 1000))::int AS ready_p95,
       round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM first_parse - created_at) * 1000))::int AS wait_p50,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM first_parse - created_at) * 1000))::int AS wait_p95
FROM per_doc
"""

FLOW_SQL = """
SELECT (SELECT count(*) FROM documents WHERE created_at >= %(since)s) AS uploaded,
       (SELECT count(DISTINCT document_id) FROM step_runs
         WHERE started_at >= %(since)s AND step = 'embed' AND outcome = 'ok') AS indexed
"""

AI_SQL = """
SELECT purpose, model,
       count(*)                                  AS calls,
       count(*) FILTER (WHERE NOT ok)            AS errors,
       sum(input_tokens)                         AS input_tokens,
       sum(output_tokens)                        AS output_tokens,
       coalesce(sum(cost_usd), 0)                AS cost_usd,
       count(*) FILTER (WHERE cost_usd IS NULL)  AS unpriced,
       round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms))::int AS p50,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms))::int AS p95
FROM llm_calls
WHERE at >= %(since)s
GROUP BY purpose, model
ORDER BY purpose, model
"""

CHAT_SQL = "SELECT outcome, count(*) AS n FROM chat_turns WHERE started_at >= %(since)s GROUP BY outcome"

CHAT_LATENCY_SQL = """
SELECT round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at) * 1000))::int AS p50,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at) * 1000))::int AS p95
FROM chat_turns WHERE started_at >= %(since)s
"""


def document_stats(db: Db) -> DocumentStats:
    """GET /metrics/documents. Whole vault, no window: these describe current state."""
    with db.connection() as conn:
        groups = conn.execute(DOCUMENTS_SQL).fetchall()
        insights = {InsightsStatus(row["status"]): row["n"] for row in conn.execute(INSIGHTS_SQL)}
    by_status: Counter[DocStatus] = Counter()
    by_kind: Counter[DocKind] = Counter()
    failures: Counter[ErrorCode] = Counter()
    for row in groups:
        by_status[DocStatus(row["status"])] += row["n"]
        by_kind[DocKind(row["kind"])] += row["n"]
        if row["error_code"] is not None:
            failures[ErrorCode(row["error_code"])] += row["n"]
    return DocumentStats(
        total=by_status.total(),
        by_status={status: by_status[status] for status in DocStatus},
        by_kind={kind: by_kind[kind] for kind in DocKind},
        insights={status: insights.get(status, 0) for status in InsightsStatus},
        pages=sum(row["pages"] for row in groups), chunks=sum(row["chunks"] for row in groups),
        bytes=sum(row["bytes"] for row in groups), failures_by_code=dict(failures))


def processing_stats(db: Db, window: timedelta) -> ProcessingStats:
    """GET /metrics/processing?window=24h: what happened in the last `window`."""
    params = {"since": datetime.now(UTC) - window}
    with db.connection() as conn:
        flow = conn.execute(FLOW_SQL, params).fetchone()
        latency = conn.execute(LATENCY_SQL, params).fetchone()
        steps = {Step(row["step"]): row for row in conn.execute(STEP_SQL, params)}
        failures: dict[Step, dict[ErrorCode, int]] = {step: {} for step in Step}
        for row in conn.execute(STEP_FAILURES_SQL, params):
            failures[Step(row["step"])][ErrorCode(row["error_code"])] = row["n"]
        usage = [AIUsage(Purpose(row["purpose"]), row["model"], row["calls"], row["errors"], row["input_tokens"],
                         row["output_tokens"], row["cost_usd"], row["unpriced"], Percentiles(row["p50"], row["p95"]))
                 for row in conn.execute(AI_SQL, params)]
        outcomes = {TurnOutcome(row["outcome"]): row["n"] for row in conn.execute(CHAT_SQL, params)}
        chat_latency = conn.execute(CHAT_LATENCY_SQL, params).fetchone()
    return ProcessingStats(
        since=params["since"], uploaded=flow["uploaded"], indexed=flow["indexed"],
        time_to_ready_ms=Percentiles(latency["ready_p50"], latency["ready_p95"]),
        queue_wait_ms=Percentiles(latency["wait_p50"], latency["wait_p95"]),
        steps={step: _step_stats(steps.get(step), failures[step]) for step in Step},
        ai=AIStats(calls=sum(u.calls for u in usage), errors=sum(u.errors for u in usage),
                   input_tokens=sum(u.input_tokens for u in usage), output_tokens=sum(u.output_tokens for u in usage),
                   cost_usd=sum((u.cost_usd for u in usage), Decimal(0)),
                   unpriced_calls=sum(u.unpriced_calls for u in usage), by_purpose_and_model=usage),
        chat=ChatStats(turns=sum(outcomes.values()),
                       outcomes={outcome: outcomes.get(outcome, 0) for outcome in TurnOutcome},
                       latency_ms=Percentiles(chat_latency["p50"], chat_latency["p95"])))


def _step_stats(row: Mapping[str, Any] | None, failures: dict[ErrorCode, int]) -> StepStats:
    if row is None:
        return StepStats(0, 0, 0, 0, 0, Percentiles(None, None), failures)
    return StepStats(row["runs"], row["ok"], row["failed"], row["abandoned"], row["retries"],
                     Percentiles(row["p50"], row["p95"]), failures)
