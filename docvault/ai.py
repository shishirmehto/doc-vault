"""The single door to every model provider, and the ledger behind it.

Callers get one object, AI, with two methods: embed() and complete(). Behind
them sit provider choice, batching, the embedding cache, timeouts, retries with
backoff, JSON-schema output, validation with one repair turn (none for a
comparison or a summary), cost pricing, the daily spending cap on completions,
and one llm_calls row per provider attempt (failed attempts included). No module can
reach a provider without going through here, so token and cost accounting
cannot be skipped.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import json
import math
import re
import struct
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, NamedTuple, Protocol

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from openai.lib import type_to_response_format_param
from openai.types import CompletionUsage
from pydantic import BaseModel, ValidationError

from . import logs, prompts
from .cache import Cache
from .config import ReasoningEffort, Settings
from .db import Db
from .domain import (
    ComparisonId,
    DocumentId,
    ErrorCode,
    KeyFact,
    Page,
    Purpose,
    Source,
    SummaryId,
    SummaryLength,
    SummaryOptions,
    SummaryTone,
    Turn,
    TurnId,
    Usage,
    VaultError,
)
from .pages import SENTENCE_OR_PARAGRAPH_END, TABLE_CELL, collapse_whitespace
from .prompts import (
    DOCUMENT_TYPES,
    AnswerDraft,
    CellDraft,
    CompareDoc,
    ComparisonDraft,
    CondensedQuery,
    EvidenceDraft,
    InsightsDraft,
    KeyFactDraft,
    Prompt,
    RowDraft,
    SummaryDraft,
    SummarySourceDraft,
    Tier,
)

type Vector = list[float]

# A caller waits at most this long for one retry. A longer Retry-After fails at once as retryable,
# because a retry inside the provider's rate-limit window fails again.
MAX_RETRY_WAIT_S = 10.0

# Celery retries these with a countdown, which frees the worker and the document lease between
# tries. Retrying them here too would multiply the attempts and hold both while sleeping.
BACKGROUND = frozenset({Purpose.EMBED_CHUNKS, Purpose.INSIGHTS})

# No repair turn. A comparison and a summary are synchronous and the web proxy gives up at 120 s, so one call of
# at most compare_timeout_s or summary_timeout_s (90 s) lets the API answer first; invalid output fails as a
# retryable 502, for the UI's Try again.
NO_REPAIR = frozenset({Purpose.COMPARE, Purpose.SUMMARY})

# One provider attempt per call, with no gateway retry: Celery retries BACKGROUND, and the UI's Try again a comparison
# or a summary.
ONE_ATTEMPT = BACKGROUND | NO_REPAIR

SPEND_TTL_S = 10.0   # how stale today's spend may be when a budget check reads it
EMBEDDING_TTL_S = 30 * 24 * 3600


@dataclass(frozen=True, slots=True)
class DocOwner:
    document_id: DocumentId


@dataclass(frozen=True, slots=True)
class TurnOwner:
    turn_id: TurnId


@dataclass(frozen=True, slots=True)
class ComparisonOwner:
    comparison_id: ComparisonId


@dataclass(frozen=True, slots=True)
class SummaryOwner:
    summary_id: SummaryId


type Owner = DocOwner | TurnOwner | ComparisonOwner | SummaryOwner   # every call is billed to exactly one of them


class Tokens(NamedTuple):
    input: int          # includes cached
    cached_input: int
    output: int         # includes reasoning tokens, which are billed as output


@dataclass(frozen=True, slots=True)
class Price:
    input_per_mtok: Decimal
    cached_input_per_mtok: Decimal
    output_per_mtok: Decimal

    def usd(self, tokens: Tokens) -> Decimal:
        return ((tokens.input - tokens.cached_input) * self.input_per_mtok
                + tokens.cached_input * self.cached_input_per_mtok
                + tokens.output * self.output_per_mtok) / 1_000_000


# USD per 1M tokens, Standard tier, from https://developers.openai.com/api/docs/pricing
# (platform.openai.com/docs/pricing redirects there), fetched 2026-11-11. Flagship rates are the
# short-context rates (input of 272K tokens or less); the insights budget keeps prompts far below that.
# A model missing here records cost_usd NULL and counts in metrics as unpriced.
PRICES: dict[str, Price] = {
    "gpt-6.1-sol": Price(Decimal("2.00"), Decimal("0.10"), Decimal("10.00")),
    "gpt-6.1-terra": Price(Decimal("1.00"), Decimal("0.05"), Decimal("5.00")),
    "gpt-6-luna": Price(Decimal("0.10"), Decimal("0.01"), Decimal("0.50")),
    "text-embedding-3-small": Price(Decimal("0.02"), Decimal("0.02"), Decimal("0")),
    "text-embedding-3-large": Price(Decimal("0.13"), Decimal("0.13"), Decimal("0")),
    "fake-chat": Price(Decimal("0"), Decimal("0"), Decimal("0")),
    "fake-hash": Price(Decimal("0"), Decimal("0"), Decimal("0")),
}


@dataclass(frozen=True, slots=True)
class RawCompletion:
    text: str                      # the JSON text ("" when the model refused)
    refusal: str | None = None


class ProviderError(Exception):
    def __init__(self, code: str, *, retryable: bool, retry_after_s: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after_s = retry_after_s


class TextSink(Protocol):
    """Receives a completion's raw output while the provider generates it."""

    def attempt(self) -> None:
        """A provider attempt starts: the first, a retry or the repair turn. Text from earlier attempts is void."""
        ...

    def text(self, chunk: str) -> None:
        """The next piece of this attempt's raw output (JSON text), in order."""
        ...


