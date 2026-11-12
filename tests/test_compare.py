from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from uuid import UUID, uuid4

import httpx2
import pytest

from docvault import compare, documents, insights, prompts
from docvault.ai import ComparisonOwner, FakeChat, ProviderError
from docvault.api import app
from docvault.domain import (
    Cell,
    ComparisonId,
    KeyFact,
    Page,
    PageBasis,
    Purpose,
    Row,
    Usage,
)
from docvault.prompts import CellDraft, CompareDoc, ComparisonDraft, Msg, Prompt, RowDraft
from docvault.ratelimit import RateLimiter

from .conftest import ENDORSEMENT, POLICY, QUOTE, SAMPLES, scalar
from .test_ai import (
    CHAT_COMPLETION,
    GOOD,
    Limit,
    ScriptedChat,
    failure_of,
    gateway,
    openai_chat,
    openai_client,
    reply,
    unavailable,
)
from .test_ai import rows as ledger_rows
from .test_insights import indexed, post, with_chat
from .test_pages import extracted


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
    (("$1,000,000", "€1,000,000"), True),
    (("1,000,000", "€1,000,000", "EUR 1,000,000"), False),
    (("$500 or €450", "USD 500 or 450 EUR"), False),
    (("-$500", "$500"), True),
    (("($500)", "$500"), True),
    (("$500.", "$500"), False),
])
def test_a_row_differs_on_distinct_values_or_an_empty_cell_beside_a_filled_one(values, differs):
    assert row(*values).differs is differs


def test_a_comparison_gets_one_attempt_of_90_seconds_billed_to_the_comparison(db):
    chat, sleeps = ScriptedChat([ProviderError("server_error", retryable=True), reply(GOOD, 10, 0, 3)]), []
    comparison_id = ComparisonId(UUID("00000000-0000-0000-0000-00000000c001"))
    prompt = Prompt(name="compare", version="compare-v1", purpose=Purpose.COMPARE, tier="main",
                    system="Compare the documents.", messages=(Msg("user", "D1, D2"),))

    failure = failure_of(lambda: gateway(db, chat, sleeps).complete(prompt, Limit,
                                                                    owner=ComparisonOwner(comparison_id)))

    assert failure == unavailable("server_error", True)
    assert (chat.timeouts, sleeps) == ([90.0], [])
    assert ledger_rows(db, "purpose, document_id, turn_id, comparison_id, ok, error_code") == [
        ("compare", None, None, comparison_id, False, "server_error")]


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


def test_the_fake_aligns_every_label_the_policy_and_the_renewal_quote_share():
    policy, quote = extracted(POLICY), extracted(QUOTE)
    prompt, _ = prompts.compare([CompareDoc("D1", POLICY, policy.basis, (), policy.pages),
                                 CompareDoc("D2", QUOTE, quote.basis, (), quote.pages)], None, budget_chars=240_000)

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


def test_ground_places_a_cell_by_its_document_label_in_any_case_and_spacing():
    rows, dropped = compare.ground(drafted(("Property Damage Deductible", [
        cell(" d2 ", "$5,000 per occurrence", 2, "$5,000 per occurrence"),
        cell("D2 ", "$2,500 per occurrence", 2, "$2,500 per occurrence")])), [DECLARATIONS, RENEWAL])

    assert (rows, dropped) == ((Row("Property Damage Deductible", (
        None, Cell("$5,000 per occurrence", 2, "$5,000 per occurrence"))),), 1)


def test_ground_reads_rows_until_it_keeps_30_and_counts_dropped_cells_only_in_the_rows_it_read():
    named = cell("D1", "Example Contractors LLC", 1, "Example Contractors LLC")
    off_page = cell("D2", "$5,000 per occurrence", 1, "$5,000 per occurrence")
    draft = drafted(("Empty", [cell("D1"), off_page]), *((f"Row {n}", [named]) for n in range(1, 31)),
                    ("Row 31", [named, off_page]), ("Empty after", [off_page]))

    rows, dropped = compare.ground(draft, [DECLARATIONS, RENEWAL])

    assert ([row.field for row in rows], dropped) == ([f"Row {n}" for n in range(1, 31)], 1)


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
    failure = failure_of(lambda: compare.create(live, [quote, policy], None))

    assert (stored.rows, stored.dropped_cells) == ((Row("Property Damage Deductible", (
        Cell("$2,500 per occurrence", 2, "Property Damage Deductible $2,500 per occurrence"), None)),), 1)
    assert (stored.model, stored.prompt_version, stored.usage) == (
        "gpt-6.1-sol", "compare-v1", Usage(1, 2000, 300, Decimal("0.005100")))
    assert failure == unavailable("server_error", True)
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


