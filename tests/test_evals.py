"""The eval harness on the fake provider, in its own database (<test database>_evals)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from docvault import chat, evals
from docvault.config import Settings
from docvault.domain import (
    Cell,
    ChunkId,
    DocRef,
    DocumentId,
    PageBasis,
    PageMap,
    Purpose,
    Row,
    SummaryLength,
    TurnOutcome,
)
from docvault.evals import ExpectedField, Fact, FieldResult, Ratio
from docvault.fake_ai import FakeEmbedder

from .conftest import TEST_DATABASE_URL


def test_the_case_set_scores_retrieval_citations_and_refusals_apart():
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, chat_provider="fake", embed_provider="fake")

    report = evals.run(settings, evals.load_cases())

    assert report.retrieval.hits / report.retrieval.total >= 0.6    # measured 21/34 (0.62) on 2026-11-11
    assert report.citation == Ratio(7, 34)
    assert (report.cited_in_full, report.answer_text) == (Ratio(5, 28), Ratio(0, 0))
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


EXTRA = evals.ROOT / "evals" / "cases-extra.yaml"


def test_the_extra_case_set_scores_whole_turns_and_the_answer_text():
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, chat_provider="fake", embed_provider="fake")

    report = evals.run(settings, evals.load_cases(EXTRA))

    assert (report.cited_in_full, report.answer_text, report.not_found) == (Ratio(0, 8), Ratio(2, 7), Ratio(2, 2))
    assert [(turn.case, turn.text_misses) for turn in report.turns if turn.text_misses is not None] == [
        ("start-end-policy-number-refused-hire", ("missing SAMPLE-CGL-2026-0001",)),
        ("start-end-quote-term-exclusion", ("missing silica",)),
        ("policy-spread-facts", ("missing 1,000,000", "missing 40 feet", "missing 30 days")),
        ("premium-increase", ("missing 3,450",)),
        ("deductible-then-renewal-only", ("missing 5,000",)),
        ("additional-insured-outside-scope", ()),
        ("renewal-premium-outside-scope", ())]


PAGES = evals.ROOT / "evals" / "cases-pages.yaml"


def test_the_page_case_set_loads_and_runs_on_the_fake_provider():
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, chat_provider="fake", embed_provider="fake")
    case_set = evals.load_cases(PAGES)
    turns = [question for case in case_set.cases for question in case.questions]

    report = evals.run(settings, case_set)

    assert (len(case_set.cases), len(turns), sum(len(turn.expected) for turn in turns)) == (6, 6, 5)
    assert (report.retrieval, report.not_found) == (Ratio(5, 5), Ratio(1, 1))
    assert [(turn.case, turn.outcome) for turn in report.turns] == [
        ("policy-last-page", TurnOutcome.MODEL_DECLINED), ("policy-how-it-ends", TurnOutcome.MODEL_DECLINED),
        ("policy-page-3", TurnOutcome.ANSWERED), ("policy-page-5-roofing", TurnOutcome.ANSWERED),
        ("quote-last-page", TurnOutcome.ANSWERED), ("policy-page-out-of-range", TurnOutcome.NO_EVIDENCE)]
    assert [(turn.case, turn.named_page, turn.passes(report.threshold)) for turn in report.turns] == [
        ("policy-last-page", True, True), ("policy-how-it-ends", True, True), ("policy-page-3", True, True),
        ("policy-page-5-roofing", True, True), ("quote-last-page", True, True),
        ("policy-page-out-of-range", False, False)]


def turn_result(similarity: float, *, answerable: bool, named_page: bool = False) -> evals.TurnResult:
    doc = DocRef(DocumentId(UUID(int=1)), POLICY_DEDUCTIBLE.document, PageBasis.PHYSICAL, 7)
    passage = chat.Candidate(ChunkId(uuid4()), doc, "text", PageMap(((0, 1),)), similarity, 0.0, False)
    scored = (evals.Scored(POLICY_DEDUCTIBLE, None, False),) if answerable else ()
    return evals.TurnResult("case", "q", "q", TurnOutcome.ANSWERED, scored, (passage,), named_page, None)


def test_a_turn_that_names_a_page_passes_the_gate_at_any_threshold_and_places_no_cut():
    turns = [turn_result(0.10, answerable=True, named_page=True), turn_result(0.30, answerable=True),
             turn_result(0.20, answerable=False)]

    assert [turn.passes(0.5) for turn in turns] == [True, False, False]
    assert evals.fit_gate(turns) == evals.GateFit(0.25, 3, 3, 0.30, 0.20)


WIRED = """\
partial: checks that the run scores the text chat.ask returned
documents:
  policy: sample-cgl-policy.pdf
  quote: sample-renewal-quote.pdf
cases:
- id: answered
  scope: [policy]
  turns:
  - ask: For my file note, give me the policy period, the per-occurrence limit, the roofing height restriction and the cancellation notice.
    expect: [policy-period]
    values: [coverage territory]
- id: refused
  scope: [quote]
  turns:
  - ask: Who is the additional insured on this account?
    expect: not_found
    forbid: [could not find]
