"""Comparison: grounding, differences, the pipeline and its routes. Fake provider, real Postgres."""

from __future__ import annotations

import pytest

from docvault.domain import Cell, Row


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
