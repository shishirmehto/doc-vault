"""Settings, parsed once from the environment at process start (the boundary).

Nothing else reads os.environ. Provider "auto" resolves here, so the rest of
the code sees a concrete choice and never branches on missing keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

type Provider = Literal["auto", "openai", "fake"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://vault:vault@db:5432/vault"
    redis_url: str = "redis://redis:6379/0"
    storage_dir: Path = Path("/data/blobs")

    openai_api_key: SecretStr | None = None
    chat_provider: Provider = "auto"
    embed_provider: Provider = "auto"

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
    insights_timeout_s: float = 120.0


def load_settings() -> Settings:
    """Parse env once. Resolve "auto": a key present selects that provider, none selects fake."""
    settings = Settings()
    resolved = "openai" if settings.openai_api_key and settings.openai_api_key.get_secret_value() else "fake"
    return settings.model_copy(update={
        "chat_provider": resolved if settings.chat_provider == "auto" else settings.chat_provider,
        "embed_provider": resolved if settings.embed_provider == "auto" else settings.embed_provider,
    })
