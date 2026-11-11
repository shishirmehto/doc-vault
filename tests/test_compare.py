"""Comparison: grounding, differences, the pipeline and its routes. Fake provider, real Postgres."""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import httpx2
import pytest
from pydantic import BaseModel

from docvault import compare, documents, pages, prompts
from docvault.ai import AI, ComparisonOwner, FakeChat, FakeEmbedder, ProviderError, RawCompletion, Tokens
from docvault.config import Settings
from docvault.domain import (
    Cell,
    ComparisonId,
    ErrorCode,
    Failure,
    KeyFact,
    Page,
    PageBasis,
    Purpose,
    Row,
    Usage,
    VaultError,
)
from docvault.prompts import CellDraft, CompareDoc, ComparisonDraft, Msg, Prompt, RowDraft

from .conftest import POLICY, QUOTE, SAMPLES, scalar
from .test_ai import CHAT_COMPLETION, openai_chat, openai_client
from .test_insights import indexed, with_chat


def row(*values: str | None) -> Row:
    return Row("Each Occurrence Limit", tuple(None if value is None else Cell(value, 2, value) for value in values))


@pytest.mark.parametrize(("values", "differs"), [
    (("$1,000,000", "1,000,000", "USD 1,000,000.00", " $1,000,000 "), False),
    (("$2,500 per occurrence", "$5,000 per occurrence"), True),
    (("$2,500", None), True),
    ((None, None), False),
    (("Example Contractors LLC", "example  contractors llc"), False),
    (("EUR 500", "€500.00", "500 eur"), False),
    (("Europe", "ope"), True),
    (("$5.005", "$5"), True),
])
def test_a_row_differs_on_distinct_values_or_an_empty_cell_beside_a_filled_one(values, differs):
    assert row(*values).differs is differs


class Rows(BaseModel):
    rows: list[str]


class FailsOnceChat(FakeChat):
    """A provider that fails its first call with a retryable error and would answer the second."""

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float] = []

    def complete_json(self, prompt, output, *, timeout_s, on_text=None):
        self.timeouts.append(timeout_s)
        if len(self.timeouts) == 1:
            raise ProviderError("server_error", retryable=True)
        return RawCompletion('{"rows": []}'), Tokens(10, 0, 3)


def test_a_comparison_gets_one_attempt_of_90_seconds_billed_to_the_comparison(db):
    chat, sleeps = FailsOnceChat(), []
    gateway = AI(chat, FakeEmbedder(), db, Settings(_env_file=None, ai_max_attempts=3), sleep=sleeps.append)
    comparison_id = ComparisonId(UUID("00000000-0000-0000-0000-00000000c001"))
    prompt = Prompt(name="compare", version="compare-v1", purpose=Purpose.COMPARE, tier="main",
                    system="Compare the documents.", messages=(Msg("user", "D1, D2"),))

    with pytest.raises(VaultError) as raised:
        gateway.complete(prompt, Rows, owner=ComparisonOwner(comparison_id))

    assert raised.value.failure == Failure(
        ErrorCode.AI_UNAVAILABLE, "The AI provider could not complete the request (server_error).", True)
    assert (chat.timeouts, sleeps) == ([90.0], [])
    with db.connection() as conn:
        ledger = [tuple(row.values()) for row in conn.execute(
            "SELECT purpose, document_id, turn_id, comparison_id, ok, error_code FROM llm_calls")]
    assert ledger == [("compare", None, None, comparison_id, False, "server_error")]


POLICY_PAGES = (Page(1, "a" * 30), Page(2, ""), Page(3, "b" * 30), Page(4, "c" * 30))
EACH_OCCURRENCE = KeyFact("Each Occurrence Limit", "$1,000,000", 3, "Each Occurrence Limit $1,000,000")


def test_the_prompt_gives_each_document_an_even_share_of_the_budget_and_its_verified_key_facts():
    policy = CompareDoc("D1", "policy.pdf", PageBasis.PHYSICAL, (EACH_OCCURRENCE,), POLICY_PAGES)
    quote = CompareDoc("D2", "quote.docx", PageBasis.SYNTHETIC,
                       (KeyFact("Deductible", "$5,000", 1, "Deductible $5,000"),), (Page(1, "Deductible $5,000"),))
    notes = CompareDoc("D3", "notes.txt", PageBasis.SYNTHETIC, (), (Page(1, "x" * 50),))

    prompt, pages_read = prompts.compare([policy, quote, notes], None, budget_chars=195)
    focused, _ = prompts.compare([policy, quote], "deductible", budget_chars=195)

    assert prompt.messages == (Msg("user", f"""\
Focus: none. Compare the fields a broker compares.

<document id="D1" filename="policy.pdf">
<key_facts>
- Each Occurrence Limit: $1,000,000 (p. 3)
</key_facts>
<page n="1">{"a" * 30}</page>
<page n="3">{"b" * 30}</page>
The text above stops at p. 3 of 4.
</document>

<document id="D2" filename="quote.docx">
<key_facts>
- Deductible: $5,000 (section 1)
</key_facts>
<page n="1">Deductible $5,000</page>
</document>

<document id="D3" filename="notes.txt">
<page n="1">{"x" * 50}</page>
</document>"""),)
    assert pages_read == (3, 1, 1)
    assert [doc.pages for doc in prompt.inputs["documents"]] == [(POLICY_PAGES[0], POLICY_PAGES[2]), quote.pages, notes.pages]
    assert focused.messages[0].content.startswith("Focus: deductible\n\n<document id=\"D1\"")
    assert (prompt.name, prompt.version, prompt.purpose, prompt.tier) == ("compare", "compare-v1", "compare", "main")


