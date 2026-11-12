from __future__ import annotations

import dataclasses
import json
import threading

import pytest

from docvault.ai import FakeChat, ProviderError, RawCompletion, Tokens
from docvault.api import AskInThread, app
from docvault.domain import ChatId
from docvault.ratelimit import RateLimiter

from .conftest import FACTS, POLICY, scalar
from .test_chat import ledger, normalised, ready
from .test_insights import provider_down, with_chat

LIMIT_QUESTION = "What is the each occurrence limit?"


def events(text: str) -> list[tuple[str, dict]]:
    parsed = []
    for frame in text.split("\n\n"):
        if frame:
            fields = dict(line.split(": ", 1) for line in frame.split("\n"))
            parsed.append((fields["event"], json.loads(fields["data"])))
    return parsed


def stream(client, chat_id: str, question: str) -> list[tuple[str, dict]]:
    response = client.post(f"/chats/{chat_id}/messages/stream", json={"question": question})
    assert response.status_code == 200, response.text
    assert (response.headers["content-type"], response.headers["cache-control"],
            response.headers["x-accel-buffering"]) == ("text/event-stream; charset=utf-8", "no-cache, no-transform", "no")
    return events(response.text)


def shape(sent: list[tuple[str, dict]]) -> list[str]:
    names: list[str] = []
    for event, data in sent:
        name = f"stage:{data['stage']}" if event == "stage" else event
        if not (name == "delta" and names and names[-1] == "delta"):
            names.append(name)
    return names


def text_of(sent: list[tuple[str, dict]]) -> str:
    return "".join(data["text"] for event, data in sent if event == "delta")


def policy_chat(client, ctx) -> str:
    return client.post("/chats", json={"document_ids": [str(ready(ctx, POLICY))]}).json()["id"]


def test_the_answer_streams_without_markers_and_ends_with_the_turn_history_keeps(client, ctx):
    chat_id = policy_chat(client, ctx)
    limit = normalised(FACTS["policy-each-occurrence-limit"]["text"])

    first = stream(client, chat_id, LIMIT_QUESTION)
    follow_up = stream(client, chat_id, "Do defence costs reduce it?")

    assert shape(first) == ["stage:searching", "stage:answering", "delta", "turn"]
    assert len([event for event, _ in first if event == "delta"]) > 1
    assert text_of(first) == limit
    assert (first[-1][1]["status"], first[-1][1]["answer"]) == ("answered", f"{limit} [1]")
    assert shape(follow_up) == ["stage:condensing", "stage:searching", "stage:answering", "delta", "turn"]
    assert text_of(follow_up) == "They do not reduce the Each Occurrence Limit or either aggregate limit."
    turns = client.get(f"/chats/{chat_id}/messages").json()["turns"]
    assert [first[-1][1], follow_up[-1][1]] == turns
    assert ledger(ctx) == {turns[0]["id"]: ["embed_query", "answer"],
                           turns[1]["id"]: ["condense", "embed_query", "answer"]}


def test_a_question_the_gate_refuses_streams_no_text_and_ends_with_the_refusal(client, ctx):
    chat_id = policy_chat(client, ctx)

    refused = stream(client, chat_id, "Is flood damage covered?")

    assert shape(refused) == ["stage:searching", "turn"]
    assert (refused[-1][1]["status"], refused[-1][1]["reason"]) == ("not_found", "no_evidence")
    assert ledger(ctx) == {refused[-1][1]["id"]: ["embed_query"]}


def test_refusals_found_before_the_stream_starts_are_the_usual_status_and_envelope(client, ctx):
    chat_id = policy_chat(client, ctx)
    url = f"/chats/{chat_id}/messages/stream"

    missing = client.post("/chats/00000000-0000-0000-0000-000000000000/messages/stream",
                          json={"question": LIMIT_QUESTION})
    blank = client.post(url, json={"question": "   "})

    assert (missing.status_code, missing.headers["content-type"], missing.json()["error"]["code"]) == (
        404, "application/json", "chat_not_found")
    assert (blank.status_code, blank.json()["error"]["code"]) == (422, "invalid_request")
    with ctx.db.connection() as conn:
        conn.execute("DELETE FROM documents")
    deleted = client.post(url, json={"question": LIMIT_QUESTION})
    assert (deleted.status_code, deleted.json()["error"]["code"]) == (409, "documents_deleted")
    assert scalar(ctx.db, "SELECT count(*) FROM chat_turns") == 0


def test_the_stream_spends_from_the_same_rate_limit_budget_as_the_plain_ask(client, ctx):
    chat_id = policy_chat(client, ctx)
    app.state.ctx = dataclasses.replace(ctx, limiter=RateLimiter(), settings=ctx.settings.model_copy(
        update={"questions_per_minute": 1}))

    plain = client.post(f"/chats/{chat_id}/messages", json={"question": LIMIT_QUESTION})
    streamed = client.post(f"/chats/{chat_id}/messages/stream", json={"question": LIMIT_QUESTION})

    assert (plain.status_code, streamed.status_code, streamed.json()["error"]["code"]) == (200, 429, "rate_limited")
    assert int(streamed.headers["retry-after"]) > 0


