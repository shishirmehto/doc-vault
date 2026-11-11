"""Summaries: the options key, the prompt, the fake's options, grounding, the cache and the pipeline. Fake provider,
real Postgres."""

from __future__ import annotations

import pytest

from docvault import documents, pages, prompts, summaries
from docvault.ai import FakeChat
from docvault.domain import (
    ErrorCode,
    Failure,
    Page,
    PageBasis,
    Summary,
    SummaryLength,
    SummaryOptions,
    SummarySource,
    SummaryTone,
    VaultError,
)
from docvault.prompts import Prompt, SummaryDraft, SummarySourceDraft

from .conftest import POLICY, SAMPLES, scalar
from .test_insights import indexed, provider_down, with_chat

BRIEF, STANDARD = SummaryLength.BRIEF, SummaryLength.STANDARD
PLAIN, FORMAL, CLIENT = SummaryTone.PLAIN, SummaryTone.FORMAL, SummaryTone.CLIENT


def options(length: SummaryLength = STANDARD, focus: str | None = None, tone: SummaryTone = PLAIN) -> SummaryOptions:
    return SummaryOptions.of(length, focus, tone)


@pytest.mark.parametrize(("focus", "key"), [
    (" Deductibles  ", "deductibles"), ("deductibles", "deductibles"), ("DEDUCTIBLES", "deductibles"),
    ("Flood \n  sublimit", "flood sublimit"), ("  ", None), (None, None)])
def test_the_focus_is_trimmed_lowercased_and_single_spaced_and_a_blank_one_is_none(focus, key):
    assert SummaryOptions.of(BRIEF, focus, CLIENT) == SummaryOptions(BRIEF, key, CLIENT)


# ------------------------------------------------------------------ the prompt


def sample_pages(name: str) -> tuple[PageBasis, tuple[Page, ...]]:
    doc = pages.extract(pages.sniff_kind(SAMPLES / name), SAMPLES / name, max_pages=100)
    return doc.basis, doc.pages


def test_the_prompt_is_the_insights_pages_then_the_word_target_the_readers_interest_and_the_tone():
    basis, policy = sample_pages(POLICY)
    insights_prompt, _ = prompts.insights(POLICY, basis, policy, budget_chars=2500)

    prompt = prompts.summary(POLICY, basis, policy, options(BRIEF, " Flood  Sublimit", CLIENT), budget_chars=2500)
    lengths = [prompts.summary(POLICY, basis, policy, options(length), budget_chars=2500).messages[0].content
               for length in SummaryLength]

    assert insights_prompt.messages[0].content.endswith("The text above stops at p. 2 of 7.")
    assert prompt.messages[0].content == insights_prompt.messages[0].content + (
        "\n\nLength: about 60 words.\nReader's interest: flood sublimit\nTone: for the client. Write for the insured "
        "client rather than the broker: explain insurance terms in everyday words, avoid broker jargon and "
        'abbreviations, and say "you" and "your" for the insured.')
    assert [content.split("\n\n")[-1].split("\n")[:2] for content in lengths] == [
        ["Length: about 60 words.", "Reader's interest: none given. Cover what a broker checks first."],
        ["Length: about 150 words.", "Reader's interest: none given. Cover what a broker checks first."],
        ["Length: about 300 words.", "Reader's interest: none given. Cover what a broker checks first."]]
    assert (prompt.inputs["pages"], prompt.inputs["options"]) == (policy[:2], SummaryOptions(BRIEF, "flood sublimit",
                                                                                              CLIENT))
    assert (prompt.name, prompt.version, prompt.purpose, prompt.tier) == ("summary", "summary-v1", "summary", "main")


# ------------------------------------------------------------------ the fake, through the pipeline

OPENING = f"{POLICY} in brief."
COVER = "SAMPLE - fictitious document. Not a real insurance contract."


