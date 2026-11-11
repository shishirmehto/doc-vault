"""Per-client budgets on upload and ask: the bucket on its own, then over HTTP."""

from __future__ import annotations

import dataclasses

from docvault.api import app
from docvault.ratelimit import RateLimiter

from .conftest import POLICY, QUOTE, SAMPLES
from .test_chat import ready


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_bucket_allows_a_minute_of_requests_then_refills_one_token_at_a_time():
    clock = Clock()
    limiter = RateLimiter(clock)

    burst = [limiter.take("10.0.0.1", "ask", 3) for _ in range(4)]
    other_client, other_action = limiter.take("10.0.0.2", "ask", 3), limiter.take("10.0.0.1", "upload", 3)
    clock.now += 19
    early = limiter.take("10.0.0.1", "ask", 3)
    clock.now += 1
    refilled = limiter.take("10.0.0.1", "ask", 3)

    assert (burst, other_client, other_action, early, refilled) == ([None, None, None, 20], None, None, 1, None)


def test_over_budget_asks_and_uploads_get_429_with_retry_after(client, ctx):
    policy = ready(ctx, POLICY)
    clock = Clock()
    app.state.ctx = dataclasses.replace(ctx, limiter=RateLimiter(clock), settings=ctx.settings.model_copy(
        update={"questions_per_minute": 2, "uploads_per_minute": 1}))
    chat_id = client.post("/chats", json={"document_ids": [str(policy)]}).json()["id"]

    def ask():
        return client.post(f"/chats/{chat_id}/messages", json={"question": "What is the each occurrence limit?"})

    statuses = [ask().status_code, ask().status_code]
    refused = ask()
    clock.now += 30
    statuses.append(ask().status_code)

    assert statuses == [200, 200, 200]
    assert (refused.status_code, refused.headers["retry-after"], refused.json()) == (429, "30", {"error": {
        "code": "rate_limited", "message": "Too many requests. Try again in 30 seconds.", "retryable": True,
        "details": {"retry_after_s": 30}}})

    uploads = [client.post("/documents", files={"file": (name, (SAMPLES / name).read_bytes())}).status_code
               for name in (QUOTE, QUOTE)]
    assert uploads == [202, 429]
