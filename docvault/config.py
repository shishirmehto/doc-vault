"""Settings, parsed once from the environment at process start (the boundary).

Nothing else reads os.environ. Provider "auto" resolves here, so the rest of
the code sees a concrete choice and never branches on missing keys.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

type Provider = Literal["auto", "openai", "fake"]
type ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://vault:vault@db:5432/vault"
    redis_url: str = "redis://redis:6379/0"
    storage_dir: Path = Path("/data/blobs")

    openai_api_key: SecretStr | None = None
    chat_provider: Provider = "auto"
    embed_provider: Provider = "auto"
    # Model ids from https://developers.openai.com/api/docs/models, fetched 2026-11-11.
    openai_chat_model: str = "gpt-6.1-sol"                    # main tier: answers and insights
    openai_small_model: str = "gpt-6-luna"                    # small tier: condense
    openai_chat_reasoning_effort: ReasoningEffort = "low"     # sol accepts low..max
    openai_small_reasoning_effort: ReasoningEffort = "none"   # luna accepts none..max
    openai_embed_model: str = "text-embedding-3-small"
    embed_dimensions: int = 1536
    ai_max_attempts: int = 3             # provider attempts per interactive call, the first one included
    ai_backoff_s: float = 0.5            # first retry delay; ai.AI._call owns the policy

    max_upload_bytes: int = 50 * 1024 * 1024
    max_pages: int = 2000
    chunk_max_chars: int = 1800          # about 450 tokens of English
    chunk_overlap_chars: int = 250
    embed_batch_size: int = 128
    retrieval_k: int = 8
    retrieval_per_doc_min: int = 2       # multi-document chats get at least this many sources per document
    history_turns: int = 4
    insights_max_chars: int = 240_000    # about 60k tokens of page-tagged text
    chat_timeout_s: float = 30.0
    fake_stream_delay_ms: int = Field(0, ge=0)
    uploads_per_minute: int = Field(30, ge=1)     # per client, per API process
    questions_per_minute: int = Field(20, ge=1)
    insights_timeout_s: float = 120.0
    compare_timeout_s: float = 90.0
    summary_timeout_s: float = 90.0
    log_format: Literal["json", "text"] = "text"   # json: one object per line, for compose and log shippers
    # Model calls stop for the rest of the UTC day once the ledger's spend reaches it. Unset: no cap.
    daily_ai_budget_usd: Annotated[Decimal, Field(gt=0)] | None = None
    cache_url: str | None = None   # a Redis for the embedding and answer caches (cache.py); unset: no cache

    @field_validator("daily_ai_budget_usd", "cache_url", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        """Compose passes DAILY_AI_BUDGET_USD through from the shell, as an empty string when it is not set there,
        and .env.example ships CACHE_URL empty."""
        return None if value == "" else value


def load_settings() -> Settings:
    """Parse env once. Resolve "auto": a key present selects that provider, none selects fake.

    An explicit openai provider without a key fails here, at process start,
    instead of on the first upload.
    """
    settings = Settings()
    has_key = bool(settings.openai_api_key and settings.openai_api_key.get_secret_value())
    keyless = [f"{name}=openai" for name, provider in (("CHAT_PROVIDER", settings.chat_provider),
                                                       ("EMBED_PROVIDER", settings.embed_provider))
               if provider == "openai" and not has_key]
    if keyless:
        raise ValueError(f"OPENAI_API_KEY is not set, but it is required by {' and '.join(keyless)}.")
    resolved = "openai" if has_key else "fake"
    return settings.model_copy(update={
        "chat_provider": resolved if settings.chat_provider == "auto" else settings.chat_provider,
        "embed_provider": resolved if settings.embed_provider == "auto" else settings.embed_provider,
    })
