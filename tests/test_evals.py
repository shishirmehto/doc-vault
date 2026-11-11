"""The eval harness on the fake provider, in its own database (<test database>_evals)."""

from __future__ import annotations

from docvault import evals
from docvault.ai import FakeEmbedder
from docvault.config import Settings
from docvault.domain import Purpose
from docvault.evals import Ratio

from .conftest import TEST_DATABASE_URL


def test_the_case_set_scores_retrieval_citations_and_refusals_apart():
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, chat_provider="fake", embed_provider="fake")

    report = evals.run(settings, evals.load_cases())

    assert report.retrieval.hits / report.retrieval.total >= 0.6    # measured 21/34 (0.62) on 2026-11-11
    assert report.citation == Ratio(7, 34)
    assert report.not_found == Ratio(15, 16)
    assert (report.grounding, report.recall) == (Ratio(31, 31), Ratio(12, 28))
    assert [(spend.purpose, spend.model, spend.calls) for spend in report.spend] == [
        (Purpose.ANSWER, "fake-chat", 19), (Purpose.CONDENSE, "fake-chat", 8), (Purpose.EMBED_CHUNKS, "fake-hash", 3),
        (Purpose.EMBED_QUERY, "fake-hash", 44), (Purpose.INSIGHTS, "fake-chat", 3)]
    explicit = next(turn for turn in report.turns if turn.case == "deductible-differ-explicit")
    assert [(scored.fact.id, scored.retrieval, scored.cited) for scored in explicit.scored] == [
        ("policy-deductible", None, False), ("quote-deductible", None, True)]
    assert evals.fit_gate(report.turns).threshold == FakeEmbedder.min_similarity, "recalibrate: --calibrate"