class ChatBackend(Protocol):
    provider: str
    models: Mapping[Tier, str]     # the backend owns its model ids; the ledger records models[prompt.tier]

    def complete_json(self, prompt: Prompt, output: type[BaseModel], *, timeout_s: float,
                      on_text: Callable[[str], None] | None = None) -> tuple[RawCompletion, Tokens]:
        """One provider round trip that must return JSON matching `output`'s schema.

        With on_text, the backend streams and passes each piece of output to it as it arrives; the
        RawCompletion still holds the whole text. Raises ProviderError on a provider failure. Retrying
        is the gateway's job.
        """
        ...


class EmbedBackend(Protocol):
    provider: str
    model: str              # priced id, recorded on ledger rows
    dimensions: int
    min_similarity: float   # retrieval gate threshold, calibrated per embedding model

    def embed(self, texts: Sequence[str], *, timeout_s: float) -> tuple[list[Vector], Tokens]:
        """L2-normalised vectors of length `dimensions` in input order, and the tokens billed."""
        ...


@dataclass(frozen=True, slots=True)
class _Billing:
    """What one provider attempt is recorded as in llm_calls."""

    purpose: Purpose
    owner: Owner
    provider: str
    model: str
    prompt: Prompt | None = None


# An owner's spend, for a query that joins llm_calls as l and groups by the owner. count(cost_usd) skips NULLs: when it
# falls short of count(id), some call was unpriced and the total is unknown.
USAGE_SQL = """count(l.id) AS calls, coalesce(sum(l.input_tokens), 0) AS input_tokens,
       coalesce(sum(l.output_tokens), 0) AS output_tokens,
       CASE WHEN count(l.cost_usd) = count(l.id) THEN coalesce(sum(l.cost_usd), 0) END AS cost_usd"""


def usage_of(row: Mapping[str, Any]) -> Usage:
    return Usage(row["calls"], row["input_tokens"], row["output_tokens"], row["cost_usd"])


class DailySpend:
    """Today's spend in the ledger, the UTC day so far, read at most once per SPEND_TTL_S. Thread-safe.

    A budget check runs before every model call; the cache keeps that from summing the day's ledger rows each
    time. The clock is injectable for tests.
    """

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._read: tuple[datetime, Decimal] | None = None   # (read at, usd since that day's midnight)

    def today(self, ledger: Db) -> tuple[Decimal, datetime]:
        """The spend since midnight UTC and the time it was asked for."""
        now = self.clock()
        midnight = _midnight(now)
        with self._lock:
            if (self._read is None or _midnight(self._read[0]) != midnight
                    or (now - self._read[0]).total_seconds() >= SPEND_TTL_S):
                with ledger.connection() as conn:
                    usd = conn.execute("SELECT coalesce(sum(cost_usd), 0) AS usd FROM llm_calls WHERE at >= %s",
                                       (midnight,)).fetchone()["usd"]
                self._read = (now, usd)
            return self._read[1], now


