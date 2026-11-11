"""The AI gateway against the real ledger. OpenAI is reached only through a mock transport."""

from __future__ import annotations

import array
import base64
import dataclasses
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

import httpx2
import pytest
from openai import OpenAI
from pydantic import BaseModel

from docvault import ai
from docvault.ai import (
    AI,
    DocOwner,
    FakeChat,
    FakeEmbedder,
    OpenAIChat,
    OpenAIEmbedder,
    ProviderError,
    RawCompletion,
    Tokens,
)
from docvault.config import Settings, load_settings
from docvault.domain import DocumentId, ErrorCode, Failure, Purpose, VaultError
from docvault.prompts import Msg, Prompt, Tier

OWNER = DocOwner(DocumentId(UUID("00000000-0000-0000-0000-0000000000d1")))
QUESTION = Msg("user", "What is the each occurrence limit?")
PROMPT = Prompt(name="answer", version="answer-v1", purpose=Purpose.ANSWER, tier="main",
                system="Answer from the sources.", messages=(QUESTION,))
GOOD = '{"amount": 1000000, "currency": "USD"}'
BAD = '{"amount": "one million", "currency": "USD"}'


class Limit(BaseModel):
    amount: int
    currency: str


LIMIT = Limit(amount=1_000_000, currency="USD")


@dataclass
class ScriptedChat:
    """Returns or raises its script in order and keeps every prompt it was sent."""

    script: list[tuple[RawCompletion, Tokens] | ProviderError]
    models: Mapping[Tier, str] = field(default_factory=lambda: {"main": "gpt-6.1-sol", "small": "gpt-6-luna"})
    provider: str = "scripted"
    prompts: list[Prompt] = field(default_factory=list)

    def complete_json(self, prompt: Prompt, output: type[BaseModel], *,
                      timeout_s: float) -> tuple[RawCompletion, Tokens]:
        self.prompts.append(prompt)
        step = self.script.pop(0)
        if isinstance(step, ProviderError):
            raise step
        return step


def reply(text: str, input_tokens: int, cached: int, output_tokens: int,
          refusal: str | None = None) -> tuple[RawCompletion, Tokens]:
    return RawCompletion(text, refusal), Tokens(input_tokens, cached, output_tokens)


def gateway(db, chat, sleeps: list[float] | None = None, *, embedder=None, **settings) -> AI:
    settings = {"ai_max_attempts": 3, "ai_backoff_s": 0.5} | settings
    return AI(chat, embedder or FakeEmbedder(), db, Settings(_env_file=None, **settings),
              sleep=(sleeps if sleeps is not None else []).append)


def rows(db, columns: str) -> list[tuple]:
    with db.connection() as conn:
        return [tuple(row.values()) for row in conn.execute(f"SELECT {columns} FROM llm_calls ORDER BY id")]


def failure_of(call: Callable[[], object]) -> Failure:
    with pytest.raises(VaultError) as raised:
        call()
    return raised.value.failure


def unavailable(code: str, retryable: bool) -> Failure:
    return Failure(ErrorCode.AI_UNAVAILABLE, f"The AI provider could not complete the request ({code}).", retryable)


def test_valid_json_is_returned_and_cached_input_is_billed_at_the_cached_rate(db):
    chat = ScriptedChat([reply(GOOD, 1_000_000, 200_000, 10_000)])

    assert gateway(db, chat).complete(PROMPT, Limit, owner=OWNER) == LIMIT
    assert rows(db, "purpose, provider, model, prompt_name, prompt_version, document_id, turn_id,"
                    " input_tokens, output_tokens, cost_usd, ok, error_code") == [
        ("answer", "scripted", "gpt-6.1-sol", "answer", "answer-v1", OWNER.document_id, None,
         1_000_000, 10_000, Decimal("1.720000"), True, None)]


def test_invalid_json_gets_one_repair_turn_that_names_the_failed_field(db):
    chat = ScriptedChat([reply(BAD, 120, 0, 15), reply(GOOD, 160, 0, 15)])

    assert gateway(db, chat).complete(PROMPT, Limit, owner=OWNER) == LIMIT
    assert rows(db, "ok, error_code, input_tokens") == [(True, None, 120), (True, None, 160)]
    assert chat.prompts[1].messages == (
        QUESTION,
        Msg("assistant", BAD),
        Msg("user", "That JSON failed validation: amount: Input should be a valid integer, unable to parse string"
                    " as an integer. Return the corrected JSON only."))


def test_invalid_json_after_the_repair_turn_is_bad_output(db):
    chat = ScriptedChat([reply(BAD, 120, 0, 15), reply("Sorry, here it is: {", 160, 0, 6)])

    assert failure_of(lambda: gateway(db, chat).complete(PROMPT, Limit, owner=OWNER)) == Failure(
        ErrorCode.AI_BAD_OUTPUT, "The model returned invalid output twice.", False)
    assert rows(db, "ok, error_code") == [(True, None), (True, None)]


