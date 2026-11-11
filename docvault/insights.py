"""AI insights for one document. The only writer of document_insights.

Insights run beside indexing, not inside it (per
separate-before-serializing-shared-state): the index pipeline owns
documents.status, this module owns its own row. An insights failure never
blocks chat, and a slow summary never delays READY.
"""

from __future__ import annotations

from collections.abc import Sequence

from .context import Ctx
from .db import Db
from .domain import DocumentId, InsightsBody, Page
from .prompts import InsightsDraft


def run_analyze(ctx: Ctx, doc_id: DocumentId, *, attempt: int = 1) -> None:
    """Generate, verify and store insights. Safe to run any number of times.

    TODO:
      with db.pipeline_steps(ctx.db, doc_id, Lease.ANALYZE, attempt) as steps:
          if steps is None: return
          if row exists with status 'ready' and prompt_version == INSIGHTS_VERSION: return
          pages = SELECT number, text FROM pages WHERE document_id = $1 ORDER BY number
          if no pages: return                       # parse not done yet; index enqueues us again
          with steps.record(Step.ANALYZE):
              prompt, pages_read = prompts.insights(filename, basis, pages, budget_chars=ctx.settings.insights_max_chars)
              draft = ctx.ai.complete(prompt, InsightsDraft, owner=DocOwner(doc_id))   # no transaction open
              body, dropped = verify(draft, pages)
              INSERT ... ON CONFLICT (document_id) DO UPDATE SET status='ready', body, pages_read, dropped_unverified, ...
      Terminal failure (after Celery retries): upsert status='failed', error_code, error_retryable.
    """
    raise NotImplementedError


def verify(draft: InsightsDraft, pages: Sequence[Page]) -> tuple[InsightsBody, int]:
    """Keep only key facts whose quote is found on the cited page (or the next one).

    Uses pages.find_on_page. A kept fact takes the page where the quote was
    actually found and the matched source span as its quote. Returns the body
    and the number of facts dropped; the count is stored so evals and metrics
    can see how often the model cites wrongly.
    """
    raise NotImplementedError


def reset_failed(db: Db, doc_id: DocumentId) -> bool:
    """DELETE a failed row so the state reads 'pending' again. Used by documents.retry."""
    raise NotImplementedError