@dataclass(frozen=True, slots=True)
class AI:
    """Gateway. Constructed once per process by build()."""

    chat: ChatBackend
    embedder: EmbedBackend
    ledger: Db
    settings: Settings
    sleep: Callable[[float], None] = time.sleep
    spend: DailySpend = field(default_factory=DailySpend)
    cache: Cache = field(default_factory=Cache.off)

    @property
    def mode(self) -> Literal["live", "fake", "mixed"]:
        fakes = [backend.provider == "fake" for backend in (self.chat, self.embedder)]
        return "fake" if all(fakes) else "live" if not any(fakes) else "mixed"

    @property
    def embed_model(self) -> str:
        """The embedding space stored in chunks.embedding_model. It names the dimension, so a changed
        EMBED_DIMENSIONS makes reconcile re-embed instead of comparing vectors of two sizes."""
        return f"{self.embedder.model}-{self.embedder.dimensions}"

    @property
    def min_similarity(self) -> float:
        return self.embedder.min_similarity

    def budget(self) -> tuple[Decimal, Decimal] | None:
        """(daily_ai_budget_usd, today's spend), or None when no cap is set."""
        limit = self.settings.daily_ai_budget_usd
        return None if limit is None else (limit, self.spend.today(self.ledger)[0])

    def check_budget(self, purpose: Purpose) -> None:
        """Raise VaultError(QUOTA_EXCEEDED) when today's spend (UTC) has reached daily_ai_budget_usd.

        An interactive purpose fails retryable, with details naming the cap, the spend, the next midnight UTC and
        the seconds until it. Insights fail for good: a Celery retry seconds later would meet the same cap, and
        the document page offers its own Retry. embed() never checks, so uploads are still indexed.
        """
        limit = self.settings.daily_ai_budget_usd
        if limit is None:
            return
        spent, now = self.spend.today(self.ledger)
        if spent < limit:
            return
        resets_at = _midnight(now) + timedelta(days=1)
        details: dict[str, object] = {
            "limit_usd": float(limit), "spent_today_usd": float(spent), "resets_at": resets_at.isoformat(),
            "retry_after_s": max(1, math.ceil((resets_at - now).total_seconds()))}
        if purpose is Purpose.INSIGHTS:
            raise VaultError(ErrorCode.QUOTA_EXCEEDED, f"Insights were not written because today's AI spending cap "
                             f"of {_dollars(limit)} is used up. Retry after midnight UTC.", details=details)
        raise VaultError(ErrorCode.QUOTA_EXCEEDED, f"Today's AI spending cap of {_dollars(limit)} is used up. "
                         "It resets at midnight UTC.", retryable=True, details=details)

    def embed(self, texts: Sequence[str], *, purpose: Purpose, owner: Owner) -> list[Vector]:
        """Embed any number of texts. Output order matches input order.

        Every text is looked up in the cache first. Only the misses go to the
        provider, each distinct text once, in batches of embed_batch_size, with
        one ledger row per attempt per batch; texts that are all hits write no
        row. Each batch is cached as soon as it is paid for, so a later batch's
        failure does not lose it. Raises VaultError(AI_UNAVAILABLE) when a batch
        fails (after the gateway's retries for interactive purposes); the
        pipeline turns a retryable one into a Celery retry.
        """
        keys = {text: self._embedding_key(text) for text in texts}
        cached = dict(zip(keys, self.cache.get_many(list(keys.values())), strict=True))
        hits = sum(cached[text] is not None for text in texts)
        self.cache.count("embedding", hits, len(texts) - hits)
        vectors = {text: _unpacked(value) for text, value in cached.items() if value is not None}
        misses = [text for text, value in cached.items() if value is None]
        billing = _Billing(purpose, owner, self.embedder.provider, self.embedder.model)
        size = self.settings.embed_batch_size
        for at in range(0, len(misses), size):
            batch = misses[at:at + size]
            fresh = dict(zip(batch, self._call(
                lambda timeout_s, batch=batch: self.embedder.embed(batch, timeout_s=timeout_s), billing), strict=True))
            self.cache.set_many({keys[text]: _packed(vector) for text, vector in fresh.items()}, EMBEDDING_TTL_S)
            vectors |= fresh
        return [vectors[text] for text in texts]

    def _embedding_key(self, text: str) -> str:
        """The model and the dimension name the embedding space, so a changed one never reads another's vectors."""
        digest = hashlib.sha256(text.encode()).hexdigest()
        return f"docvault:emb:{self.embedder.model}:{self.embedder.dimensions}:{digest}"

    def complete[T: BaseModel](self, prompt: Prompt, output: type[T], *, owner: Owner,
                               stream: TextSink | None = None) -> T:
        """Run a prompt and return its validated output model.

        Output that fails validation gets one repair turn, which shows the model
        its JSON and the errors. A refusal or a second failure raises
        VaultError(AI_BAD_OUTPUT). A NO_REPAIR purpose gets no repair turn: its
        invalid output raises a retryable AI_BAD_OUTPUT. A provider failure raises
        VaultError(AI_UNAVAILABLE), after the gateway's retries for interactive purposes.
        check_budget runs before the first call and before the repair turn, so either
        can raise VaultError(QUOTA_EXCEEDED).
        With a stream, every attempt streams its raw output into it, each one announced by
        stream.attempt(). The LLM is never called inside a database transaction: callers commit first.
        """
        raw = self._complete_once(prompt, output, owner, stream)
        try:
            return _validated(output, raw)
        except ValidationError as exc:
            if prompt.purpose in NO_REPAIR:
                raise VaultError(ErrorCode.AI_BAD_OUTPUT, "The model returned invalid output.", retryable=True) from exc
            repair = prompts.repair(prompt, raw.text, _describe(exc))
        try:
            return _validated(output, self._complete_once(repair, output, owner, stream))
        except ValidationError as exc:
            raise VaultError(ErrorCode.AI_BAD_OUTPUT, "The model returned invalid output twice.",
                             retryable=False) from exc

    def _complete_once(self, prompt: Prompt, output: type[BaseModel], owner: Owner,
                       stream: TextSink | None) -> RawCompletion:
        self.check_budget(prompt.purpose)
        billing = _Billing(prompt.purpose, owner, self.chat.provider, self.chat.models[prompt.tier], prompt)

        def attempt(timeout_s: float) -> tuple[RawCompletion, Tokens]:
            if stream is None:
                return self.chat.complete_json(prompt, output, timeout_s=timeout_s)
            stream.attempt()
            return self.chat.complete_json(prompt, output, timeout_s=timeout_s, on_text=stream.text)

        return self._call(attempt, billing)

    def _call[R](self, call: Callable[[float], tuple[R, Tokens]], billing: _Billing) -> R:
        """Call the provider with the purpose's timeout, writing one ledger row per attempt.

        Interactive purposes get ai_max_attempts tries with doubling backoff;
        ONE_ATTEMPT purposes get one. Only ProviderError counts as an attempt.
        Anything else is a bug or a crash, and it propagates without a row.
        """
        purpose, settings = billing.purpose, self.settings
        timeout_s = {Purpose.INSIGHTS: settings.insights_timeout_s, Purpose.COMPARE: settings.compare_timeout_s,
                     Purpose.SUMMARY: settings.summary_timeout_s}.get(purpose, settings.chat_timeout_s)
        attempts = 1 if purpose in ONE_ATTEMPT else settings.ai_max_attempts
        for attempt in itertools.count(1):
            started = time.perf_counter()
            try:
                result, tokens = call(timeout_s)
            except ProviderError as exc:
                self._record(billing, Tokens(0, 0, 0), _elapsed_ms(started), exc.code)
                wait = max(self.settings.ai_backoff_s * 2 ** (attempt - 1), exc.retry_after_s or 0.0)
                if not exc.retryable or attempt >= attempts or wait > MAX_RETRY_WAIT_S:
                    raise VaultError(ErrorCode.AI_UNAVAILABLE,
                                     f"The AI provider could not complete the request ({exc.code}).",
                                     retryable=exc.retryable) from exc
                self.sleep(wait)
            else:
                self._record(billing, tokens, _elapsed_ms(started), None)
                return result
        raise AssertionError("itertools.count does not end")

    def _record(self, billing: _Billing, tokens: Tokens, latency_ms: int, error_code: str | None) -> None:
        """INSERT INTO llm_calls on the ledger pool (autocommit), so the row
        survives even when the caller's later work rolls back. Spent money stays spent.
        cost_usd is NULL when the model is unpriced. request_id is the API request's, or the one
        its Celery task carried, so a request's spend can be traced from its log line."""
        price = PRICES.get(billing.model)
        prompt, owner = billing.prompt, billing.owner
        with self.ledger.connection() as conn:
            conn.execute(
                "INSERT INTO llm_calls (purpose, provider, model, prompt_name, prompt_version, document_id, turn_id,"
                " comparison_id, summary_id, input_tokens, output_tokens, cost_usd, latency_ms, ok, error_code,"
                " request_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (billing.purpose, billing.provider, billing.model, prompt and prompt.name, prompt and prompt.version,
                 owner.document_id if isinstance(owner, DocOwner) else None,
                 owner.turn_id if isinstance(owner, TurnOwner) else None,
                 owner.comparison_id if isinstance(owner, ComparisonOwner) else None,
                 owner.summary_id if isinstance(owner, SummaryOwner) else None,
                 tokens.input, tokens.output, None if price is None else price.usd(tokens), latency_ms,
                 error_code is None, error_code, logs.request_id.get()))


