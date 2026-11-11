"""The single door to every model provider, and the ledger behind it.

Callers get one object, AI, with two methods: embed() and complete(). Behind
them sit provider choice, batching, timeouts, retries, JSON-schema output,
validation with one repair attempt, cost pricing, and one llm_calls row per
provider call (failed calls included). No module can reach a provider
without going through here, so token and cost accounting cannot be skipped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel

from .config import Settings
from .db import Db
from .domain import DocumentId, Purpose, TurnId
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
PRICES: dict[str, Price] = {}


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
        batch. Raises
        VaultError(AI_UNAVAILABLE, retryable=True) when the provider fails after
        the SDK's own retries; the pipeline turns that into a Celery retry.
        """
        raise NotImplementedError

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

    def _record(self, *, purpose: Purpose, prompt: Prompt | None, owner: Owner, model: str,
                      input_tokens: int, output_tokens: int, latency_ms: int, error_code: str | None) -> None:
        """INSERT INTO llm_calls on the ledger pool (autocommit), so the row
        survives even when the caller's later work rolls back. Spent money stays spent.
        cost_usd = tokens x PRICES[model], or NULL when the model is unpriced."""
        raise NotImplementedError


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

    def embed(self, texts: Sequence[str]) -> tuple[list[Vector], int]:
        raise NotImplementedError


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