def test_each_length_gives_the_fake_more_passages(ctx):
    policy = indexed(ctx, SAMPLES / POLICY)

    brief, standard, detailed = (summaries.create(ctx, policy, options(length)) for length in SummaryLength)

    assert brief.text == f"{OPENING} {COVER}"
    assert standard.text == f"{OPENING} {COVER} COMMERCIAL GENERAL LIABILITY POLICY DECLARATIONS"
    assert detailed.text == (
        f"{OPENING} {COVER} COMMERCIAL GENERAL LIABILITY POLICY DECLARATIONS Insurer: Sample Mutual Insurance Company "
        "Policy Number: SAMPLE-CGL-2026-0001 Named Insured: Example Contractors LLC Mailing Address: 100 Example "
        "Street, Sampletown, ST 00000")
    assert [len(summary.sources) for summary in (brief, standard, detailed)] == [2, 4, 6]
    assert detailed.sources[2:4] == (SummarySource(1, "COMMERCIAL GENERAL LIABILITY POLICY"),
                                     SummarySource(1, "DECLARATIONS"))
    assert {summary.dropped_sources for summary in (brief, standard, detailed)} == {0}


def test_a_focus_puts_the_passages_it_names_first(ctx):
    policy = indexed(ctx, SAMPLES / POLICY)

    unfocused = summaries.create(ctx, policy, options())
    focused = summaries.create(ctx, policy, options(focus=" Deductibles "))

    assert focused.options.focus == "deductibles"
    assert focused.text.startswith(f"{OPENING} DEDUCTIBLE Property Damage Deductible $2,500 per occurrence The ")
    assert focused.sources[:2] == (SummarySource(2, "DEDUCTIBLE"),
                                   SummarySource(2, "Property Damage Deductible $2,500 per occurrence"))
    assert focused.text != unfocused.text


def test_each_tone_gives_the_fake_its_own_opening_sentence_and_the_same_passages(ctx):
    policy = indexed(ctx, SAMPLES / POLICY)

    texts = [summaries.create(ctx, policy, options(BRIEF, tone=tone)).text for tone in (PLAIN, FORMAL, CLIENT)]

    assert texts == [f"{OPENING} {COVER}", f"This note summarises {POLICY}. {COVER}",
                     f"Here is what {POLICY} means for you. {COVER}"]


# ------------------------------------------------------------------ grounding

DECLARATIONS = (Page(1, "Named Insured: Example Contractors LLC"),
                Page(2, "Property Damage Deductible\n$2,500 per occurrence"))


def drafted(*sources: tuple[int, str]) -> SummaryDraft:
    return SummaryDraft(summary="A summary.", sources=[SummarySourceDraft(page=page, quote=quote)
                                                       for page, quote in sources])


def test_ground_keeps_a_quote_found_on_its_page_as_the_source_span_and_counts_the_rest():
    sources, dropped = summaries.ground(drafted(
        (2, "property damage deductible $2,500  per occurrence"),
        (1, "Property Damage Deductible $2,500 per occurrence"),
        (1, "Flood sublimit $50,000"),
        (2, "PROPERTY DAMAGE DEDUCTIBLE $2,500 per occurrence"),
        (1, "Example Contractors LLC")), DECLARATIONS)

    assert (sources, dropped) == ((SummarySource(2, "Property Damage Deductible $2,500 per occurrence"),
                                   SummarySource(1, "Example Contractors LLC")), 2)


def test_ground_reads_sources_until_it_keeps_six():
    words = ["Named", "Insured", "Example", "Contractors", "LLC", "Named Insured", "Insured: Example"]

    sources, dropped = summaries.ground(drafted(
        (1, words[0]), (2, "Named Insured"), *((1, word) for word in words[1:]), (2, "LLC")), DECLARATIONS)

    assert (sources, dropped) == (tuple(SummarySource(1, word) for word in words[:6]), 1)


# ------------------------------------------------------------------ the cache and the ledger