def _validated[T: BaseModel](output: type[T], raw: RawCompletion) -> T:
    if raw.refusal is not None:
        raise VaultError(ErrorCode.AI_BAD_OUTPUT, "The model declined to produce this output.", retryable=False)
    return output.model_validate_json(raw.text)


def _describe(exc: ValidationError) -> str:
    """'amount: Input should be a valid integer; ...', without the doc URLs str(exc) adds."""
    errors = []
    for error in exc.errors(include_url=False):
        where = ".".join(str(part) for part in error["loc"])
        errors.append(f"{where}: {error['msg']}" if where else error["msg"])
    return "; ".join(errors)


def _dollars(amount: Decimal) -> str:
    """$5.00, $12.50, $0.000001: two decimals, or as many as the amount needs."""
    return f"${amount:.{max(2, -int(amount.normalize().as_tuple().exponent))}f}"


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _midnight(at: datetime) -> datetime:
    return at.replace(hour=0, minute=0, second=0, microsecond=0)


def _packed(vector: Vector) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpacked(value: bytes) -> Vector:
    return list(struct.unpack(f"<{len(value) // 4}f", value))


def build(settings: Settings, ledger: Db, cache: Cache | None = None) -> AI:
    """Pick backends from resolved settings: OpenAIChat | FakeChat and OpenAIEmbedder | FakeEmbedder."""

    @functools.cache
    def client() -> OpenAI:
        # SDK retries are off because the gateway retries, and each of its attempts gets a ledger row.
        key = settings.openai_api_key
        return OpenAI(api_key=None if key is None else key.get_secret_value(), max_retries=0)

    chat: ChatBackend = OpenAIChat(
        client(), {"main": settings.openai_chat_model, "small": settings.openai_small_model},
        {"main": settings.openai_chat_reasoning_effort, "small": settings.openai_small_reasoning_effort},
    ) if settings.chat_provider == "openai" else FakeChat(settings.fake_stream_delay_ms)
    embedder: EmbedBackend = OpenAIEmbedder(
        client(), settings.openai_embed_model, settings.embed_dimensions,
    ) if settings.embed_provider == "openai" else FakeEmbedder(settings.embed_dimensions)
    return AI(chat, embedder, ledger, settings, cache=Cache.off() if cache is None else cache)


