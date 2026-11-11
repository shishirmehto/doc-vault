"""The single door to every model provider, and the ledger behind it.

Callers get one object, AI, with two methods: embed() and complete(). Behind
them sit provider choice, batching, timeouts, retries with backoff,
JSON-schema output, validation with one repair attempt, cost pricing, and one
llm_calls row per provider attempt (failed attempts included). No module can
reach a provider without going through here, so token and cost accounting
cannot be skipped.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, NamedTuple, Protocol

from openai import APIConnectionError, APIStatusError, APITimeoutError, InternalServerError, OpenAI, RateLimitError
from openai.lib import type_to_response_format_param
from pydantic import BaseModel, ValidationError

from . import prompts
from .config import ReasoningEffort, Settings
from .db import Db
from .domain import DocumentId, ErrorCode, Page, Purpose, Source, Turn, TurnId, VaultError
from .pages import SENTENCE_OR_PARAGRAPH_END, TABLE_CELL, collapse_whitespace
from .prompts import (
    DOCUMENT_TYPES,
    AnswerDraft,
    CondensedQuery,
    EvidenceDraft,
    InsightsDraft,
    KeyFactDraft,
    Prompt,
    Tier,
)

type Vector = list[float]

# A caller waits at most this long for one retry. A longer Retry-After fails at once as retryable,
# because a retry inside the provider's rate-limit window fails again.
MAX_RETRY_WAIT_S = 10.0

# Celery retries these with a countdown, which frees the worker and the document lease between
# tries. Retrying them here too would multiply the attempts and hold both while sleeping.
BACKGROUND = frozenset({Purpose.EMBED_CHUNKS, Purpose.INSIGHTS})


@dataclass(frozen=True, slots=True)
class DocOwner:
    document_id: DocumentId


@dataclass(frozen=True, slots=True)
class TurnOwner:
    turn_id: TurnId


type Owner = DocOwner | TurnOwner   # every call is billed to exactly one document or one chat turn


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


class ChatBackend(Protocol):
    provider: str
    models: Mapping[Tier, str]     # the backend owns its model ids; the ledger records models[prompt.tier]

    def complete_json(self, prompt: Prompt, output: type[BaseModel], *,
                      timeout_s: float) -> tuple[RawCompletion, Tokens]:
        """One provider round trip that must return JSON matching `output`'s schema.

        Raises ProviderError on a provider failure. Retrying is the gateway's job.
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