def test_a_provider_failure_mid_stream_is_an_error_event_and_every_attempt_stays_billed(client, ctx):
    chat_id = policy_chat(client, ctx)
    app.state.ctx = with_chat(ctx, provider_down("server_error", retryable=True, prompt="answer"))

    failed = stream(client, chat_id, LIMIT_QUESTION)

    assert shape(failed) == ["stage:searching", "stage:answering", "error"]
    assert failed[-1][1] == {"error": {"code": "ai_unavailable", "retryable": True, "details": {},
                                       "message": "The AI provider could not complete the request (server_error)."}}
    assert scalar(ctx.db, "SELECT count(*) FROM chat_turns") == 0
    with ctx.db.connection() as conn:
        rows = conn.execute("SELECT purpose, ok, error_code FROM llm_calls WHERE turn_id IS NOT NULL ORDER BY id")
        assert [tuple(row.values()) for row in rows] == [
            ("embed_query", True, None), ("answer", False, "server_error"), ("answer", False, "server_error"),
            ("answer", False, "server_error")]


class BreaksOnceAfterText(FakeChat):
    def __init__(self) -> None:
        super().__init__()
        self.broken = False

    def complete_json(self, prompt, output, *, timeout_s, on_text=None):
        if prompt.name != "answer" or self.broken or on_text is None:
            return super().complete_json(prompt, output, timeout_s=timeout_s, on_text=on_text)
        self.broken = True
        text = self.handlers["answer"](prompt).model_dump_json()
        on_text(text[:len(text) // 2])
        raise ProviderError("server_error", retryable=True)


class RepairedOnce(FakeChat):
    def complete_json(self, prompt, output, *, timeout_s, on_text=None):
        if prompt.name != "answer" or len(prompt.messages) > 1 or on_text is None:
            return super().complete_json(prompt, output, timeout_s=timeout_s, on_text=on_text)
        bad = '{"supported": true, "answer": "A first draft [S1].", "evidence": "S1"}'
        on_text(bad)
        return RawCompletion(bad), Tokens(100, 0, 10)


@pytest.mark.parametrize(("backend", "attempts"), [
    pytest.param(BreaksOnceAfterText, [False, True], id="retry after a provider failure"),
    pytest.param(RepairedOnce, [True, True], id="repair after invalid output"),
])
def test_text_from_a_failed_or_repaired_attempt_is_reset_before_the_next_attempt_streams(client, ctx, backend,
                                                                                         attempts):
    limit = normalised(FACTS["policy-each-occurrence-limit"]["text"])
    chat_id = policy_chat(client, ctx)
    app.state.ctx = with_chat(ctx, backend())

    sent = stream(client, chat_id, LIMIT_QUESTION)

    assert shape(sent) == ["stage:searching", "stage:answering", "delta", "reset", "delta", "turn"]
    assert text_of(sent[:[event for event, _ in sent].index("reset")]) != ""
    assert text_of(sent[[event for event, _ in sent].index("reset"):]) == limit
    assert sent[-1][1]["answer"] == f"{limit} [1]"
    with ctx.db.connection() as conn:
        rows = conn.execute("SELECT ok FROM llm_calls WHERE turn_id = %s AND purpose = 'answer' ORDER BY id",
                            (sent[-1][1]["id"],))
        assert [row["ok"] for row in rows] == attempts


class HeldAfterFirstWord(FakeChat):
    def __init__(self, go: threading.Event) -> None:
        super().__init__()
        self.go = go

    def complete_json(self, prompt, output, *, timeout_s, on_text=None):
        if prompt.name != "answer" or on_text is None:
            return super().complete_json(prompt, output, timeout_s=timeout_s, on_text=on_text)
        pieces = 0

        def held(piece: str) -> None:
            nonlocal pieces
            on_text(piece)
            pieces += 1
            if pieces == 2:
                assert self.go.wait(10), "the test never let the answer finish"

        return super().complete_json(prompt, output, timeout_s=timeout_s, on_text=held)


def test_a_client_that_leaves_mid_answer_still_gets_its_turn_stored_and_billed(client, ctx):
    chat_id = ChatId(policy_chat(client, ctx))
    go = threading.Event()

    asking = AskInThread(with_chat(ctx, HeldAfterFirstWord(go)), chat_id, LIMIT_QUESTION)
    frames = asking.frames(asking.first())
    seen = [next(frames) for _ in range(3)]
    frames.close()
    go.set()
    asking.thread.join(10)

    assert not asking.thread.is_alive()
    assert shape(events("".join(seen))) == ["stage:searching", "stage:answering", "delta"]
    [turn] = client.get(f"/chats/{chat_id}/messages").json()["turns"]
    assert (turn["question"], turn["status"]) == (LIMIT_QUESTION, "answered")
    assert ledger(ctx) == {turn["id"]: ["embed_query", "answer"]}
    assert scalar(ctx.db, "SELECT count(*) FROM llm_calls WHERE turn_id = %s AND cost_usd IS NOT NULL",
                  (turn["id"],)) == 2


def test_the_route_declares_its_event_stream_and_the_json_envelope_for_errors(client):
    operation = client.get("/openapi.json").json()["paths"]["/chats/{chat_id}/messages/stream"]["post"]

    assert {status: list(response["content"]) for status, response in operation["responses"].items()} == {
        "200": ["text/event-stream"], "404": ["application/json"], "409": ["application/json"],
        "422": ["application/json"], "429": ["application/json"]}
    assert all(f"`{event}`" in operation["description"] for event in ("stage", "delta", "reset", "turn", "error"))
