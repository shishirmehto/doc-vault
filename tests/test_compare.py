"""Comparison: grounding, differences, the pipeline and its routes. Fake provider, real Postgres."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import BaseModel

from docvault import pages, prompts
from docvault.ai import AI, ComparisonOwner, FakeChat, FakeEmbedder, ProviderError, RawCompletion, Tokens
from docvault.config import Settings
from docvault.domain import Cell, ComparisonId, ErrorCode, Failure, KeyFact, Page, PageBasis, Purpose, Row, VaultError
from docvault.prompts import CompareDoc, ComparisonDraft, Msg, Prompt

from .conftest import POLICY, QUOTE, SAMPLES


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