def sample(label: str, name: str) -> CompareDoc:
    doc = pages.extract(pages.sniff_kind(SAMPLES / name), SAMPLES / name, max_pages=100)
    return CompareDoc(label, name, doc.basis, (), doc.pages)


def test_the_fake_aligns_every_label_the_policy_and_the_renewal_quote_share():
    prompt, _ = prompts.compare([sample("D1", POLICY), sample("D2", QUOTE)], None, budget_chars=240_000)

    raw, _ = FakeChat().complete_json(prompt, ComparisonDraft, timeout_s=90.0)

    assert [(row.field, *((cell.document, cell.value, cell.page) for cell in row.cells))
            for row in ComparisonDraft.model_validate_json(raw.text).rows] == [
        ("Insurer", ("D1", "Sample Mutual Insurance Company", 1), ("D2", "Sample Mutual Insurance Company", 1)),
        ("Named Insured", ("D1", "Example Contractors LLC", 1), ("D2", "Example Contractors LLC", 1)),
        ("Producer", ("D1", "Example Brokerage Services", 1), ("D2", "Example Brokerage Services", 1)),
        ("Each Occurrence Limit", ("D1", "$1,000,000", 2), ("D2", "$1,000,000", 2)),
        ("Damage to Premises Rented to You Limit", ("D1", "$100,000 any one premises", 2),
         ("D2", "$100,000 any one premises", 2)),
        ("Medical Expense Limit", ("D1", "$5,000 any one person", 2), ("D2", "$10,000 any one person", 2)),
        ("Personal and Advertising Injury Limit", ("D1", "$1,000,000", 2), ("D2", "$1,000,000", 2)),
        ("General Aggregate Limit", ("D1", "$2,000,000", 2), ("D2", "$3,000,000", 2)),
        ("Products-Completed Operations Aggregate Limit", ("D1", "$2,000,000", 2), ("D2", "$2,000,000", 2)),
        ("Property Damage Deductible", ("D1", "$2,500 per occurrence", 2), ("D2", "$5,000 per occurrence", 2)),
        ("Estimated Annual Payroll", ("D1", "$640,000", 2), ("D2", "$710,000", 2)),
        ("Rate per $1,000 of Payroll", ("D1", "$28.83", 2), ("D2", "$30.85", 2)),
    ]


# ------------------------------------------------------------------ grounding

DECLARATIONS = CompareDoc("D1", POLICY, PageBasis.PHYSICAL, (), (
    Page(1, "Named Insured: Example Contractors LLC"), Page(2, "Property Damage Deductible\n$2,500 per occurrence")))
RENEWAL = CompareDoc("D2", QUOTE, PageBasis.PHYSICAL, (), (
    Page(1, "Named Insured: Example Contractors LLC"), Page(2, "Property Damage Deductible\n$5,000 per occurrence")))


def cell(document: str, value: str | None = None, page: int | None = None, quote: str | None = None) -> CellDraft:
    return CellDraft(document=document, value=value, page=page, quote=quote)


def drafted(*rows: tuple[str, list[CellDraft]]) -> ComparisonDraft:
    return ComparisonDraft(rows=[RowDraft(field=field, cells=cells) for field, cells in rows])


def test_ground_keeps_a_cell_only_when_its_quote_is_on_the_page_it_cites_and_quotes_the_source():
    rows, dropped = compare.ground(drafted(("Property Damage  Deductible", [
        cell("D1", "$2,500  per occurrence", 2, "property damage deductible $2,500 per occurrence"),
        cell("D2", "$5,000 per occurrence", 1, "Property Damage Deductible $5,000 per occurrence")])),
        [DECLARATIONS, RENEWAL])

    assert (rows, dropped) == ((Row("Property Damage Deductible", (
        Cell("$2,500 per occurrence", 2, "Property Damage Deductible $2,500 per occurrence"), None)),), 1)


def test_ground_counts_only_values_it_could_not_keep_and_removes_rows_left_empty():
    rows, dropped = compare.ground(drafted(
        ("Flood Sublimit", [cell("D1"), cell("D2", "  ", 1, "Named Insured")]),
        ("Named Insured", [cell("D1", "Example Contractors LLC", 1, "Named Insured: Example Contractors LLC"),
                           cell("D2"), cell("D9", "Example Contractors LLC", 1, "Named Insured")]),
        ("Property Damage Deductible", [cell("D1", "$2,500 per occurrence", 2, None),
                                        cell("D2", "$5,000 per occurrence", None, "$5,000 per occurrence")])),
        [DECLARATIONS, RENEWAL])

    assert rows == (Row("Named Insured", (
        Cell("Example Contractors LLC", 1, "Named Insured: Example Contractors LLC"), None)),)
    assert dropped == 3


