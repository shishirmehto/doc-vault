"""Summaries: the options key, the prompt, the fake's options, grounding, the cache and the pipeline. Fake provider,
real Postgres."""

from __future__ import annotations

import dataclasses
from uuid import UUID, uuid4

import httpx2
import pytest

from docvault import documents, pages, prompts, summaries
from docvault.ai import FakeChat
from docvault.api import app
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
from docvault.ratelimit import RateLimiter

from .conftest import ENDORSEMENT, POLICY, SAMPLES, scalar
from .test_ai import openai_chat, openai_client
from .test_compare import completion
from .test_insights import indexed, post, provider_down, with_chat

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


def test_a_focus_the_document_never_mentions_gives_the_leading_passages(ctx):
    policy = indexed(ctx, SAMPLES / POLICY)

    unfocused = summaries.create(ctx, policy, options())
    earthquake = summaries.create(ctx, policy, options(focus="earthquake"))

    assert (earthquake.text, earthquake.sources) == (unfocused.text, unfocused.sources)
    assert earthquake.id != unfocused.id


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


def ledger(ctx, columns: str = "purpose, document_id, turn_id, comparison_id, summary_id, ok") -> list[tuple]:
    with ctx.db.connection() as conn:
        return [tuple(row.values()) for row in conn.execute(
            f"SELECT {columns} FROM llm_calls WHERE purpose = 'summary' ORDER BY id")]


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
    assert [summary_id == stored[0].id for summary_id, in ledger(ctx, "summary_id")] == [True, False]
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


# ------------------------------------------------------------------ over HTTP


def summarise(client, doc_id: UUID, **body: object):
    return client.post(f"/documents/{doc_id}/summaries", json=body)


def test_a_broker_summarises_the_policy_gets_the_stored_summary_back_and_lists_both(client, ctx):
    policy = indexed(ctx, SAMPLES / POLICY)

    created = summarise(client, policy, length="brief", focus="Deductibles")
    again = summarise(client, policy, length="brief", focus="  DEDUCTIBLES ")
    other = summarise(client, policy, length="brief", tone="client")
    body = created.json()

    assert (created.status_code, again.status_code, other.status_code) == (201, 200, 201)
    assert {key: value for key, value in body.items() if key not in ("id", "created_at")} == {
        "document_id": str(policy), "length": "brief", "focus": "deductibles", "tone": "plain",
        "text": f"{OPENING} DEDUCTIBLE Property Damage Deductible $2,500 per occurrence",
        "sources": [{"page": 2, "page_label": "p. 2", "quote": "DEDUCTIBLE"},
                    {"page": 2, "page_label": "p. 2", "quote": "Property Damage Deductible $2,500 per occurrence"}],
        "dropped_sources": 0, "model": "fake-chat", "prompt_version": "summary-v1", "cached": False}
    assert again.json() == {**body, "cached": True}
    assert (other.json()["text"], other.json()["cached"]) == (f"Here is what {POLICY} means for you. {COVER}", False)
    assert ledger(ctx, "summary_id") == [(UUID(body["id"]),), (UUID(other.json()["id"]),)]

    with ctx.db.connection() as conn:   # a minute apart, so the order does not rest on clock resolution
        conn.execute("UPDATE document_summaries SET created_at = now() - interval '1 minute' WHERE id = %s",
                     (body["id"],))
    newest = client.get(f"/documents/{policy}/summaries", params={"limit": 1}).json()
    rest = client.get(f"/documents/{policy}/summaries", params={"limit": 1, "cursor": newest["next_cursor"]}).json()
    bad_cursor = client.get(f"/documents/{policy}/summaries", params={"cursor": "nope"})

    assert newest["items"] == [{**other.json(), "cached": True}]
    assert (rest["items"], rest["next_cursor"]) == ([{**body, "cached": True,
                                                      "created_at": rest["items"][0]["created_at"]}], None)
    assert (bad_cursor.status_code, bad_cursor.json()["error"]["code"]) == (400, "invalid_cursor")


