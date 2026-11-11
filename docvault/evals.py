"""Prompt and retrieval evals: python -m docvault.evals evals/cases.yaml

Drives chat.start / chat.ask in-process against the configured provider,
then reports per prompt version:
  status accuracy (answered vs not_found), cited-page hit rate,
  must_contain hit rate, unverified-drop rate, cost and p95 latency
  (the last two read from llm_calls by turn id, the same source as /metrics).
Used to calibrate EmbedBackend.min_similarity and to compare answer-v1 with
answer-v2 before switching. Results go into AI_USAGE.md.
"""

from __future__ import annotations

from pathlib import Path


def main(cases: Path) -> int:
    raise NotImplementedError
