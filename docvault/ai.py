"""The single door to every model provider, and the ledger behind it.

Callers get one object, AI, with two methods: embed() and complete(). Behind
them sit provider choice, batching, timeouts, retries, JSON-schema output,
validation with one repair attempt, cost pricing, and one llm_calls row per
provider call (failed calls included). No module can reach a provider
without going through here, so token and cost accounting cannot be skipped.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel

from .config import Settings
from .db import Db
from .domain import DocumentId, ErrorCode, Purpose, TurnId, VaultError
from .prompts import Prompt

type Vector = list[float]


@dataclass(frozen=True, slots=True)
class DocOwner:
    document_id: DocumentId


@dataclass(frozen=True, slots=True)
class TurnOwner:
    turn_id: TurnId


type Owner = DocOwner | TurnOwner   # every call is billed to exactly one document or one chat turn


@dataclass(frozen=True, slots=True)
class Price:
    input_per_mtok: Decimal
    output_per_mtok: Decimal


# USD per million tokens. Verify against provider pricing pages at build time.
# A model missing here records cost_usd NULL and counts in metrics as unpriced.
PRICES: dict[str, Price] = {"fake-hash-1536": Price(Decimal(0), Decimal(0))}


@dataclass(frozen=True, slots=True)
class RawCompletion:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


class ChatBackend(Protocol):
    provider: str

    def complete_json(self, prompt: Prompt, schema: dict[str, object], *, model: str,
                            timeout_s: float) -> RawCompletion:
        """One provider round trip that must return JSON matching `schema`.

        OpenAI: response_format json_schema, strict.
        Raises ProviderError(retryable=True) on timeout, 429 and 5xx,
        ProviderError(retryable=False) on 4xx such as context length.
        """
        ...


class EmbedBackend(Protocol):
    provider: str
    model: str
    min_similarity: float   # retrieval gate threshold, calibrated per embedding model

    def embed(self, texts: Sequence[str]) -> tuple[list[Vector], int]:
        """Vectors (len 1536, L2-normalised) and input tokens billed."""
        ...


class ProviderError(Exception):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class AI:
    """Gateway. Constructed once per process by build()."""

    def __init__(self, embedder: EmbedBackend, ledger: Db, *, embed_batch_size: int) -> None:
        self._embedder = embedder
        self._ledger = ledger
        self._embed_batch_size = embed_batch_size

    @property
    def mode(self) -> Literal["live", "fake"]:
        return "fake" if self._embedder.provider == "fake" else "live"

    @property
    def embed_model(self) -> str:
        return self._embedder.model

    @property
    def min_similarity(self) -> float:
        return self._embedder.min_similarity

    def embed(self, texts: Sequence[str], *, purpose: Purpose, owner: Owner) -> list[Vector]:
        """Embed any number of texts. Output order matches input order.

        Splits into batches of embed_batch_size and writes one ledger row per
        batch. Raises VaultError(AI_UNAVAILABLE) when the provider fails; the
        pipeline turns a retryable one into a Celery retry.
        """
        vectors: list[Vector] = []
        for at in range(0, len(texts), self._embed_batch_size):
            batch = texts[at:at + self._embed_batch_size]
            started = time.perf_counter()
            tokens, error = 0, None
            try:
                batch_vectors, tokens = self._embedder.embed(batch)
            except ProviderError as exc:
                error = exc
            self._record(purpose=purpose, prompt=None, owner=owner, provider=self._embedder.provider,
                         model=self._embedder.model, input_tokens=tokens, output_tokens=0,
                         latency_ms=_elapsed_ms(started), error_code=error and error.code)
            if error:
                raise VaultError(ErrorCode.AI_UNAVAILABLE, "The embedding provider is not responding.",
                                 retryable=error.retryable) from error
            vectors += batch_vectors
        return vectors

    def complete[T: BaseModel](self, prompt: Prompt, output: type[T], *, owner: Owner) -> T:
        """Run a prompt and return its validated output model.

        TODO:
          schema = output.model_json_schema()
          raw = chat.complete_json(prompt, schema, model=models[prompt.tier], timeout_s=...)
          record(raw, ok=True)                       # ledger row even if parsing fails next
          try: return output.model_validate_json(raw.text)
          except ValidationError as e:
              repair = prompt + assistant(raw.text) + user("Your JSON failed validation: {e}. Return corrected JSON only.")
              raw2 = ...; record(raw2)
              try: return output.model_validate_json(raw2.text)
              except ValidationError: raise VaultError(AI_BAD_OUTPUT, retryable=False)
          on ProviderError: record(ok=False, error_code); raise VaultError(AI_UNAVAILABLE, retryable=e.retryable)
        The LLM is never called inside a database transaction: callers commit first.
        """
        raise NotImplementedError

    def _record(self, *, purpose: Purpose, prompt: Prompt | None, owner: Owner, provider: str, model: str,
                input_tokens: int, output_tokens: int, latency_ms: int, error_code: str | None) -> None:
        """INSERT INTO llm_calls on the ledger pool (autocommit), so the row
        survives even when the caller's later work rolls back. Spent money stays spent.
        cost_usd = tokens x PRICES[model], or NULL when the model is unpriced."""
        price = PRICES.get(model)
        cost = price and (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1_000_000
        with self._ledger.connection() as conn:
            conn.execute(
                "INSERT INTO llm_calls (purpose, provider, model, prompt_name, prompt_version, document_id, turn_id,"
                " input_tokens, output_tokens, cost_usd, latency_ms, ok, error_code)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (purpose, provider, model, prompt and prompt.name, prompt and prompt.version,
                 owner.document_id if isinstance(owner, DocOwner) else None,
                 owner.turn_id if isinstance(owner, TurnOwner) else None,
                 input_tokens, output_tokens, cost, latency_ms, error_code is None, error_code))


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def build(settings: Settings, ledger: Db) -> AI:
    """Pick backends from resolved settings: OpenAIChat | FakeChat and OpenAIEmbedder | FakeEmbedder."""
    if settings.embed_provider != "fake" or settings.chat_provider != "fake":
        raise NotImplementedError("only the fake provider is built so far")
    return AI(FakeEmbedder(), ledger, embed_batch_size=settings.embed_batch_size)


# ------------------------------------------------------------------ backends


class OpenAIChat:
    provider = "openai"


class OpenAIEmbedder:
    provider = "openai"
    min_similarity = 0.25   # starting value; calibrated with evals/ on the sample policies


class FakeEmbedder:
    """Deterministic content-dependent embeddings, so retrieval tests mean something.

    Hashing-trick bag of words: lowercase, strip punctuation, drop English
    stopwords, crude suffix stemming; each token adds 1 + log(tf) to bucket
    blake2b(token) % 1536 with a hash-derived sign; L2-normalise. Texts sharing
    content words are near; unrelated texts are near-orthogonal. Same text,
    same vector, on every machine.
    """

    provider = "fake"
    model = "fake-hash-1536"
    min_similarity = 0.30   # with bag-of-words vectors only real word overlap passes

    dimensions = 1536

    def embed(self, texts: Sequence[str]) -> tuple[list[Vector], int]:
        return [self._vector(text) for text in texts], sum(len(text.split()) for text in texts)

    def _vector(self, text: str) -> Vector:
        counts = Counter(_stem(word) for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in _STOPWORDS)
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


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


class FakeChat:
    """Deterministic stand-in that reads Prompt.inputs, never the rendered text.

    condense: previous question's content words + the latest question.
    answer: split every source into sentences, score each by content-word
      overlap with the *original* question (ties broken by the standalone
      query), cite the best sentence verbatim as the evidence quote with
      answer "<sentence> [S#]"; supported=False when the best overlap is < 2 words.
    insights: title = filename, type "other", summary = first three sentences,
      key facts = lines matching "<Label> $<amount>" with their page.
    Token counts = whitespace word counts, so ledger and metrics tests have real numbers.
    """

    provider = "fake"