def test_a_refusal_is_bad_output_without_a_repair_turn(db):
    chat = ScriptedChat([reply("", 90, 0, 12, refusal="I can't help with that.")])

    assert failure_of(lambda: gateway(db, chat).complete(PROMPT, Limit, owner=OWNER)) == Failure(
        ErrorCode.AI_BAD_OUTPUT, "The model declined to produce this output.", False)
    assert rows(db, "ok, error_code, input_tokens, output_tokens") == [(True, None, 90, 12)]
    assert len(chat.prompts) == 1


def test_rate_limits_are_retried_with_doubling_backoff_and_every_attempt_is_a_row(db):
    sleeps: list[float] = []
    limited = ProviderError("rate_limited", retryable=True)
    chat = ScriptedChat([limited, limited, reply(GOOD, 100, 0, 20)])

    assert gateway(db, chat, sleeps).complete(PROMPT, Limit, owner=OWNER) == LIMIT
    assert rows(db, "ok, error_code, input_tokens") == [(False, "rate_limited", 0), (False, "rate_limited", 0),
                                                        (True, None, 100)]
    assert sleeps == [0.5, 1.0]


def test_a_non_retryable_provider_error_fails_at_once(db):
    sleeps: list[float] = []
    chat = ScriptedChat([ProviderError("context_length_exceeded", retryable=False)])

    assert failure_of(lambda: gateway(db, chat, sleeps).complete(PROMPT, Limit, owner=OWNER)) == unavailable(
        "context_length_exceeded", False)
    assert rows(db, "ok, error_code") == [(False, "context_length_exceeded")]
    assert sleeps == []


def test_a_provider_down_for_every_attempt_is_retryable_unavailable(db):
    sleeps: list[float] = []
    chat = ScriptedChat([ProviderError("server_error", retryable=True)] * 3)

    assert failure_of(lambda: gateway(db, chat, sleeps).complete(PROMPT, Limit, owner=OWNER)) == unavailable(
        "server_error", True)
    assert rows(db, "ok, error_code") == [(False, "server_error")] * 3
    assert sleeps == [0.5, 1.0]


def test_a_retry_after_longer_than_the_wait_limit_fails_at_once_as_retryable(db):
    sleeps: list[float] = []
    chat = ScriptedChat([ProviderError("rate_limited", retryable=True, retry_after_s=45.0), reply(GOOD, 100, 0, 20)])

    assert failure_of(lambda: gateway(db, chat, sleeps).complete(PROMPT, Limit, owner=OWNER)) == unavailable(
        "rate_limited", True)
    assert rows(db, "ok, error_code") == [(False, "rate_limited")]
    assert sleeps == []


def test_background_purposes_are_left_to_celery_and_not_retried_here(db):
    sleeps: list[float] = []
    insights = dataclasses.replace(PROMPT, name="insights", purpose=Purpose.INSIGHTS)
    chat = ScriptedChat([ProviderError("server_error", retryable=True), reply(GOOD, 100, 0, 20)])

    assert failure_of(lambda: gateway(db, chat, sleeps).complete(insights, Limit, owner=OWNER)) == unavailable(
        "server_error", True)
    assert rows(db, "purpose, ok, error_code") == [("insights", False, "server_error")]
    assert sleeps == []


def test_a_model_without_a_price_records_cost_null(db):
    chat = ScriptedChat([reply(GOOD, 100, 0, 20)], models={"main": "gpt-7-preview", "small": "gpt-7-preview"})

    gateway(db, chat).complete(PROMPT, Limit, owner=OWNER)

    assert rows(db, "model, input_tokens, cost_usd") == [("gpt-7-preview", 100, None)]


# ------------------------------------------------------------------ OpenAI translators

CHAT_COMPLETION = {
    "id": "chatcmpl-u5", "object": "chat.completion", "created": 1762819200, "model": "gpt-6.1-sol",
    "choices": [{"index": 0, "finish_reason": "stop", "logprobs": None,
                 "message": {"role": "assistant", "content": GOOD, "refusal": None}}],
    "usage": {"prompt_tokens": 2000, "completion_tokens": 300, "total_tokens": 2300,
              "prompt_tokens_details": {"cached_tokens": 1000}},
}


def openai_client(*responses: httpx2.Response | Exception, sent: list[tuple[str, dict]] | None = None) -> OpenAI:
    queue = list(responses)

    def handle(request: httpx2.Request) -> httpx2.Response:
        if sent is not None:
            sent.append((request.url.path, json.loads(request.content)))
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return OpenAI(api_key="sk-test", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))