def test_ground_gives_a_column_to_the_first_cell_that_names_its_document():
    rows, dropped = compare.ground(drafted(("Property Damage Deductible", [
        cell("D2"), cell("D2", "$5,000 per occurrence", 2, "$5,000 per occurrence"),
        cell("D1", "$2,500 per occurrence", 2, "$2,500 per occurrence")])), [DECLARATIONS, RENEWAL])

    assert (rows, dropped) == ((Row("Property Damage Deductible", (
        Cell("$2,500 per occurrence", 2, "$2,500 per occurrence"), None)),), 1)


def test_ground_removes_empty_rows_before_it_keeps_the_first_30():
    named = cell("D1", "Example Contractors LLC", 1, "Example Contractors LLC")
    draft = drafted(("Empty", [cell("D1")]), *((f"Row {n}", [named]) for n in range(1, 32)))

    rows, dropped = compare.ground(draft, [DECLARATIONS, RENEWAL])

    assert ([row.field for row in rows], dropped) == ([f"Row {n}" for n in range(1, 31)], 0)


# ------------------------------------------------------------------ the pipeline


def completion(draft: dict) -> dict:
    return {**CHAT_COMPLETION, "choices": [{"index": 0, "finish_reason": "stop", "logprobs": None, "message": {
        "role": "assistant", "content": json.dumps(draft), "refusal": None}}]}


def test_the_openai_path_stores_grounded_cells_and_tries_a_failing_call_once(ctx):
    policy, quote = indexed(ctx, SAMPLES / POLICY), indexed(ctx, SAMPLES / QUOTE)
    draft = completion({"rows": [{"field": "Property Damage Deductible", "cells": [
        {"document": "D1", "value": "$2,500 per occurrence", "page": 2,
         "quote": "Property Damage Deductible $2,500 per occurrence"},
        {"document": "D2", "value": "$5,000 per occurrence", "page": 1,
         "quote": "Property Damage Deductible $5,000 per occurrence"}]}]})
    sent: list[tuple[str, dict]] = []
    client = openai_client(httpx2.Response(200, json=draft), httpx2.Response(500, json={"error": {
        "message": "Internal error", "type": "server_error", "code": None}}), httpx2.Response(200, json=draft),
        sent=sent)
    live = with_chat(ctx, openai_chat(client))

    stored = compare.create(live, [policy, quote], None)
    with pytest.raises(VaultError) as raised:
        compare.create(live, [quote, policy], None)

    assert (stored.rows, stored.dropped_cells) == ((Row("Property Damage Deductible", (
        Cell("$2,500 per occurrence", 2, "Property Damage Deductible $2,500 per occurrence"), None)),), 1)
    assert (stored.model, stored.prompt_version, stored.usage) == (
        "gpt-6.1-sol", "compare-v1", Usage(1, 2000, 300, Decimal("0.005100")))
    assert raised.value.failure == Failure(
        ErrorCode.AI_UNAVAILABLE, "The AI provider could not complete the request (server_error).", True)
    assert len(sent) == 2
    path, request = sent[0]
    response_format = request["response_format"]
    assert (path, request["model"], response_format["type"], response_format["json_schema"]["name"],
            response_format["json_schema"]["strict"]) == (
        "/v1/chat/completions", "gpt-6.1-sol", "json_schema", "ComparisonDraft", True)
    assert response_format["json_schema"]["schema"]["$defs"]["CellDraft"]["required"] == [
        "document", "value", "page", "quote"]
    with ctx.db.connection() as conn:
        ledger = [tuple(row.values()) for row in conn.execute(
            "SELECT comparison_id, ok, error_code FROM llm_calls WHERE purpose = 'compare' ORDER BY id")]
    assert [(ok, error) for _, ok, error in ledger] == [(True, None), (False, "server_error")]
    assert ledger[0][0] == stored.id and ledger[1][0] not in (None, stored.id)
    assert scalar(ctx.db, "SELECT count(*) FROM comparisons") == 1


def test_a_document_deleted_during_the_model_call_reads_back_as_deleted(ctx):
    policy, quote = indexed(ctx, SAMPLES / POLICY), indexed(ctx, SAMPLES / QUOTE)

    def delete_the_quote_then_compare(prompt: Prompt) -> ComparisonDraft:
        documents.delete(ctx, quote)
        return FakeChat.handlers["compare"](prompt)

    class DeletingChat(FakeChat):
        handlers = {**FakeChat.handlers, "compare": delete_the_quote_then_compare}

    stored = compare.create(with_chat(ctx, DeletingChat()), [policy, quote], None)

    assert [(doc.filename, doc.deleted, doc.pages_read, doc.page_count) for doc in stored.documents] == [
        (POLICY, False, 7, 7), (QUOTE, True, 3, 3)]
    assert len(stored.rows) == 12
