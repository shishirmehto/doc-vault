"""The daily AI spending cap: model calls stop at the cap until midnight UTC; indexing and search do not. Real Postgres."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from docvault import documents, tasks
from docvault.ai import DailySpend
from docvault.config import Settings
from docvault.domain import ErrorCode, Purpose, VaultError

from .conftest import POLICY, QUOTE, SAMPLES, scalar, step_rows
from .test_ai import BAD, GOOD, OWNER, PROMPT, Limit, ScriptedChat, gateway, reply
from .test_ask_stream import LIMIT_QUESTION
from .test_chat import ready

NOW = datetime(2030, 1, 15, 21, 30, tzinfo=UTC)   # 2 h 30 min before the cap resets
MIDNIGHT = datetime(2030, 1, 15, tzinfo=UTC)


def capped(ctx, limit: str, clock: Callable[[], datetime] = lambda: NOW):
    """ctx with daily_ai_budget_usd set and the spend read at `clock`'s time."""
    settings = ctx.settings.model_copy(update={"daily_ai_budget_usd": Decimal(limit)})
    return dataclasses.replace(ctx, settings=settings,
                               ai=dataclasses.replace(ctx.ai, settings=settings, spend=DailySpend(clock)))


def spent(db, usd: str, at: datetime) -> None:
    """One priced ledger row, as a real model call at `at` would leave."""
    with db.connection() as conn:
        conn.execute("INSERT INTO llm_calls (at, purpose, provider, model, document_id, cost_usd, latency_ms, ok)"
                     " VALUES (%s, 'answer', 'openai', 'gpt-6.1-sol', gen_random_uuid(), %s, 1, true)", (at, usd))


@pytest.mark.parametrize(("value", "parsed"), [("", None), ("0.000001", Decimal("0.000001")), ("5", Decimal("5"))])
def test_an_empty_budget_is_no_cap_and_a_number_is_the_cap(monkeypatch, value, parsed):
    monkeypatch.setenv("DAILY_AI_BUDGET_USD", value)

    assert Settings(_env_file=None).daily_ai_budget_usd == parsed


@pytest.mark.parametrize("value", ["0", "-1", "five"])
def test_a_budget_that_is_not_a_positive_amount_stops_the_process_at_start(monkeypatch, value):
    monkeypatch.setenv("DAILY_AI_BUDGET_USD", value)

    with pytest.raises(ValidationError, match="daily_ai_budget_usd"):
        Settings(_env_file=None)


def test_at_the_cap_asks_comparisons_and_summaries_get_429_until_midnight_utc(client, ctx):
    policy, quote = ready(ctx, POLICY), ready(ctx, QUOTE)
    chat_id = client.post("/chats", json={"document_ids": [str(policy)]}).json()["id"]
    spent(ctx.db, "3.00", NOW - timedelta(hours=2))
    spent(ctx.db, "2.00", MIDNIGHT)
    client.app.state.ctx = capped(ctx, "5")

    refused = [client.post(f"/chats/{chat_id}/messages", json={"question": LIMIT_QUESTION}),
               client.post(f"/chats/{chat_id}/messages/stream", json={"question": LIMIT_QUESTION}),
               client.post("/comparisons", json={"document_ids": [str(policy), str(quote)]}),
               client.post(f"/documents/{policy}/summaries", json={})]

    assert [(r.status_code, r.headers["content-type"], r.headers["retry-after"]) for r in refused] == [
        (429, "application/json", "9000")] * 4
    assert [r.json() for r in refused] == [{"error": {
        "code": "quota_exceeded", "message": "Today's AI spending cap of $5.00 is used up. It resets at midnight UTC.",
        "retryable": True, "details": {"limit_usd": 5.0, "spent_today_usd": 5.0,
                                       "resets_at": "2030-01-16T00:00:00+00:00", "retry_after_s": 9000}}}] * 4
    assert scalar(ctx.db, "SELECT count(*) FROM llm_calls WHERE document_id IS NULL") == 0
    assert [scalar(ctx.db, f"SELECT count(*) FROM {table}") for table in
            ("chat_turns", "comparisons", "document_summaries")] == [0, 0, 0]


def test_yesterdays_spend_does_not_count_and_under_the_cap_an_ask_is_answered(client, ctx):
    chat_id = client.post("/chats", json={"document_ids": [str(ready(ctx, POLICY))]}).json()["id"]
    spent(ctx.db, "40.00", MIDNIGHT - timedelta(seconds=1))
    spent(ctx.db, "0.99", MIDNIGHT)
    client.app.state.ctx = capped(ctx, "1")

    answered = client.post(f"/chats/{chat_id}/messages", json={"question": LIMIT_QUESTION})

    assert (answered.status_code, answered.json()["status"]) == (200, "answered")
    assert client.app.state.ctx.ai.budget() == (Decimal("1"), Decimal("0.99"))