# ------------------------------------------------------------------ backends


class OpenAIChat:
    """Chat Completions with strict JSON-schema output. Maps a Prompt to a request and back, nothing more."""

    provider = "openai"

    def __init__(self, client: OpenAI, models: Mapping[Tier, str], efforts: Mapping[Tier, ReasoningEffort]) -> None:
        self.models = models
        self._client, self._efforts = client, efforts

    def complete_json(self, prompt: Prompt, output: type[BaseModel], *, timeout_s: float,
                      on_text: Callable[[str], None] | None = None) -> tuple[RawCompletion, Tokens]:
        # create() rather than parse(): the raw text feeds the repair turn and the usage feeds the ledger.
        request = {
            "model": self.models[prompt.tier],
            "messages": [{"role": "developer", "content": prompt.system},
                         *({"role": m.role, "content": m.content} for m in prompt.messages)],
            "response_format": type_to_response_format_param(output),
            "reasoning_effort": self._efforts[prompt.tier],
            "timeout": timeout_s,
        }
        if on_text is not None:
            return self._streamed(request, on_text)
        try:
            response = self._client.chat.completions.create(**request)
        except (APIConnectionError, APIStatusError) as exc:
            raise _provider_error(exc) from exc
        if not response.choices or response.usage is None:
            raise ProviderError("malformed_response", retryable=True)
        message = response.choices[0].message
        return RawCompletion(message.content or "", message.refusal), _usage_tokens(response.usage)

    def _streamed(self, request: dict, on_text: Callable[[str], None]) -> tuple[RawCompletion, Tokens]:
        """Usage comes from the final chunk, which include_usage adds with no choices."""
        text: list[str] = []
        refusal: list[str] = []
        usage: CompletionUsage | None = None
        answered = False
        try:
            with self._client.chat.completions.create(
                    **request, stream=True, stream_options={"include_usage": True}) as chunks:
                for chunk in chunks:
                    usage = chunk.usage or usage
                    if not chunk.choices:
                        continue
                    answered = True
                    delta = chunk.choices[0].delta
                    if delta.refusal:
                        refusal.append(delta.refusal)
                    if delta.content:
                        text.append(delta.content)
                        on_text(delta.content)
        except (APIConnectionError, APIStatusError) as exc:
            raise _provider_error(exc) from exc
        except APIError as exc:   # an error event inside a 200 stream
            raise ProviderError(exc.code or "stream_error", retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("malformed_response", retryable=True) from exc
        if not answered or usage is None:
            raise ProviderError("malformed_response", retryable=True)
        return RawCompletion("".join(text), "".join(refusal) or None), _usage_tokens(usage)


def _usage_tokens(usage: CompletionUsage) -> Tokens:
    cached = usage.prompt_tokens_details and usage.prompt_tokens_details.cached_tokens
    return Tokens(usage.prompt_tokens, cached or 0, usage.completion_tokens)


class OpenAIEmbedder:
    provider = "openai"
    # Calibrated on the frozen case set (docs/evals.md): the lowest answerable turn scored 0.239, and the
    # out-of-scope turns (0.257 to 0.514) overlap the answerable ones, so no threshold separates them. The gate
    # only screens off-topic questions; the answer step declines what the documents do not say.
    min_similarity = 0.20

    def __init__(self, client: OpenAI, model: str, dimensions: int) -> None:
        self.model, self.dimensions = model, dimensions
        self._client = client

    def embed(self, texts: Sequence[str], *, timeout_s: float) -> tuple[list[Vector], Tokens]:
        try:
            response = self._client.embeddings.create(model=self.model, input=list(texts),
                                                      dimensions=self.dimensions, timeout=timeout_s)
        except (APIConnectionError, APIStatusError) as exc:
            raise _provider_error(exc) from exc
        except ValueError as exc:   # the SDK's response parser raises it when "data" is empty or null
            raise ProviderError("malformed_response", retryable=True) from exc
        if len(response.data) != len(texts) or response.usage is None:
            raise ProviderError("malformed_response", retryable=True)
        ordered = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in ordered], Tokens(response.usage.prompt_tokens, 0, 0)