def test_a_reply_with_31_rows_is_valid_and_the_comparison_keeps_the_first_30(ctx):
    policy, quote = indexed(ctx, SAMPLES / POLICY), indexed(ctx, SAMPLES / QUOTE)
    cells = [{"document": "D1", "value": "$2,500 per occurrence", "page": 2,
              "quote": "Property Damage Deductible $2,500 per occurrence"},
             {"document": "D2", "value": "$5,000 per occurrence", "page": 2,
              "quote": "Property Damage Deductible $5,000 per occurrence"}]
    sent: list[tuple[str, dict]] = []
    client = openai_client(httpx2.Response(200, json=completion(
        {"rows": [{"field": f"Row {n}", "cells": cells} for n in range(1, 32)]})), sent=sent)

    stored = compare.create(with_chat(ctx, openai_chat(client)), [policy, quote], None)

    schema = sent[0][1]["response_format"]["json_schema"]["schema"]
    assert ([row.field for row in stored.rows], stored.dropped_cells, stored.usage.calls) == (
        [f"Row {n}" for n in range(1, 31)], 0, 1)
    assert "maxItems" not in schema["properties"]["rows"]


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


def test_verified_key_facts_reach_the_prompt_only_for_documents_with_ready_insights(ctx):
    policy, quote = indexed(ctx, SAMPLES / POLICY), indexed(ctx, SAMPLES / QUOTE)
    insights.run_analyze(ctx, policy)
    sent: list[Prompt] = []

    class RecordingChat(FakeChat):
        def complete_json(self, prompt, output, *, timeout_s, on_text=None):
            sent.append(prompt)
            return super().complete_json(prompt, output, timeout_s=timeout_s, on_text=on_text)

    compare.create(with_chat(ctx, RecordingChat()), [policy, quote], None)

    [prompt] = sent
    assert [len(doc.key_facts) for doc in prompt.inputs["documents"]] == [12, 0]
    assert f'<document id="D1" filename="{POLICY}">\n<key_facts>\n- Insurer: Sample Mutual Insurance Company (p. 1)\n' in (
        prompt.messages[0].content)
    assert f'<document id="D2" filename="{QUOTE}">\n<page n="1">' in prompt.messages[0].content


def compared(client, *document_ids: UUID, **body: object):
    return client.post("/comparisons", json={"document_ids": [str(doc_id) for doc_id in document_ids], **body})


def test_a_comparison_needs_two_to_five_distinct_ready_documents_and_a_focus_of_200_characters(client, ctx):
    policy, quote = indexed(ctx, SAMPLES / POLICY), indexed(ctx, SAMPLES / QUOTE)
    queued, unknown = post(client, SAMPLES / ENDORSEMENT), uuid4()

    refused = [compared(client, policy), compared(client, *(uuid4() for _ in range(6))),
               compared(client, policy, policy), compared(client, policy, quote, focus="a" * 201)]
    longest_focus = compared(client, policy, quote, focus="a" * 200)
    not_ready = compared(client, policy, queued)
    missing = compared(client, policy, unknown)

    assert [(response.status_code, response.json()["error"]["code"]) for response in refused] == [
        (422, "invalid_request")] * 4
    assert refused[2].json()["error"]["details"]["errors"] == [
        {"field": "body.document_ids", "message": "Value error, must not repeat a document"}]
    assert longest_focus.status_code == 201
    assert (not_ready.status_code, not_ready.json()) == (409, {"error": {
        "code": "documents_not_ready", "message": "1 of 2 documents are not ready.", "retryable": False,
        "details": {"not_ready": [{"id": str(queued), "filename": ENDORSEMENT, "status": "queued"}]}}})
    assert (missing.status_code, missing.json()) == (404, {"error": {
        "code": "document_not_found", "message": "1 of 2 documents do not exist.", "retryable": False,
        "details": {"missing": [str(unknown)]}}})
    assert scalar(ctx.db, "SELECT count(*) FROM comparisons") == 1