"""


def test_the_run_scores_the_answer_text_that_chat_ask_returned(tmp_path):
    (tmp_path / "cases.yaml").write_text(WIRED)
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, chat_provider="fake", embed_provider="fake")

    report = evals.run(settings, evals.load_cases(tmp_path / "cases.yaml"))

    assert [(turn.case, turn.outcome, turn.text_misses) for turn in report.turns] == [
        ("answered", TurnOutcome.ANSWERED, ()), ("refused", TurnOutcome.NO_EVIDENCE, ("says could not find",))]


SECTIONED = """\
partial: checks that a turn naming a section counts as named, as one naming a page does
documents:
  policy: sample-cgl-policy.pdf
  quote: sample-renewal-quote.pdf
  endorsement: sample-endorsement-additional-insured.docx
cases:
- id: by-section
  scope: [endorsement]
  turns:
  - ask: Who does Endorsement SM 40 03 add?
    expect: [endorsement-additional-insured]
- id: by-meaning
  scope: [endorsement]
  turns:
  - ask: Who is added as an insured?
    expect: [endorsement-additional-insured]
"""


def test_a_turn_that_names_a_section_counts_as_named(tmp_path):
    (tmp_path / "cases.yaml").write_text(SECTIONED)
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, chat_provider="fake", embed_provider="fake")

    report = evals.run(settings, evals.load_cases(tmp_path / "cases.yaml"))

    assert [(turn.case, turn.named_page) for turn in report.turns] == [("by-section", True), ("by-meaning", False)]


def test_the_answer_text_must_hold_each_value_and_no_forbidden_string_ignoring_case_dollars_commas_and_bold():
    assert evals.answer_text_misses("It rises by **$3,450** [1].", ["3,450"], []) == ()
    assert evals.answer_text_misses("It rises by $3,540 [1].", ["3,450"], []) == ("missing 3,450",)
    assert evals.answer_text_misses("The additional insured is Example Property Holdings LLC [1].", [],
                                     ["Example Property Holdings"]) == ("says Example Property Holdings",)
    assert evals.answer_text_misses("Notice within 30 DAYS; limit 1000000 [2].", ["30 days", "$1,000,000"],
                                     ["$21,900"]) == ()
    assert evals.answer_text_misses("The renewal premium is $21,900 [1].", ["3,450"], ["21900"]) == (
        "missing 3,450", "says 21900")


def test_the_answer_text_matches_whole_words_and_numbers_only():
    assert evals.answer_text_misses("The deductible is $15,000 [1].", ["5,000"], []) == ("missing 5,000",)
    assert evals.answer_text_misses("The renewal premium is $121,900 [1].", [], ["21,900"]) == ()
    assert evals.answer_text_misses("The aggregate is $2,000,000 [1].", ["2,000"], []) == ("missing 2,000",)
    assert evals.answer_text_misses("Policy SAMPLE-CGL-2026-0001 adds a Silica-Related Exclusion; notice is 30 days.",
                                     ["silica", "SAMPLE-CGL-2026-0001", "30 days"], []) == ()


def test_the_extra_case_file_loads_as_a_partial_set():
    case_set = evals.load_cases(EXTRA)

    turns = [question for case in case_set.cases for question in case.questions]
    assert (len(case_set.cases), len(turns), sum(len(turn.expected) for turn in turns),
            sum(not turn.expected for turn in turns)) == (9, 10, 16, 2)
    spread = next(case for case in case_set.cases if case.id == "policy-spread-facts")
    assert spread.questions[0].values == ("1,000,000", "40 feet", "30 days")


ONE_CASE = """\
documents:
  quote: sample-renewal-quote.pdf
cases:
- id: renewal-premium
  scope: [quote]
  turns:
  - ask: What premium is the carrier asking for the renewal?
    expect: [quote-premium]
    values: [21900]
"""


def test_a_partial_case_file_skips_the_coverage_checks_and_needs_no_not_asked(tmp_path):
    (tmp_path / "partial.yaml").write_text("partial: one case only\n" + ONE_CASE)
    (tmp_path / "full.yaml").write_text(ONE_CASE)

    [case] = evals.load_cases(tmp_path / "partial.yaml").cases
    with pytest.raises(ValueError) as raised:
        evals.load_cases(tmp_path / "full.yaml")

    assert case.questions[0].values == ("21900",)
    assert "policy-number is neither asked nor listed in not_asked" in str(raised.value)
    assert "no out-of-scope question asks about 'flood'" in str(raised.value)


def test_a_partial_case_file_still_reports_unknown_facts_empty_values_and_values_on_a_not_found_turn(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text("""\
partial: checks only
documents:
  quote: sample-renewal-quote.pdf
cases:
- id: premium
  scope: [quote]
  turns:
  - ask: What premium is the carrier asking for the renewal?
    expect: [quote-premum]
    values: ["21,900", ""]
- id: flood
  scope: [quote]
  turns:
  - ask: Is flood damage covered?
    expect: not_found
    values: [flood]
""")

    with pytest.raises(ValueError) as raised:
        evals.load_cases(path)

    assert str(raised.value) == (f"{path}: premium: unknown fact 'quote-premum'; premium: empty values entry ''; "
                                 "flood: values on a not_found turn")


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
