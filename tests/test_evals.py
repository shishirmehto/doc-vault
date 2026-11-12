"""The eval harness on the fake provider, in its own database (<test database>_evals)."""

from __future__ import annotations

import pytest

from docvault import evals
from docvault.ai import FakeEmbedder
from docvault.config import Settings
from docvault.domain import Cell, Purpose, Row, SummaryLength
from docvault.evals import ExpectedField, Fact, FieldResult, Ratio

from .conftest import TEST_DATABASE_URL


def test_the_case_set_scores_retrieval_citations_and_refusals_apart():
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, chat_provider="fake", embed_provider="fake")

    report = evals.run(settings, evals.load_cases())

    assert report.retrieval.hits / report.retrieval.total >= 0.6    # measured 21/34 (0.62) on 2026-11-11
    assert report.citation == Ratio(7, 34)
    assert report.not_found == Ratio(15, 16)
    assert (report.grounding, report.recall) == (Ratio(31, 31), Ratio(12, 28))
    assert report.suggestions == Ratio(108, 108)
    assert (report.cell_grounding, report.difference_recall, report.equal_kept) == (
        Ratio(24, 24), Ratio(3, 4), Ratio(1, 1))
    assert [(result.expected.field, result.row) for result in report.fields if not result.found] == [("premium", None)]
    assert report.summary_grounding == {
        SummaryLength.BRIEF: Ratio(6, 6), SummaryLength.STANDARD: Ratio(12, 12), SummaryLength.DETAILED: Ratio(18, 18)}
    assert [(spend.purpose, spend.model, spend.calls) for spend in report.spend] == [
        (Purpose.ANSWER, "fake-chat", 127), (Purpose.COMPARE, "fake-chat", 1), (Purpose.CONDENSE, "fake-chat", 116),
        (Purpose.EMBED_CHUNKS, "fake-hash", 3), (Purpose.EMBED_QUERY, "fake-hash", 152), (Purpose.INSIGHTS, "fake-chat", 3),
        (Purpose.SUMMARY, "fake-chat", 9)]
    explicit = next(turn for turn in report.turns if turn.case == "deductible-differ-explicit")
    assert [(scored.fact.id, scored.retrieval, scored.cited) for scored in explicit.scored] == [
        ("policy-deductible", None, False), ("quote-deductible", None, True)]
    assert evals.fit_gate(report.turns).threshold == FakeEmbedder.min_similarity, "recalibrate: --calibrate"


POLICY_DEDUCTIBLE = Fact("policy-deductible", "sample-cgl-policy.pdf", 2, "Property Damage Deductible $2,500 per occurrence")
QUOTE_DEDUCTIBLE = Fact("quote-deductible", "sample-renewal-quote.pdf", 2, "Property Damage Deductible $5,000 per occurrence")
DEDUCTIBLE = ExpectedField("property damage deductible", POLICY_DEDUCTIBLE, QUOTE_DEDUCTIBLE, True)


def test_a_row_matches_a_planted_field_only_when_each_cell_quotes_its_fact_and_holds_a_value_from_it():
    renewal = Cell("$5,000 per occurrence", 2, QUOTE_DEDUCTIBLE.text)
    wrong_value = Row("Deductible", (Cell("$5,000", 2, POLICY_DEDUCTIBLE.text), renewal))
    right_value = Row("Property Damage Deductible", (Cell("$2,500  PER Occurrence", 2, POLICY_DEDUCTIBLE.text), renewal))

    assert evals.match_field(DEDUCTIBLE, [wrong_value, right_value]) == FieldResult(DEDUCTIBLE, right_value)
    assert evals.match_field(DEDUCTIBLE, [wrong_value]) == FieldResult(DEDUCTIBLE, None)


def test_an_unknown_fact_in_the_comparison_fields_is_a_reported_problem(tmp_path, monkeypatch):
    facts = (evals.SAMPLES / "facts.yaml").read_text()
    (tmp_path / "facts.yaml").write_text(facts.replace("  quote: quote-premium\n", "  quote: quote-premum\n"))
    monkeypatch.setattr(evals, "SAMPLES", tmp_path)

    with pytest.raises(ValueError) as raised:
        evals.load_cases()

    assert str(raised.value) == f"{evals.CASES}: facts.yaml changes 'premium': unknown fact 'quote-premum'"