def ledger(ctx) -> list[tuple]:
    with ctx.db.connection() as conn:
        return [tuple(row.values()) for row in conn.execute(
            "SELECT purpose, document_id, turn_id, comparison_id, summary_id, ok FROM llm_calls"
            " WHERE purpose = 'summary' ORDER BY id")]


def test_the_same_options_are_served_from_the_store_without_a_model_call(ctx):
    policy = indexed(ctx, SAMPLES / POLICY)

    first = summaries.create(ctx, policy, options(focus="Deductibles"))
    again = summaries.create(ctx, policy, options(focus=" deductibles "))
    other = summaries.create(ctx, policy, options(BRIEF, "deductibles"))

    assert (first.cached, again.cached, other.cached) == (False, True, False)
    assert (again.id, again.text, again.sources, again.created_at) == (
        first.id, first.text, first.sources, first.created_at)
    assert other.id != first.id
    assert ledger(ctx) == [("summary", None, None, None, first.id, True), ("summary", None, None, None, other.id, True)]
    assert scalar(ctx.db, "SELECT count(*) FROM document_summaries") == 2


def test_a_summary_stored_by_an_identical_request_during_the_call_is_returned_uncached(ctx):
    policy = indexed(ctx, SAMPLES / POLICY)
    stored: list[Summary] = []

    def summarise_first(prompt: Prompt) -> SummaryDraft:
        stored.append(summaries.create(ctx, policy, options()))
        return FakeChat.handlers["summary"](prompt)

    class RacingChat(FakeChat):
        handlers = {**FakeChat.handlers, "summary": summarise_first}

    raced = summaries.create(with_chat(ctx, RacingChat()), policy, options())

    assert (raced.id, raced.text, raced.cached) == (stored[0].id, stored[0].text, False)
    assert [row[4] == stored[0].id for row in ledger(ctx)] == [True, False]
    assert scalar(ctx.db, "SELECT count(*) FROM document_summaries") == 1


def test_a_provider_failure_stores_nothing_and_its_call_stays_billed(ctx):
    policy = indexed(ctx, SAMPLES / POLICY)

    with pytest.raises(VaultError) as raised:
        summaries.create(with_chat(ctx, provider_down("server_error", retryable=True, prompt="summary")), policy,
                         options())

    assert raised.value.failure == Failure(
        ErrorCode.AI_UNAVAILABLE, "The AI provider could not complete the request (server_error).", True)
    assert [(purpose, ok) for purpose, *_, ok in ledger(ctx)] == [("summary", False)]
    assert scalar(ctx.db, "SELECT count(*) FROM document_summaries") == 0


# ------------------------------------------------------------------ deletes


def test_deleting_the_document_deletes_its_summaries(ctx):
    policy = indexed(ctx, SAMPLES / POLICY)
    summaries.create(ctx, policy, options())

    documents.delete(ctx, policy)

    assert scalar(ctx.db, "SELECT count(*) FROM document_summaries") == 0


def test_a_document_deleted_during_the_model_call_is_not_found_and_nothing_is_stored(ctx):
    policy = indexed(ctx, SAMPLES / POLICY)

    def delete_then_summarise(prompt: Prompt) -> SummaryDraft:
        documents.delete(ctx, policy)
        return FakeChat.handlers["summary"](prompt)

    class DeletingChat(FakeChat):
        handlers = {**FakeChat.handlers, "summary": delete_then_summarise}

    with pytest.raises(VaultError) as raised:
        summaries.create(with_chat(ctx, DeletingChat()), policy, options())

    assert (raised.value.failure, raised.value.details) == (
        Failure(ErrorCode.DOCUMENT_NOT_FOUND, "1 of 1 documents do not exist.", False), {"missing": [str(policy)]})
    assert [(purpose, ok) for purpose, *_, ok in ledger(ctx)] == [("summary", True)]
    assert scalar(ctx.db, "SELECT count(*) FROM document_summaries") == 0