def openai_chat(client: OpenAI) -> OpenAIChat:
    return OpenAIChat(client, {"main": "gpt-6.1-sol", "small": "gpt-6-luna"}, {"main": "low", "small": "none"})


def test_openai_chat_sends_a_strict_schema_and_the_ledger_prices_its_cached_tokens(db):
    sent: list[tuple[str, dict]] = []
    client = openai_client(httpx2.Response(200, json=CHAT_COMPLETION), sent=sent)

    assert gateway(db, openai_chat(client)).complete(PROMPT, Limit, owner=OWNER) == LIMIT
    assert sent == [("/v1/chat/completions", {
        "model": "gpt-6.1-sol",
        "messages": [{"role": "developer", "content": "Answer from the sources."},
                     {"role": "user", "content": "What is the each occurrence limit?"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "Limit", "strict": True, "schema": {
            "type": "object", "title": "Limit", "additionalProperties": False, "required": ["amount", "currency"],
            "properties": {"amount": {"title": "Amount", "type": "integer"},
                           "currency": {"title": "Currency", "type": "string"}}}}},
        "reasoning_effort": "low",
    })]
    assert rows(db, "provider, model, input_tokens, output_tokens, cost_usd, ok") == [
        ("openai", "gpt-6.1-sol", 2000, 300, Decimal("0.005100"), True)]


def test_openai_rate_limit_waits_for_retry_after_then_succeeds(db):
    sleeps: list[float] = []
    client = openai_client(
        httpx2.Response(429, headers={"retry-after": "2"}, json={"error": {
            "message": "Rate limit reached for requests", "type": "requests", "param": None,
            "code": "rate_limit_exceeded"}}),
        httpx2.Response(200, json=CHAT_COMPLETION))

    assert gateway(db, openai_chat(client), sleeps).complete(PROMPT, Limit, owner=OWNER) == LIMIT
    assert rows(db, "ok, error_code") == [(False, "rate_limited"), (True, None)]
    assert sleeps == [2.0]


def test_openai_insufficient_quota_is_not_retried(db):
    sleeps: list[float] = []
    client = openai_client(httpx2.Response(429, json={"error": {
        "message": "You exceeded your current quota.", "type": "insufficient_quota", "param": None,
        "code": "insufficient_quota"}}))

    assert failure_of(lambda: gateway(db, openai_chat(client), sleeps).complete(PROMPT, Limit, owner=OWNER)) == (
        unavailable("insufficient_quota", False))
    assert rows(db, "ok, error_code") == [(False, "insufficient_quota")]
    assert sleeps == []


@pytest.mark.parametrize(("failure", "code", "retryable"), [
    (httpx2.ReadTimeout("read timed out"), "timeout", True),
    (httpx2.ConnectError("connection refused"), "connection_error", True),
    (httpx2.Response(503, json={"error": {"message": "Overloaded", "type": "server_error", "code": None}}),
     "server_error", True),
    (httpx2.Response(400, json={"error": {"message": "Too long", "type": "invalid_request_error",
                                          "code": "context_length_exceeded"}}), "context_length_exceeded", False),
    (httpx2.Response(403, json={"error": {"message": "Forbidden", "type": "invalid_request_error", "code": None}}),
     "http_403", False),
    (httpx2.Response(200, json={**CHAT_COMPLETION, "choices": []}), "malformed_response", True),
    (httpx2.Response(200, json={**CHAT_COMPLETION, "usage": None}), "malformed_response", True),
])
def test_openai_failures_map_to_the_retry_policy(db, failure, code, retryable):
    client = openai_client(failure)

    assert failure_of(lambda: gateway(db, openai_chat(client), ai_max_attempts=1).complete(
        PROMPT, Limit, owner=OWNER)) == unavailable(code, retryable)
    assert rows(db, "ok, error_code") == [(False, code)]


def float32(*values: float) -> str:
    return base64.b64encode(array.array("f", values).tobytes()).decode()


def test_openai_embeddings_come_back_in_input_order_with_the_requested_dimensions(db):
    sent: list[tuple[str, dict]] = []
    client = openai_client(httpx2.Response(200, json={
        "object": "list", "model": "text-embedding-3-small",
        "data": [{"object": "embedding", "index": 1, "embedding": float32(0.0, 1.0)},
                 {"object": "embedding", "index": 0, "embedding": float32(1.0, 0.0)}],
        "usage": {"prompt_tokens": 50_000, "total_tokens": 50_000}}), sent=sent)
    live = gateway(db, ScriptedChat([]), embedder=OpenAIEmbedder(client, "text-embedding-3-small", 2))

    vectors = live.embed(["first text", "second text"], purpose=Purpose.EMBED_QUERY, owner=OWNER)

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    [(path, body)] = sent
    assert (path, body["model"], body["input"], body["dimensions"]) == (
        "/v1/embeddings", "text-embedding-3-small", ["first text", "second text"], 2)
    assert live.embed_model == "text-embedding-3-small-2"
    assert rows(db, "purpose, provider, model, input_tokens, cost_usd") == [
        ("embed_query", "openai", "text-embedding-3-small", 50_000, Decimal("0.001000"))]