def _provider_error(exc: APIConnectionError | APIStatusError) -> ProviderError:
    """The retry policy for SDK failures. APITimeoutError subclasses APIConnectionError, so it comes first."""
    match exc:
        case APITimeoutError():
            return ProviderError("timeout", retryable=True)
        case APIConnectionError():
            return ProviderError("connection_error", retryable=True)
        case RateLimitError(code="insufficient_quota"):
            return ProviderError("insufficient_quota", retryable=False)
        case RateLimitError(response=response):
            return ProviderError("rate_limited", retryable=True,
                                 retry_after_s=_seconds(response.headers.get("retry-after")))
        case InternalServerError():
            return ProviderError("server_error", retryable=True)
        case APIStatusError(code=code, status_code=status):
            return ProviderError(code or f"http_{status}", retryable=False)


def _seconds(header: str | None) -> float | None:
    """Retry-After in seconds. The HTTP-date form is ignored."""
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None


class FakeEmbedder:
    """Deterministic content-dependent embeddings, so retrieval tests mean something.

    Hashing-trick bag of words: lowercase, strip punctuation, drop English
    stopwords, crude suffix stemming; each token adds 1 + log(tf) to bucket
    blake2b(token) % dimensions with a hash-derived sign; L2-normalise. Texts
    sharing content words are near; unrelated texts are near-orthogonal. Same
    text, same vector, on every machine.
    """

    provider = "fake"
    model = "fake-hash"
    # Calibrated by `python -m docvault.evals --calibrate` on evals/cases.yaml (docs/evals.md has the margins).
    # tests/test_evals.py fails when the best value on the case set moves.
    min_similarity = 0.184

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str], *, timeout_s: float) -> tuple[list[Vector], Tokens]:
        return [self._vector(text) for text in texts], Tokens(sum(len(text.split()) for text in texts), 0, 0)

    def _vector(self, text: str) -> Vector:
        counts = Counter(_tokens(text))
        vector = [0.0] * self.dimensions
        # A text with no content words still needs a non-zero vector: cosine distance to zero is undefined.
        for token, count in (counts or {"": 1}).items():
            digest = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest())
            vector[digest % self.dimensions] += (1 if digest >> 63 else -1) * (1 + math.log(count))
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]


_STOPWORDS = frozenset(
    "a an and are as at be by for from has have if in is it its of on or that the this to was we were what "
    "when which will with you your our us not no any all does do".split())


def _tokens(text: str) -> list[str]:
    return [_stem(word) for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in _STOPWORDS]


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


_ENDS_SENTENCE = re.compile(r"""[.!?]["')]?$""")


def _segments(text: str) -> list[str]:
    """Extracted page text cut into the passages a reader sees, each a verbatim span of `text`.

    PDF extraction ends a line wherever the layout wrapped, and puts a label and its
    value on separate lines. A line joins the passage before it when it starts with a
    lowercase letter, '$' or a digit, when the line before ends with ':', or when the
    line before is 60 characters or longer and does not end a sentence. A DOCX table
    row stands alone: the long 'Location ... | address' row would otherwise swallow
    the row after it. Passages are then split at sentence ends.
    """
    passages: list[list[str]] = []
    for line in (raw.strip() for raw in text.split("\n")):
        previous = passages[-1][-1] if passages and passages[-1] else None
        if not line:
            passages.append([])
        elif previous is not None and _continues(previous, line):
            passages[-1].append(line)
        else:
            passages.append([line])
    return [part for lines in passages if lines
            for part in SENTENCE_OR_PARAGRAPH_END.split("\n".join(lines)) if part]


def _continues(previous: str, line: str) -> bool:
    if TABLE_CELL in previous or TABLE_CELL in line:
        return False
    return line[0].islower() or line[0] == "$" or line[0].isdigit() or previous.endswith(":") or _wrapped(previous)


def _wrapped(line: str) -> bool:
    """A long line that stops mid-sentence: the layout wrapped it."""
    return len(line) >= 60 and not _ENDS_SENTENCE.search(line)


def _key_fact(page: int, passage: str) -> KeyFactDraft | None:
    """A short label line ending in ':' over its value, a short label line over a '$' value, or a 'Label | value' row."""
    first, _, rest = passage.partition("\n")
    if rest and not _wrapped(first) and not _ENDS_SENTENCE.search(first) and (first.endswith(":") or rest.startswith("$")):
        label, value = first.removesuffix(":"), rest
    elif not rest and TABLE_CELL in first:
        label, _, value = first.partition(TABLE_CELL)
    else:
        return None
    return KeyFactDraft(label=label, value=collapse_whitespace(value), page=page, quote=collapse_whitespace(passage))