def test_a_broker_compares_the_policy_with_its_renewal_quote_and_reads_it_back(client, ctx):
    policy, quote = indexed(ctx, SAMPLES / POLICY), indexed(ctx, SAMPLES / QUOTE)

    created = compared(client, policy, quote, focus="  ")
    focused = compared(client, policy, quote, focus=" deductible ")
    body = created.json()
    rows = {row["field"]: row for row in body["rows"]}

    assert (created.status_code, focused.status_code) == (201, 201)
    assert rows["Property Damage Deductible"] == {"field": "Property Damage Deductible", "differs": True, "cells": [
        {"document_id": str(policy), "value": "$2,500 per occurrence", "page": 2, "page_label": "p. 2",
         "quote": "Property Damage Deductible $2,500 per occurrence"},
        {"document_id": str(quote), "value": "$5,000 per occurrence", "page": 2, "page_label": "p. 2",
         "quote": "Property Damage Deductible $5,000 per occurrence"}]}
    assert rows["Each Occurrence Limit"]["differs"] is False
    assert {key: body[key] for key in ("focus", "documents", "dropped_cells", "model", "prompt_version")} == {
        "focus": None, "dropped_cells": 0, "model": "fake-chat", "prompt_version": "compare-v1", "documents": [
            {"id": str(policy), "filename": POLICY, "version": 1, "deleted": False, "pages_read": 7, "page_count": 7},
            {"id": str(quote), "filename": QUOTE, "version": 1, "deleted": False, "pages_read": 3, "page_count": 3}]}
    assert (body["usage"]["calls"], body["usage"]["cost_usd"]) == (1, 0.0)
    assert (focused.json()["focus"], [row["field"] for row in focused.json()["rows"]]) == (
        "deductible", ["Property Damage Deductible"])
    with ctx.db.connection() as conn:
        ledger = [tuple(row.values()) for row in conn.execute(
            "SELECT purpose, document_id, turn_id, comparison_id FROM llm_calls WHERE comparison_id IS NOT NULL"
            " ORDER BY id")]
    assert ledger == [("compare", None, None, UUID(body["id"])), ("compare", None, None, UUID(focused.json()["id"]))]
    assert client.get(f"/comparisons/{body['id']}").json() == body

    with ctx.db.connection() as conn:
        conn.execute("UPDATE comparisons SET created_at = now() - interval '1 minute' WHERE id = %s", (body["id"],))
    newest = client.get("/comparisons", params={"limit": 1}).json()
    rest = client.get("/comparisons", params={"limit": 1, "cursor": newest["next_cursor"]}).json()

    assert [item["id"] for item in newest["items"]] == [focused.json()["id"]]
    assert ([item["id"] for item in rest["items"]], rest["next_cursor"]) == ([body["id"]], None)
    unknown = uuid4()
    missing = client.get(f"/comparisons/{unknown}")
    bad_cursor = client.get("/comparisons", params={"cursor": "nope"})
    assert (missing.status_code, missing.json()["error"]) == (404, {
        "code": "comparison_not_found", "message": f"No comparison has the id {unknown}.", "retryable": False,
        "details": {}})
    assert (bad_cursor.status_code, bad_cursor.json()["error"]["code"]) == (400, "invalid_cursor")


def test_a_document_deleted_after_the_comparison_keeps_its_column_and_page_labels(client, ctx):
    policy, quote = indexed(ctx, SAMPLES / POLICY), indexed(ctx, SAMPLES / QUOTE)
    created = compared(client, policy, quote).json()

    assert client.delete(f"/documents/{quote}").status_code == 204
    after = client.get(f"/comparisons/{created['id']}").json()

    assert after["rows"] == created["rows"]
    assert after["documents"] == [
        {"id": str(policy), "filename": POLICY, "version": 1, "deleted": False, "pages_read": 7, "page_count": 7},
        {"id": str(quote), "filename": QUOTE, "version": 1, "deleted": True, "pages_read": 3, "page_count": 3}]
    assert {cell["page_label"] for row in after["rows"] for cell in row["cells"]} == {"p. 1", "p. 2"}


def test_a_comparison_spends_from_the_same_rate_limit_budget_as_an_ask(client, ctx):
    policy, quote = indexed(ctx, SAMPLES / POLICY), indexed(ctx, SAMPLES / QUOTE)
    chat_id = client.post("/chats", json={"document_ids": [str(policy)]}).json()["id"]
    app.state.ctx = dataclasses.replace(ctx, limiter=RateLimiter(), settings=ctx.settings.model_copy(
        update={"questions_per_minute": 1}))

    asked = client.post(f"/chats/{chat_id}/messages", json={"question": "What is the each occurrence limit?"})
    refused = compared(client, policy, quote)

    assert (asked.status_code, refused.status_code, refused.json()["error"]["code"]) == (200, 429, "rate_limited")
    assert int(refused.headers["retry-after"]) > 0
    assert scalar(ctx.db, "SELECT count(*) FROM comparisons") == 0
