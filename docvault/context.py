"""The per-process context that commands receive, built once at start.

Commands (upload, run_index, run_analyze, ask, retry, delete, reconcile)
take a Ctx. Reads take only a Db, which tells the reader at the signature
that they cannot call a model or enqueue work.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from . import ai, db
from .config import Settings
from .domain import DocumentId, Sha256


class Jobs(Protocol):
    """How commands ask for background work. tasks.CeleryJobs in production;
    tests pass a recorder and call the pipeline functions directly. Callers
    enqueue only after the commit that makes the work visible to the worker."""

    def index(self, doc_id: DocumentId) -> None: ...
    def analyze(self, doc_id: DocumentId) -> None: ...


@dataclass(frozen=True, slots=True)
class BlobStore:
    """Content-addressed files at <root>/<sha[:2]>/<sha>.

    The path derives from the hash, so the database stores no path and
    writing the same bytes twice is a no-op.
    """

    root: Path

    def path(self, sha: Sha256) -> Path:
        h = sha.hex()
        return self.root / h[:2] / h

    def put(self, stream: BinaryIO, *, max_bytes: int) -> tuple[Sha256, int]:
        """Stream to a temp file under root while hashing. Raise FILE_TOO_LARGE as soon
        as max_bytes is passed and EMPTY_FILE at 0 bytes. os.replace onto path(sha),
        which is atomic. Returns (sha, size). The file is never held in memory."""
        raise NotImplementedError

    def delete(self, sha: Sha256) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Ctx:
    settings: Settings
    db: db.Db
    ai: ai.AI
    blobs: BlobStore
    jobs: Jobs


def build_ctx(settings: Settings, jobs: Jobs) -> Ctx:
    """Called by the API lifespan and once per worker process."""
    db.migrate(settings.database_url)
    pool = db.open_pool(settings.database_url)
    return Ctx(settings, pool, ai.build(settings, pool), BlobStore(settings.storage_dir), jobs)