def _fake_insights(prompt: Prompt) -> InsightsDraft:
    """Title: the first all-capitals line of three or more words, else the filename. Type: the
    first document type named in the title, else "other". Summary: the first three passages.
    Key facts: label-and-value passages in page order, at most 12, each quoting its passage.
    Tags: up to six title words longer than three letters."""
    pages: tuple[Page, ...] = prompt.inputs["pages"]
    lines = [line.strip() for page in pages for line in page.text.split("\n")]
    title = next((line for line in lines if line.isupper() and len(line.split()) >= 3), prompt.inputs["filename"])
    words = re.findall(r"[a-z]+", title.lower())
    passages = [(page.number, passage) for page in pages for passage in _segments(page.text)]
    return InsightsDraft(
        title=title,
        document_type=next((word for word in words if word in DOCUMENT_TYPES), "other"),
        summary=" ".join(collapse_whitespace(passage) for _, passage in passages[:3]),
        key_facts=[fact for page, passage in passages if (fact := _key_fact(page, passage))][:12],
        tags=list(dict.fromkeys(word for word in words if len(word) > 3))[:6])


def _fake_compare(prompt: Prompt) -> ComparisonDraft:
    documents: tuple[CompareDoc, ...] = prompt.inputs["documents"]
    focus: str | None = prompt.inputs["focus"]
    fields: list[dict[str, KeyFact | KeyFactDraft]] = []
    for doc in documents:
        found = [*doc.key_facts, *(fact for page in doc.pages for passage in _segments(page.text)
                                   if (fact := _key_fact(page.number, passage)))]
        fields.append({})
        for fact in found:
            fields[-1].setdefault(" ".join(re.findall(r"[a-z0-9]+", fact.label.lower())), fact)
    wanted = set(_tokens(focus or ""))
    rows: list[RowDraft] = []
    for label in dict.fromkeys(label for by_label in fields for label in by_label):
        facts = [by_label.get(label) for by_label in fields]
        named = [fact for fact in facts if fact is not None]
        if len(named) < 2 or (focus and not wanted & set(_tokens(named[0].label))):
            continue
        rows.append(RowDraft(field=named[0].label, cells=[
            CellDraft(document=doc.label, value=None, page=None, quote=None) if fact is None else
            CellDraft(document=doc.label, value=fact.value, page=fact.page, quote=fact.quote)
            for doc, fact in zip(documents, facts, strict=True)]))
    return ComparisonDraft(rows=rows)


_FAKE_SUMMARY_PASSAGES: dict[SummaryLength, int] = {
    SummaryLength.BRIEF: 2, SummaryLength.STANDARD: 4, SummaryLength.DETAILED: 8}

_FAKE_SUMMARY_OPENINGS: dict[SummaryTone, str] = {
    SummaryTone.PLAIN: "{filename} in brief.",
    SummaryTone.FORMAL: "This note summarises {filename}.",
    SummaryTone.CLIENT: "Here is what {filename} means for you.",
}


def _fake_summary(prompt: Prompt) -> SummaryDraft:
    """The tone's opening sentence, naming the file, then the length's number of passages (_segments of the
    included pages), whitespace collapsed. With a focus, passages sharing more of its content words come first,
    ties and passages sharing none in document order. A focus word and a passage word are shared when their stems
    are equal or the shorter, of 4 letters or more, starts the longer: _stem leaves "deductibles" as "deductibl" but
    "deductible" whole, and a list marker such as "e." must not match "earthquake". Sources: the passages used, at
    most 6, each quoting itself on its page."""
    pages: tuple[Page, ...] = prompt.inputs["pages"]
    options: SummaryOptions = prompt.inputs["options"]
    passages = [(page.number, collapse_whitespace(passage)) for page in pages for passage in _segments(page.text)]
    wanted = set(_tokens(options.focus or ""))

    def same_stem(a: str, b: str) -> bool:
        shorter, longer = sorted((a, b), key=len)
        return shorter == longer or (len(shorter) >= 4 and longer.startswith(shorter))

    def shared(passage: str) -> int:
        words = set(_tokens(passage))
        return sum(any(same_stem(focus, word) for word in words) for focus in wanted)

    chosen = sorted(passages, key=lambda item: -shared(item[1]))[:_FAKE_SUMMARY_PASSAGES[options.length]]
    opening = _FAKE_SUMMARY_OPENINGS[options.tone].format(filename=prompt.inputs["filename"])
    return SummaryDraft(summary=" ".join([opening, *(passage for _, passage in chosen)]),
                        sources=[SummarySourceDraft(page=page, quote=passage) for page, passage in chosen[:6]])


