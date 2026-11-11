"""Comparison: grounding, differences, the pipeline and its routes. Fake provider, real Postgres."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import BaseModel

from docvault.ai import AI, ComparisonOwner, FakeChat, FakeEmbedder, ProviderError, RawCompletion, Tokens
from docvault.config import Settings
from docvault.domain import Cell, ComparisonId, ErrorCode, Failure, Purpose, Row, VaultError
from docvault.prompts import Msg, Prompt


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