def test_over_the_cap_insights_fail_for_good_while_upload_and_indexing_reach_ready(client, ctx, monkeypatch):
    spent(ctx.db, "0.000001", NOW)
    over = capped(ctx, "0.000001")
    client.app.state.ctx = over
    doc_id = client.post("/documents", files={"file": (POLICY, (SAMPLES / POLICY).read_bytes())}).json()["id"]
    documents.run_index(over, UUID(doc_id))
    monkeypatch.setattr(tasks, "_ctx", lambda: over)

    tasks.analyze_document.apply(args=[doc_id])

    document = client.get(f"/documents/{doc_id}").json()
    assert (document["status"], document["insights_status"]) == ("ready", "failed")
    assert client.get(f"/documents/{doc_id}/insights").json() == {"status": "failed", "error": {
        "code": "quota_exceeded", "retryable": False, "details": {},
        "message": "Insights were not written because today's AI spending cap of $0.000001 is used up. "
                   "Retry after midnight UTC."}}
    assert step_rows(ctx.db, UUID(doc_id)) == [("parse", 1, "ok", None), ("embed", 1, "ok", None),
                                               ("analyze", 1, "failed", "quota_exceeded")]
    assert scalar(ctx.db, "SELECT count(*) FROM llm_calls WHERE purpose = 'insights'") == 0


def test_the_days_spend_is_read_at_most_once_every_10_seconds(ctx):
    now = [NOW]
    ai = capped(ctx, "1", clock=lambda: now[0]).ai

    ai.check_budget(Purpose.ANSWER)
    spent(ctx.db, "1.00", NOW)
    now[0] = NOW + timedelta(seconds=9.999)
    ai.check_budget(Purpose.ANSWER)
    seen_within = ai.budget()
    now[0] = NOW + timedelta(seconds=10)
    seen_after = ai.budget()

    assert (seen_within, seen_after) == ((Decimal("1"), Decimal("0")), (Decimal("1"), Decimal("1.00")))
    with pytest.raises(VaultError) as refused:
        ai.check_budget(Purpose.ANSWER)
    assert (refused.value.failure.code, refused.value.details["retry_after_s"]) == (ErrorCode.QUOTA_EXCEEDED, 8990)


def test_a_new_utc_day_starts_a_new_count_even_within_10_seconds(ctx):
    now = [MIDNIGHT - timedelta(seconds=5)]
    spent(ctx.db, "1.00", now[0])
    ai = capped(ctx, "1", clock=lambda: now[0]).ai

    before_midnight = ai.budget()
    now[0] = MIDNIGHT + timedelta(seconds=1)
    after_midnight = ai.budget()

    assert (before_midnight, after_midnight) == ((Decimal("1"), Decimal("1.00")), (Decimal("1"), Decimal("0")))
    ai.check_budget(Purpose.ANSWER)


def test_health_shows_the_cap_and_todays_spend_only_when_a_cap_is_set(client, ctx):
    ctx = dataclasses.replace(ctx, settings=ctx.settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"}))
    client.app.state.ctx = ctx
    uncapped = client.get("/health").json()
    spent(ctx.db, "0.75", NOW - timedelta(minutes=1))
    client.app.state.ctx = capped(ctx, "2.5")
    with_cap = client.get("/health").json()

    assert (uncapped["ai_budget"], with_cap["ai_budget"]) == (None, {"limit_usd": 2.5, "spent_today_usd": 0.75})


def test_the_cap_is_checked_again_before_the_repair_turn(db):
    """The first answer costs $0.003 and fails validation; 11 s later the cache has expired and the cap is reached."""
    started = datetime.now(UTC)
    ticks = iter([started, started + timedelta(seconds=11)])
    chat = ScriptedChat([reply(BAD, 1000, 0, 100), reply(GOOD, 1000, 0, 100)])
    ai = dataclasses.replace(gateway(db, chat, daily_ai_budget_usd=Decimal("0.003")),
                             spend=DailySpend(lambda: next(ticks)))

    with pytest.raises(VaultError) as refused:
        ai.complete(PROMPT, Limit, owner=OWNER)

    assert (refused.value.failure.code, refused.value.failure.retryable, len(chat.prompts)) == (
        ErrorCode.QUOTA_EXCEEDED, True, 1)
    assert scalar(db, "SELECT cost_usd FROM llm_calls") == Decimal("0.003000")