_REFERENCE_WORD = re.compile(r"\b(?:it|its|that|this|these|those|they|them|one|same|other)\b", re.IGNORECASE)
_CONTINUATION = re.compile(r"^\W*(?:and|also|but|or|what about|how about)\b", re.IGNORECASE)


def _fake_condense(prompt: Prompt) -> CondensedQuery:
    """A follow-up is appended to the previous turn's standalone query, so it carries the earlier topic's words.
    A question is a follow-up when it has a reference word ("does it", "that limit") or opens by continuing the
    last one ("And the premium?", "What about flood?"); any other question already stands alone."""
    history: tuple[Turn, ...] = prompt.inputs["history"]
    question: str = prompt.inputs["question"]
    if _REFERENCE_WORD.search(question) or _CONTINUATION.search(question):
        return CondensedQuery(standalone=f"{history[-1].standalone_query} {question}")
    return CondensedQuery(standalone=question)


def _fake_answer(prompt: Prompt) -> AnswerDraft:
    """Per document, the segment sharing the most content words with the standalone query. Ties go to the
    segment with fewer distinct content words (the tighter match), then the earlier source, then the earlier
    segment. Every document whose best segment shares at least two words is cited, with that segment as both
    the sentence and the quote; with none, the draft is unsupported."""
    query = set(_tokens(prompt.inputs["standalone"]))
    sources: tuple[Source, ...] = prompt.inputs["sources"]
    best: dict[DocumentId, tuple[tuple[int, int, int, int], Source, str]] = {}
    for at, source in enumerate(sources):
        for position, segment in enumerate(_segments(source.text)):
            words = set(_tokens(segment))
            rank = (-len(words & query), len(words), at, position)
            if source.doc.id not in best or rank < best[source.doc.id][0]:
                best[source.doc.id] = (rank, source, segment)
    cited = sorted((rank, source, segment) for rank, source, segment in best.values() if -rank[0] >= 2)
    follow_ups = _fake_follow_ups(sources, query)
    if not cited:
        return AnswerDraft(supported=False, answer="The selected documents do not say.", evidence=[],
                           follow_ups=follow_ups)
    return AnswerDraft(
        supported=True,
        answer=" ".join(f"{collapse_whitespace(segment)} [{source.label}]" for _, source, segment in cited),
        evidence=[EvidenceDraft(source=source.label, quote=segment) for _, source, segment in cited],
        follow_ups=follow_ups)


def _fake_follow_ups(sources: Sequence[Source], query: set[str]) -> list[str]:
    """"What is the <label>?" for the first three labels of label-and-value passages in the sources, in source
    order. A label needs two content words, because _fake_answer cites only a segment sharing two words with
    the question, and a label whose words the query already holds would ask the same question again."""
    labels = (fact.label for source in sources for segment in _segments(source.text)
              if (fact := _key_fact(0, segment)) and len(words := set(_tokens(fact.label))) >= 2
              and not words <= query)
    return [f"What is the {label}?" for label in list(dict.fromkeys(labels))[:3]]


class FakeChat:
    """Deterministic stand-in that reads Prompt.inputs, never the rendered text.

    complete_json looks the prompt's name up in `handlers`. A handler turns the
    prompt into an output model, which goes back as its JSON; a name without a
    handler raises NotImplementedError. Tests subclass FakeChat and replace
    `handlers` to script other output or a provider failure.

    Handlers: insights (_fake_insights), condense (_fake_condense), answer (_fake_answer),
    compare (_fake_compare), summary (_fake_summary).

    Token counts are whitespace word counts (system and messages in, JSON out),
    so ledger and metrics tests have real numbers.
    """

    provider = "fake"
    models: Mapping[Tier, str] = {"main": "fake-chat", "small": "fake-chat"}
    handlers: Mapping[str, Callable[[Prompt], BaseModel]] = {
        "insights": _fake_insights, "condense": _fake_condense, "answer": _fake_answer, "compare": _fake_compare,
        "summary": _fake_summary}

    def __init__(self, stream_delay_ms: int = 0) -> None:
        self.stream_delay_ms = stream_delay_ms

    def complete_json(self, prompt: Prompt, output: type[BaseModel], *, timeout_s: float,
                      on_text: Callable[[str], None] | None = None) -> tuple[RawCompletion, Tokens]:
        handler = self.handlers.get(prompt.name)
        if handler is None:
            raise NotImplementedError(f"FakeChat has no handler for the {prompt.name!r} prompt")
        text = handler(prompt).model_dump_json()
        if on_text is not None:
            for word in _WORD_PIECES.split(text):
                if self.stream_delay_ms:
                    time.sleep(self.stream_delay_ms / 1000)
                on_text(word)
        sent = [prompt.system, *(message.content for message in prompt.messages)]
        return RawCompletion(text), Tokens(sum(len(part.split()) for part in sent), 0, len(text.split()))


_WORD_PIECES = re.compile(r"(?<=\s)(?=\S)")