def test_openai_embedding_failures_are_retried_like_chat_failures(db):
    sleeps: list[float] = []
    client = openai_client(
        httpx2.Response(500, json={"error": {"message": "Internal error", "type": "server_error", "code": None}}),
        httpx2.Response(200, json={"object": "list", "model": "text-embedding-3-small",
                                   "data": [{"object": "embedding", "index": 0, "embedding": float32(0.5, 0.5)}],
                                   "usage": {"prompt_tokens": 3, "total_tokens": 3}}))
    live = gateway(db, ScriptedChat([]), sleeps,
                   embedder=OpenAIEmbedder(client, "text-embedding-3-small", 2))

    assert live.embed(["a policy"], purpose=Purpose.EMBED_QUERY, owner=OWNER) == [[0.5, 0.5]]
    assert rows(db, "ok, error_code, input_tokens") == [(False, "server_error", 0), (True, None, 3)]
    assert sleeps == [0.5]


@pytest.mark.parametrize(("vectors", "usage"), [
    (1, {"prompt_tokens": 6, "total_tokens": 6}),   # fewer vectors than texts
    (2, None),
])
def test_an_embedding_response_short_of_vectors_or_usage_is_a_provider_error(db, vectors, usage):
    client = openai_client(httpx2.Response(200, json={
        "object": "list", "model": "text-embedding-3-small",
        "data": [{"object": "embedding", "index": i, "embedding": float32(0.5, 0.5)} for i in range(vectors)],
        "usage": usage}))
    live = gateway(db, ScriptedChat([]), embedder=OpenAIEmbedder(client, "text-embedding-3-small", 2), ai_max_attempts=1)

    assert failure_of(lambda: live.embed(["a policy", "an endorsement"], purpose=Purpose.EMBED_QUERY, owner=OWNER)) == (
        unavailable("malformed_response", True))
    assert rows(db, "ok, error_code") == [(False, "malformed_response")]


def test_fake_chat_answers_from_its_handler_and_counts_words_as_tokens(db):
    class LimitChat(FakeChat):
        handlers = {"answer": lambda prompt: Limit(amount=prompt.inputs["amount"], currency="USD")}

    fake = gateway(db, LimitChat())
    prompt = dataclasses.replace(PROMPT, inputs={"amount": 250_000})

    assert fake.complete(prompt, Limit, owner=OWNER) == Limit(amount=250_000, currency="USD")
    assert rows(db, "provider, model, input_tokens, output_tokens, cost_usd") == [
        ("fake", "fake-chat", 10, 1, Decimal("0.000000"))]
    with pytest.raises(NotImplementedError):
        fake.complete(dataclasses.replace(PROMPT, name="condense"), Limit, owner=OWNER)


def test_the_embedding_space_names_the_dimension(db):
    small = gateway(db, ScriptedChat([]), embedder=FakeEmbedder(dimensions=8))

    [vector] = small.embed(["Each occurrence limit $1,000,000"], purpose=Purpose.EMBED_CHUNKS, owner=OWNER)

    assert (len(vector), round(sum(x * x for x in vector), 9), small.embed_model) == (8, 1.0, "fake-hash-8")


# ------------------------------------------------------------------ settings and build


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)   # no .env file to read
    for name in ("OPENAI_API_KEY", "CHAT_PROVIDER", "EMBED_PROVIDER", "EMBED_DIMENSIONS", "OPENAI_CHAT_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_an_explicit_openai_provider_without_a_key_fails_at_load(clean_env, monkeypatch):
    monkeypatch.setenv("CHAT_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "")   # .env.example ships the key empty

    with pytest.raises(ValueError, match=re.escape(
            "OPENAI_API_KEY is not set, but it is required by CHAT_PROVIDER=openai.")):
        load_settings()


def test_a_key_with_auto_providers_builds_the_live_gateway_from_settings(clean_env, monkeypatch, db):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_CHAT_MODEL", "gpt-6.1-terra")
    monkeypatch.setenv("EMBED_DIMENSIONS", "256")

    live = ai.build(load_settings(), db)

    assert (live.mode, live.embed_model, dict(live.chat.models)) == (
        "live", "text-embedding-3-small-256", {"main": "gpt-6.1-terra", "small": "gpt-6-luna"})
    monkeypatch.setenv("EMBED_PROVIDER", "fake")
    assert ai.build(load_settings(), db).mode == "mixed"