@dataclass(frozen=True, slots=True)
class AI:
    """Gateway. Constructed once per process by build()."""

    chat: ChatBackend
    embedder: EmbedBackend
    ledger: Db
    settings: Settings
    sleep: Callable[[float], None] = time.sleep

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

    def embed(self, texts: Sequence[str], *, purpose: Purpose, owner: Owner) -> list[Vector]:
        """Embed any number of texts. Output order matches input order.

        Splits into batches of embed_batch_size and writes one ledger row per
        attempt per batch. Raises VaultError(AI_UNAVAILABLE) when a batch fails
        (after the gateway's retries for interactive purposes); the pipeline
        turns a retryable one into a Celery retry.
        """
        billing = _Billing(purpose, owner, self.embedder.provider, self.embedder.model)
        size = self.settings.embed_batch_size
        vectors: list[Vector] = []
        for at in range(0, len(texts), size):
            batch = texts[at:at + size]
            vectors += self._call(lambda timeout_s, batch=batch: self.embedder.embed(batch, timeout_s=timeout_s),
                                  billing)
        return vectors

    def complete[T: BaseModel](self, prompt: Prompt, output: type[T], *, owner: Owner) -> T:
        """Run a prompt and return its validated output model.

        Output that fails validation gets one repair turn, which shows the model
        its JSON and the errors. A refusal or a second failure raises
        VaultError(AI_BAD_OUTPUT). A provider failure raises
        VaultError(AI_UNAVAILABLE), after the gateway's retries for interactive purposes.
        The LLM is never called inside a database transaction: callers commit first.
        """
        raw = self._complete_once(prompt, output, owner)
        try:
            return _validated(output, raw)
        except ValidationError as exc:
            repair = prompts.repair(prompt, raw.text, _describe(exc))
        try:
            return _validated(output, self._complete_once(repair, output, owner))
        except ValidationError as exc:
            raise VaultError(ErrorCode.AI_BAD_OUTPUT, "The model returned invalid output twice.",
                             retryable=False) from exc

    def _complete_once(self, prompt: Prompt, output: type[BaseModel], owner: Owner) -> RawCompletion:
        billing = _Billing(prompt.purpose, owner, self.chat.provider, self.chat.models[prompt.tier], prompt)
        return self._call(lambda timeout_s: self.chat.complete_json(prompt, output, timeout_s=timeout_s), billing)

    def _call[R](self, call: Callable[[float], tuple[R, Tokens]], billing: _Billing) -> R:
        """Call the provider with the purpose's timeout, writing one ledger row per attempt.

        Interactive purposes get ai_max_attempts tries with doubling backoff;
        BACKGROUND purposes get one. Only ProviderError counts as an attempt.
        Anything else is a bug or a crash, and it propagates without a row.
        """
        purpose = billing.purpose
        timeout_s = self.settings.insights_timeout_s if purpose is Purpose.INSIGHTS else self.settings.chat_timeout_s
        attempts = 1 if purpose in BACKGROUND else self.settings.ai_max_attempts
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
        cost_usd is NULL when the model is unpriced."""
        price = PRICES.get(billing.model)
        prompt, owner = billing.prompt, billing.owner
        with self.ledger.connection() as conn:
            conn.execute(
                "INSERT INTO llm_calls (purpose, provider, model, prompt_name, prompt_version, document_id, turn_id,"
                " input_tokens, output_tokens, cost_usd, latency_ms, ok, error_code)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (billing.purpose, billing.provider, billing.model, prompt and prompt.name, prompt and prompt.version,
                 owner.document_id if isinstance(owner, DocOwner) else None,
                 owner.turn_id if isinstance(owner, TurnOwner) else None,
                 tokens.input, tokens.output, None if price is None else price.usd(tokens), latency_ms,
                 error_code is None, error_code))


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


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def build(settings: Settings, ledger: Db) -> AI:
    """Pick backends from resolved settings: OpenAIChat | FakeChat and OpenAIEmbedder | FakeEmbedder."""

    @functools.cache
    def client() -> OpenAI:
        # SDK retries are off because the gateway retries, and each of its attempts gets a ledger row.
        key = settings.openai_api_key
        return OpenAI(api_key=None if key is None else key.get_secret_value(), max_retries=0)

    chat: ChatBackend = OpenAIChat(
        client(), {"main": settings.openai_chat_model, "small": settings.openai_small_model},
        {"main": settings.openai_chat_reasoning_effort, "small": settings.openai_small_reasoning_effort},
    ) if settings.chat_provider == "openai" else FakeChat()
    embedder: EmbedBackend = OpenAIEmbedder(
        client(), settings.openai_embed_model, settings.embed_dimensions,
    ) if settings.embed_provider == "openai" else FakeEmbedder(settings.embed_dimensions)
    return AI(chat, embedder, ledger, settings)


# ------------------------------------------------------------------ backends


class OpenAIChat:
    """Chat Completions with strict JSON-schema output. Maps a Prompt to a request and back, nothing more."""

    provider = "openai"

    def __init__(self, client: OpenAI, models: Mapping[Tier, str], efforts: Mapping[Tier, ReasoningEffort]) -> None:
        self.models = models
        self._client, self._efforts = client, efforts

    def complete_json(self, prompt: Prompt, output: type[BaseModel], *,
                      timeout_s: float) -> tuple[RawCompletion, Tokens]:
        # create() rather than parse(): the raw text feeds the repair turn and the usage feeds the ledger.
        try:
            response = self._client.chat.completions.create(
                model=self.models[prompt.tier],
                messages=[{"role": "developer", "content": prompt.system},
                          *({"role": m.role, "content": m.content} for m in prompt.messages)],
                response_format=type_to_response_format_param(output),
                reasoning_effort=self._efforts[prompt.tier],
                timeout=timeout_s)
        except (APIConnectionError, APIStatusError) as exc:
            raise _provider_error(exc) from exc
        message, usage = response.choices[0].message, response.usage
        cached = usage.prompt_tokens_details and usage.prompt_tokens_details.cached_tokens
        return (RawCompletion(message.content or "", message.refusal),
                Tokens(usage.prompt_tokens, cached or 0, usage.completion_tokens))


class OpenAIEmbedder:
    provider = "openai"
    min_similarity = 0.25   # uncalibrated; docs/evals.md gives the command that calibrates it with a key

    def __init__(self, client: OpenAI, model: str, dimensions: int) -> None:
        self.model, self.dimensions = model, dimensions
        self._client = client

    def embed(self, texts: Sequence[str], *, timeout_s: float) -> tuple[list[Vector], Tokens]:
        try:
            response = self._client.embeddings.create(model=self.model, input=list(texts),
                                                      dimensions=self.dimensions, timeout=timeout_s)
        except (APIConnectionError, APIStatusError) as exc:
            raise _provider_error(exc) from exc
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
    if not cited:
        return AnswerDraft(supported=False, answer="The selected documents do not say.", evidence=[])
    return AnswerDraft(
        supported=True,
        answer=" ".join(f"{collapse_whitespace(segment)} [{source.label}]" for _, source, segment in cited),
        evidence=[EvidenceDraft(source=source.label, quote=segment) for _, source, segment in cited])


class FakeChat:
    """Deterministic stand-in that reads Prompt.inputs, never the rendered text.

    complete_json looks the prompt's name up in `handlers`. A handler turns the
    prompt into an output model, which goes back as its JSON; a name without a
    handler raises NotImplementedError. Tests subclass FakeChat and replace
    `handlers` to script other output or a provider failure.

    Handlers: insights (_fake_insights), condense (_fake_condense), answer (_fake_answer).

    Token counts are whitespace word counts (system and messages in, JSON out),
    so ledger and metrics tests have real numbers.
    """

    provider = "fake"
    models: Mapping[Tier, str] = {"main": "fake-chat", "small": "fake-chat"}
    handlers: Mapping[str, Callable[[Prompt], BaseModel]] = {
        "insights": _fake_insights, "condense": _fake_condense, "answer": _fake_answer}

    def complete_json(self, prompt: Prompt, output: type[BaseModel], *,
                      timeout_s: float) -> tuple[RawCompletion, Tokens]:
        handler = self.handlers.get(prompt.name)
        if handler is None:
            raise NotImplementedError(f"FakeChat has no handler for the {prompt.name!r} prompt")
        text = handler(prompt).model_dump_json()
        sent = [prompt.system, *(message.content for message in prompt.messages)]
        return RawCompletion(text), Tokens(sum(len(part.split()) for part in sent), 0, len(text.split()))