def test_a_summary_needs_valid_options_and_a_known_ready_document(client, ctx):
    policy = indexed(ctx, SAMPLES / POLICY)
    queued, unknown = post(client, SAMPLES / ENDORSEMENT), uuid4()

    refused = [summarise(client, policy, length="short"), summarise(client, policy, tone="casual"),
               summarise(client, policy, focus=f" {'a' * 201} ")]
    longest = summarise(client, policy, focus=f"  {'a' * 200}  ")
    not_ready = summarise(client, queued)
    missing = summarise(client, unknown)
    missing_list = client.get(f"/documents/{unknown}/summaries")
    malformed = [client.post("/documents/not-an-id/summaries", json={}), client.get("/documents/not-an-id/summaries")]
    queued_list = client.get(f"/documents/{queued}/summaries")

    assert [(response.status_code, [error["field"] for error in response.json()["error"]["details"]["errors"]])
            for response in refused] == [(422, ["body.length"]), (422, ["body.tone"]), (422, ["body.focus"])]
    assert (longest.status_code, longest.json()["focus"]) == (201, "a" * 200)
    assert (not_ready.status_code, not_ready.json()) == (409, {"error": {
        "code": "documents_not_ready", "message": "1 of 1 documents are not ready.", "retryable": False,
        "details": {"not_ready": [{"id": str(queued), "filename": ENDORSEMENT, "status": "queued"}]}}})
    assert (missing.status_code, missing.json()) == (404, {"error": {
        "code": "document_not_found", "message": "1 of 1 documents do not exist.", "retryable": False,
        "details": {"missing": [str(unknown)]}}})
    assert (missing_list.status_code, missing_list.json()) == (404, {"error": {
        "code": "document_not_found", "message": f"No document has the id {unknown}.", "retryable": False,
        "details": {}}})
    assert [(response.status_code, response.json()["error"]["code"]) for response in malformed] == [
        (422, "invalid_request")] * 2
    assert (queued_list.status_code, queued_list.json()) == (200, {"items": [], "next_cursor": None})
    assert scalar(ctx.db, "SELECT count(*) FROM document_summaries") == 1


def test_a_summary_whose_sources_are_not_on_their_pages_is_kept_without_them(client, ctx):
    policy = indexed(ctx, SAMPLES / POLICY)

    class OffPageChat(FakeChat):
        handlers = {"summary": lambda prompt: SummaryDraft(summary="The policy  insures a roofer.", sources=[
            SummarySourceDraft(page=1, quote="Property Damage Deductible $2,500 per occurrence"),
            SummarySourceDraft(page=9, quote="Insurer: Sample Mutual Insurance Company"),
            SummarySourceDraft(page=2, quote="Flood sublimit $50,000")])}

    app.state.ctx = with_chat(ctx, OffPageChat())
    created = summarise(client, policy)

    assert (created.status_code, created.json()["text"], created.json()["sources"],
            created.json()["dropped_sources"]) == (201, "The policy insures a roofer.", [], 3)


def test_a_summary_spends_from_the_same_rate_limit_budget_as_an_ask(client, ctx):
    policy = indexed(ctx, SAMPLES / POLICY)
    chat_id = client.post("/chats", json={"document_ids": [str(policy)]}).json()["id"]
    app.state.ctx = dataclasses.replace(ctx, limiter=RateLimiter(), settings=ctx.settings.model_copy(
        update={"questions_per_minute": 1}))

    asked = client.post(f"/chats/{chat_id}/messages", json={"question": "What is the each occurrence limit?"})
    refused = summarise(client, policy)

    assert (asked.status_code, refused.status_code, refused.json()["error"]["code"]) == (200, 429, "rate_limited")
    assert scalar(ctx.db, "SELECT count(*) FROM document_summaries") == 0


def test_the_openai_path_grounds_each_source_and_a_failed_call_gets_no_retry_and_no_repair(client, ctx):
    policy = indexed(ctx, SAMPLES / POLICY)
    sent: list[tuple[str, dict]] = []
    timeouts: list[float] = []
    openai = openai_client(
        httpx2.Response(200, json=completion({
            "summary": "The policy has a $2,500 property damage deductible.",
            "sources": [{"page": 2, "quote": "Property Damage Deductible $2,500 per occurrence"},
                        {"page": 1, "quote": "Each Occurrence Limit $1,000,000"}]})),
        httpx2.Response(500, json={"error": {"message": "Internal error", "type": "server_error", "code": None}}),
        httpx2.Response(200, json=completion({"summary": "No sources key."})),
        sent=sent, timeouts=timeouts)
    app.state.ctx = with_chat(ctx, openai_chat(openai))

    created = summarise(client, policy)
    down = summarise(client, policy, length="brief")
    invalid = summarise(client, policy, length="detailed")

    assert (created.status_code, created.json()["sources"], created.json()["dropped_sources"],
            created.json()["model"]) == (201, [{"page": 2, "page_label": "p. 2", "quote": (
                "Property Damage Deductible $2,500 per occurrence")}], 1, "gpt-6.1-sol")
    assert (down.status_code, down.headers["retry-after"], down.json()["error"]) == (503, "5", {
        "code": "ai_unavailable", "message": "The AI provider could not complete the request (server_error).",
        "retryable": True, "details": {}})
    assert (invalid.status_code, invalid.json()["error"]) == (502, {
        "code": "ai_bad_output", "message": "The model returned invalid output.", "retryable": True, "details": {}})
    path, request = sent[0]
    response_format = request["response_format"]
    assert (len(sent), timeouts, path, request["model"], response_format["type"],
            response_format["json_schema"]["name"], response_format["json_schema"]["strict"]) == (
        3, [90.0] * 3, "/v1/chat/completions", "gpt-6.1-sol", "json_schema", "SummaryDraft", True)
    assert ledger(ctx, "ok, error_code") == [(True, None), (False, "server_error"), (True, None)]
    assert scalar(ctx.db, "SELECT count(*) FROM document_summaries") == 1
