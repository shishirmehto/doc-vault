"""Chat: start, ask, follow up, refuse, compare, history. Fake provider, real Postgres."""

from __future__ import annotations

import dataclasses
import re
from uuid import UUID, uuid4

import httpx2
import pytest

from docvault import chat, pages, prompts
from docvault.api import app
from docvault.chat import Candidate, PageRequest
from docvault.domain import (
    Answered,
    ChunkId,
    Citation,
    DocRef,
    DocumentId,
    NotFound,
    PageBasis,
    PageMap,
    Source,
    TurnOutcome,
)
from docvault.fake_ai import FakeChat
from docvault.prompts import AnswerDraft, EvidenceDraft

from .conftest import FACTS, POLICY, QUOTE, SAMPLES, TRUTH, scalar
from .test_ai import openai_chat, openai_client
from .test_compare import completion
from .test_insights import indexed, post, provider_down, with_chat


def normalised(text: str) -> str:
    return " ".join(text.split())


def ready(ctx, name: str) -> DocumentId:
    return indexed(ctx, SAMPLES / name)


def ask(client, chat_id: str, question: str) -> dict:
    response = client.post(f"/chats/{chat_id}/messages", json={"question": question})
    assert response.status_code == 200, response.json()
    return response.json()


def cited(turn: dict) -> list[tuple]:
    return [(c["ref"], c["filename"], c["page"], c["page_label"], c["quote"], c["page_url"]) for c in turn["citations"]]


def ledger(ctx) -> dict[str, list[str]]:
    with ctx.db.connection() as conn:
        rows = conn.execute("SELECT turn_id, purpose FROM llm_calls WHERE turn_id IS NOT NULL ORDER BY id").fetchall()
    purposes: dict[str, list[str]] = {}
    for row in rows:
        purposes.setdefault(str(row["turn_id"]), []).append(row["purpose"])
    return purposes


def test_a_broker_asks_follows_up_is_refused_and_compares_two_documents(client, ctx):
    policy, quote = ready(ctx, POLICY), ready(ctx, QUOTE)

    started = client.post("/chats", json={"document_ids": [str(policy), str(policy)], "title": "CGL renewal"})
    assert started.status_code == 201
    chat_id = started.json()["id"]
    assert {k: started.json()[k] for k in ("title", "documents")} == {
        "title": "CGL renewal", "documents": [{"id": str(policy), "filename": POLICY, "deleted": False}]}

    limit = FACTS["policy-each-occurrence-limit"]
    first = ask(client, chat_id, "What is the each occurrence limit?")
    assert {k: first[k] for k in ("question", "interpreted_as", "status", "reason", "answer")} == {
        "question": "What is the each occurrence limit?", "interpreted_as": "What is the each occurrence limit?",
        "status": "answered", "reason": None, "answer": f"{normalised(limit['text'])} [1]"}
    assert cited(first) == [(1, POLICY, limit["page"], "p. 2", normalised(limit["text"]),
                             f"/documents/{policy}/pages/2")]
    assert scalar(ctx.db, "SELECT document_id FROM chunks WHERE id = %s", (first["citations"][0]["chunk_id"],)) == policy

    second = ask(client, chat_id, "Do defence costs reduce it?")
    defence = "They do not reduce the Each Occurrence Limit or either aggregate limit."
    assert {k: second[k] for k in ("interpreted_as", "status", "answer")} == {
        "interpreted_as": "What is the each occurrence limit? Do defence costs reduce it?",
        "status": "answered", "answer": f"{defence} [1]"}
    assert cited(second) == [(1, POLICY, 5, "p. 5", defence, f"/documents/{policy}/pages/5")]

    assert "flood" in TRUTH["absent_topics"]
    third = ask(client, chat_id, "Is flood damage covered?")
    assert {k: third[k] for k in ("interpreted_as", "status", "reason", "answer", "citations")} == {
        "interpreted_as": "Is flood damage covered?", "status": "not_found", "reason": "no_evidence",
        "answer": f"I could not find anything about this in {POLICY}, so I can't answer it from the selected documents.",
        "citations": []}

    assert ledger(ctx) == {first["id"]: ["embed_query", "answer"],
                           second["id"]: ["condense", "embed_query", "answer"],
                           third["id"]: ["condense", "embed_query"]}
    with ctx.db.connection() as conn:
        spent = {str(row["turn_id"]): (row["calls"], row["input_tokens"], row["output_tokens"]) for row in conn.execute(
            "SELECT turn_id, count(*) AS calls, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens"
            " FROM llm_calls WHERE turn_id IS NOT NULL GROUP BY turn_id")}
    for turn in (first, second, third):
        calls, input_tokens, output_tokens = spent[turn["id"]]
        assert turn["usage"] == {"calls": calls, "input_tokens": input_tokens, "output_tokens": output_tokens,
                                 "cost_usd": 0.0}
    assert [turn["usage"]["calls"] for turn in (first, second, third)] == [2, 3, 2]

    assert client.get(f"/chats/{chat_id}/messages").json() == {"chat_id": chat_id, "turns": [first, second, third]}
    assert client.get(f"/chats/{chat_id}/messages", params={"limit": 2}).json()["turns"] == [second, third]

    both = client.post("/chats", json={"document_ids": [str(quote), str(policy)]}).json()
    assert both["documents"] == [{"id": str(policy), "filename": POLICY, "deleted": False},
                                 {"id": str(quote), "filename": QUOTE, "deleted": False}]
    change = next(row for row in TRUTH["changes"] if row["field"] == "general aggregate limit")
    compared = ask(client, both["id"], "Compare the general aggregate limit in the two documents.")
    assert (compared["status"], compared["answer"]) == (
        "answered", f"{normalised(change['quote_text'])} [1] {normalised(change['policy_text'])} [2]")
    assert cited(compared) == [
        (1, QUOTE, FACTS[change["quote"]]["page"], "p. 2", normalised(change["quote_text"]), f"/documents/{quote}/pages/2"),
        (2, POLICY, FACTS[change["policy"]]["page"], "p. 2", normalised(change["policy_text"]), f"/documents/{policy}/pages/2")]
    assert ledger(ctx)[compared["id"]] == ["embed_query", "answer"]


def test_starting_a_chat_needs_existing_ready_documents(client, ctx):
    policy = ready(ctx, POLICY)
    queued = post(client, SAMPLES / QUOTE)
    unknown = uuid4()

    empty = client.post("/chats", json={"document_ids": []})
    missing = client.post("/chats", json={"document_ids": [str(policy), str(unknown)]})
    not_ready = client.post("/chats", json={"document_ids": [str(policy), str(queued)]})

    assert empty.status_code == 422
    assert (missing.status_code, missing.json()) == (404, {"error": {
        "code": "document_not_found", "message": "1 of 2 documents do not exist.", "retryable": False,
        "details": {"missing": [str(unknown)]}}})
    assert (not_ready.status_code, not_ready.json()) == (409, {"error": {
        "code": "documents_not_ready", "message": "1 of 2 documents are not ready.", "retryable": False,
        "details": {"not_ready": [{"id": str(queued), "filename": QUOTE, "status": "queued"}]}}})
    assert scalar(ctx.db, "SELECT count(*) FROM chats") == 0
    assert client.post(f"/chats/{unknown}/messages", json={"question": "Hello?"}).json()["error"]["code"] == (
        "chat_not_found")
    assert client.get(f"/chats/{unknown}/messages").status_code == 404


def test_a_chat_that_lost_a_document_keeps_its_history_and_takes_no_questions(client, ctx):
    policy, quote = ready(ctx, POLICY), ready(ctx, QUOTE)
    chat_id = client.post("/chats", json={"document_ids": [str(policy), str(quote)]}).json()["id"]
    answered = ask(client, chat_id, "What is the each occurrence limit?")
    calls = scalar(ctx.db, "SELECT count(*) FROM llm_calls")

    assert client.delete(f"/documents/{quote}").status_code == 204
    refused = client.post(f"/chats/{chat_id}/messages", json={"question": "What is the general aggregate limit?"})

    assert (refused.status_code, refused.json()) == (409, {"error": {
        "code": "documents_deleted", "retryable": False,
        "message": "1 of 2 documents in this chat were deleted. Start a new chat with the documents you want to ask about.",
        "details": {"deleted": [{"id": str(quote), "filename": QUOTE, "status": "deleted"}]}}})
    assert scalar(ctx.db, "SELECT count(*) FROM llm_calls") == calls
    assert client.get(f"/chats/{chat_id}/messages").json()["turns"] == [answered]
    assert client.get(f"/chats/{chat_id}").json()["documents"] == [
        {"id": str(policy), "filename": POLICY, "deleted": False}, {"id": str(quote), "filename": QUOTE, "deleted": True}]


def test_chats_read_back_one_at_a_time_and_newest_first_a_page_at_a_time(client, ctx):
    policy, quote = ready(ctx, POLICY), ready(ctx, QUOTE)
    first, second, third = (client.post("/chats", json=body).json() for body in (
        {"document_ids": [str(policy)], "title": "Policy"}, {"document_ids": [str(quote)]},
        {"document_ids": [str(quote), str(policy)], "title": "Renewal"}))
    with ctx.db.connection() as conn:   # one chat a minute, so the order does not rest on clock resolution
        for minutes, chat in ((3, first), (2, second), (1, third)):
            conn.execute("UPDATE chats SET created_at = now() - %s * interval '1 minute' WHERE id = %s",
                         (minutes, chat["id"]))

    one = client.get(f"/chats/{third['id']}").json()
    newest = client.get("/chats", params={"limit": 2}).json()
    rest = client.get("/chats", params={"limit": 2, "cursor": newest["next_cursor"]}).json()

    assert {k: one[k] for k in ("id", "title", "documents")} == {"id": third["id"], "title": "Renewal", "documents": [
        {"id": str(policy), "filename": POLICY, "deleted": False}, {"id": str(quote), "filename": QUOTE, "deleted": False}]}
    assert [(c["id"], c["title"]) for c in newest["items"]] == [(third["id"], "Renewal"), (second["id"], None)]
    assert ([(c["id"], c["title"]) for c in rest["items"]], rest["next_cursor"]) == ([(first["id"], "Policy")], None)
    missing = client.get(f"/chats/{uuid4()}")
    bad_cursor = client.get("/chats", params={"cursor": "nope"})
    assert [(r.status_code, r.json()["error"]["code"]) for r in (missing, bad_cursor)] == [
        (404, "chat_not_found"), (400, "invalid_cursor")]


def test_a_question_over_2000_characters_is_refused_and_the_limit_is_in_the_schema(client, ctx):
    chat_id = client.post("/chats", json={"document_ids": [str(ready(ctx, POLICY))]}).json()["id"]
    too_long = client.post(f"/chats/{chat_id}/messages", json={"question": "a" * 2001})
    schema = client.get("/openapi.json").json()["components"]["schemas"]["AskIn"]["properties"]["question"]

    assert (too_long.status_code, too_long.json()["error"]["details"]["errors"]) == (422, [
        {"field": "body.question", "message": "String should have at most 2000 characters"}])
    assert (schema["minLength"], schema["maxLength"]) == (1, 2000)
    assert scalar(ctx.db, "SELECT count(*) FROM chat_turns") == 0


def test_a_question_the_model_declines_is_kept_as_not_found_with_its_reason(client, ctx):
    policy = ready(ctx, POLICY)
    chat_id = client.post("/chats", json={"document_ids": [str(policy)]}).json()["id"]

    declined = ask(client, chat_id, "Roofing?")

    assert {k: declined[k] for k in ("status", "reason", "answer", "citations")} == {
        "status": "not_found", "reason": "model_declined", "answer": "The selected documents do not say.",
        "citations": []}
    assert ledger(ctx) == {declined["id"]: ["embed_query", "answer"]}
    assert client.get(f"/chats/{chat_id}/messages").json()["turns"] == [declined]


def test_a_question_about_the_whole_document_is_answered_from_its_opening_passage(client, ctx):
    def says_what_it_is(prompt):
        opening = " ".join(prompt.inputs["sources"][0].text.split()[:6])
        return draft(f"{opening} [E1]", ("E1", "S1", opening))

    class SaysWhatItIs(FakeChat):
        declines_unanswerable = True
        handlers = {**FakeChat.handlers, "answer": says_what_it_is}

    policy = ready(ctx, POLICY)
    chat_id = client.post("/chats", json={"document_ids": [str(policy)]}).json()["id"]
    app.state.ctx = with_chat(ctx, SaysWhatItIs())

    about = ask(client, chat_id, "What is this about?")

    first = scalar(ctx.db, "SELECT id FROM chunks WHERE document_id = %s ORDER BY ord LIMIT 1", (policy,))
    assert (about["status"], [(c["page"], c["chunk_id"]) for c in about["citations"]]) == ("answered", [(1, str(first))])
    assert ledger(ctx) == {about["id"]: ["embed_query", "answer"]}


def test_a_turn_with_an_unpriced_call_reports_no_cost(client, ctx):
    class UnpricedChat(FakeChat):
        models = {"main": "gpt-7-preview", "small": "gpt-7-preview"}

    policy = ready(ctx, POLICY)
    chat_id = client.post("/chats", json={"document_ids": [str(policy)]}).json()["id"]
    app.state.ctx = with_chat(ctx, UnpricedChat())

    usage = ask(client, chat_id, "What is the each occurrence limit?")["usage"]

    assert (usage["calls"], usage["cost_usd"]) == (2, None)


def test_a_provider_failure_on_the_answer_writes_no_turn_and_keeps_the_failed_calls(client, ctx):
    policy = ready(ctx, POLICY)
    chat_id = client.post("/chats", json={"document_ids": [str(policy)]}).json()["id"]
    app.state.ctx = with_chat(ctx, provider_down("server_error", retryable=True, prompt="answer"))

    response = client.post(f"/chats/{chat_id}/messages", json={"question": "What is the each occurrence limit?"})

    assert (response.status_code, response.headers["retry-after"], response.json()["error"]) == (503, "5", {
        "code": "ai_unavailable", "message": "The AI provider could not complete the request (server_error).",
        "retryable": True, "details": {}})
    assert scalar(ctx.db, "SELECT count(*) FROM chat_turns") == 0
    with ctx.db.connection() as conn:
        rows = conn.execute("SELECT turn_id, purpose, ok, error_code FROM llm_calls WHERE turn_id IS NOT NULL"
                            " ORDER BY id").fetchall()
    assert [(row["purpose"], row["ok"], row["error_code"]) for row in rows] == [
        ("embed_query", True, None), ("answer", False, "server_error"), ("answer", False, "server_error"),
        ("answer", False, "server_error")]
    assert len({row["turn_id"] for row in rows}) == 1


# ------------------------------------------------------------------ pure decisions

POLICY_DOC = DocRef(DocumentId(UUID(int=1)), POLICY, PageBasis.PHYSICAL, 7)
QUOTE_DOC = DocRef(DocumentId(UUID(int=2)), QUOTE, PageBasis.PHYSICAL, 3)


def source(label: str, doc: DocRef, text: str, page_map: PageMap, chunk: int) -> Source:
    return Source(label, ChunkId(UUID(int=chunk)), doc, text, page_map, similarity=0.5, rrf=0.03)


def draft(answer: str, *evidence: tuple[str, str, str], supported: bool = True) -> AnswerDraft:
    return AnswerDraft(supported=supported, answer=answer, follow_ups=[], evidence=[
        EvidenceDraft(id=evidence_id, source=label, quote=quote) for evidence_id, label, quote in evidence])


def limits() -> list[Source]:
    policy, quote = FACTS["policy-each-occurrence-limit"], FACTS["quote-each-occurrence-limit"]
    return [source("S1", POLICY_DOC, f"Limits of Insurance\n{policy['text']}", PageMap(((0, 2),)), chunk=11),
            source("S2", QUOTE_DOC, f"Proposed Limits\n{quote['text']}", PageMap(((0, 2),)), chunk=21)]


def test_ground_cites_the_page_each_quote_sits_on_inside_a_page_spanning_chunk():
    clause = FACTS["policy-defence-clause"]
    text = f"{clause['text']}\n\n{clause['continues']['text']}"
    spanning = source("S1", POLICY_DOC, text, PageMap(((0, 4), (len(clause["text"]) + 2, 5))), chunk=7)

    answer = chat.ground(draft("There is no duty to defend uncovered suits [E1].",
                               ("E1", "S1", clause["continues"]["text"].upper()), ("E1", "S1", clause["text"])),
                         [spanning])

    assert answer == Answered("There is no duty to defend uncovered suits [1][2].", (
        Citation(POLICY_DOC.id, POLICY, 5, PageBasis.PHYSICAL, clause["continues"]["text"], ChunkId(UUID(int=7))),
        Citation(POLICY_DOC.id, POLICY, 4, PageBasis.PHYSICAL, clause["text"], ChunkId(UUID(int=7)))))


def test_ground_drops_unknown_labels_and_paraphrases_and_removes_their_markers():
    limit = FACTS["policy-each-occurrence-limit"]["text"]

    answer = chat.ground(draft("The limit is $1,000,000 [E1]. Defence costs reduce it [E2]. The quote keeps it [E3].",
                               ("E1", "S1", limit), ("E2", "S9", limit),
                               ("E3", "S2", "the quote keeps the limit at one million")),
                         limits())

    assert answer == Answered("The limit is $1,000,000 [1]. Defence costs reduce it. The quote keeps it.", (
        Citation(POLICY_DOC.id, POLICY, 2, PageBasis.PHYSICAL, limit, ChunkId(UUID(int=11))),))


def test_ground_rewrites_a_marker_naming_two_items_and_gives_a_repeated_quote_its_first_number():
    limit = FACTS["quote-each-occurrence-limit"]["text"]

    answer = chat.ground(draft("Both documents set it at $1,000,000 [E1, E2]. The policy states it once [E3].",
                               ("E1", "S1", limit), ("E2", "S2", limit), ("E3", "S1", limit)), limits())

    assert answer.text == "Both documents set it at $1,000,000 [1][2]. The policy states it once [1]."
    assert [(c.filename, c.quote) for c in answer.citations] == [(POLICY, limit), (QUOTE, limit)]


def test_ground_rewrites_the_markers_ending_markdown_list_items_and_keeps_the_list():
    limit = FACTS["policy-each-occurrence-limit"]["text"]

    answer = chat.ground(draft("The each occurrence limit is the same in both:\n\n"
                               "- The policy: **$1,000,000** [E1]\n"
                               "- The quote: **$1,000,000** [E2]\n"
                               "- Defence costs reduce it [E3]",
                               ("E1", "S1", limit), ("E2", "S2", limit)), limits())

    assert answer.text == ("The each occurrence limit is the same in both:\n\n"
                           "- The policy: **$1,000,000** [1]\n"
                           "- The quote: **$1,000,000** [2]\n"
                           "- Defence costs reduce it")
    assert [(c.filename, c.quote) for c in answer.citations] == [(POLICY, limit), (QUOTE, limit)]


AGGREGATE = "Item 1. General Aggregate Limit $2,000,000"
DEDUCTIBLE = "Item 2. Deductible $5,000 per claim"


def aggregate_and_deductible() -> list[Source]:
    return [source("S1", POLICY_DOC, f"{AGGREGATE}\n{DEDUCTIBLE}", PageMap(((0, 2),)), chunk=33)]


def test_ground_cites_each_sentence_its_own_quote_whatever_order_the_evidence_comes_in():
    answer = chat.ground(draft("The aggregate is $2,000,000 [E1]. The deductible is $5,000 per claim [E2].",
                               ("E2", "S1", DEDUCTIBLE), ("E1", "S1", AGGREGATE)), aggregate_and_deductible())

    assert answer.text == "The aggregate is $2,000,000 [2]. The deductible is $5,000 per claim [1]."
    assert [citation.quote for citation in answer.citations] == [DEDUCTIBLE, AGGREGATE]



def test_ground_reads_an_evidence_id_written_with_brackets_or_in_lower_case():
    answer = chat.ground(draft("The aggregate is $2,000,000 [E1]. The deductible is $5,000 per claim [E2].",
                               ("[E1]", "S1", AGGREGATE), (" e2", "S1", DEDUCTIBLE)), aggregate_and_deductible())

    assert answer.text == "The aggregate is $2,000,000 [1]. The deductible is $5,000 per claim [2]."

def test_ground_leaves_a_sentence_whose_quote_is_not_found_without_a_number_and_numbers_the_others():
    answer = chat.ground(draft("The aggregate is $2,000,000 [E1]. The deductible is $25,000 [E2]. "
                               "It applies per claim [E3].",
                               ("E1", "S1", AGGREGATE), ("E2", "S1", "Deductible $25,000"), ("E3", "S1", DEDUCTIBLE)),
                         aggregate_and_deductible())

    assert answer.text == "The aggregate is $2,000,000 [1]. The deductible is $25,000. It applies per claim [2]."
    assert [citation.quote for citation in answer.citations] == [AGGREGATE, DEDUCTIBLE]


def test_ground_removes_a_marker_with_no_evidence_item():
    answer = chat.ground(draft("The aggregate is $2,000,000 [E1]. The deductible is $5,000 per claim [E2].",
                               ("E1", "S1", AGGREGATE)), aggregate_and_deductible())

    assert answer.text == "The aggregate is $2,000,000 [1]. The deductible is $5,000 per claim."


def test_ground_gives_two_sentences_that_share_a_marker_the_same_number():
    answer = chat.ground(draft("The deductible is $5,000 [E1]. It applies per claim [E1]. "
                               "The aggregate is $2,000,000 [E2].",
                               ("E1", "S1", DEDUCTIBLE), ("E2", "S1", AGGREGATE)), aggregate_and_deductible())

    assert answer.text == "The deductible is $5,000 [1]. It applies per claim [1]. The aggregate is $2,000,000 [2]."


def test_ground_removes_a_stray_source_marker_without_a_number():
    answer = chat.ground(draft("The aggregate is $2,000,000 [E1]. The deductible is $5,000 per claim [S1].",
                               ("E1", "S1", AGGREGATE), ("S1", "S1", DEDUCTIBLE)), aggregate_and_deductible())

    assert answer.text == "The aggregate is $2,000,000 [1]. The deductible is $5,000 per claim."


def test_ground_numbers_the_markers_of_a_markdown_list_and_keeps_its_syntax():
    answer = chat.ground(draft("- Aggregate **$2,000,000** [E1]\n- Deductible $5,000 [E2]",
                               ("E1", "S1", AGGREGATE), ("E2", "S1", DEDUCTIBLE)), aggregate_and_deductible())

    assert answer.text == "- Aggregate **$2,000,000** [1]\n- Deductible $5,000 [2]"


def test_ground_refuses_a_declined_draft_and_a_draft_with_nothing_verified():
    declined = chat.ground(draft("The documents do not mention flood cover [E1].", supported=False), limits())
    unverified = chat.ground(draft("The limit is one million [E1].", ("E1", "S1", "a limit of one million dollars")),
                             limits())

    assert declined == NotFound(TurnOutcome.MODEL_DECLINED, "The documents do not mention flood cover.")
    assert unverified == NotFound(TurnOutcome.UNVERIFIED, "I found passages that look related, but I could not "
                                                          "verify an answer against their text, so I am not giving one.")


def candidate(doc: DocRef, name: str, rrf: float) -> Candidate:
    return Candidate(ChunkId(uuid4()), doc, name, PageMap(((0, 1),)), similarity=0.3, rrf=rrf, lexical_hit=False)


def test_each_document_gets_its_quota_of_sources_even_when_another_dominates():
    dominant = [candidate(POLICY_DOC, f"policy {n}", 0.03 - n / 1000) for n in range(1, 11)]
    trailing = [candidate(QUOTE_DOC, f"quote {n}", 0.015 - n / 1000) for n in range(1, 4)]

    both = chat.select_sources([*trailing, *dominant], k=8, per_doc_min=2)
    alone = chat.select_sources(dominant, k=8, per_doc_min=2)

    assert [(s.label, s.text) for s in both] == [
        ("S1", "policy 1"), ("S2", "policy 2"), ("S3", "policy 3"), ("S4", "policy 4"), ("S5", "policy 5"),
        ("S6", "policy 6"), ("S7", "quote 1"), ("S8", "quote 2")]
    assert [s.text for s in alone] == [f"policy {n}" for n in range(1, 9)]


def documents(count: int) -> list[DocRef]:
    return [DocRef(DocumentId(UUID(int=100 + n)), f"doc-{n}.pdf", PageBasis.PHYSICAL, 10) for n in range(count)]


def test_a_document_keeps_its_strong_passages_when_the_chat_holds_more_documents_than_slots():
    lead, *rest = documents(10)
    best = [candidate(lead, f"lead {n}", 0.05 - n / 1000) for n in range(1, 4)]
    weak = [candidate(doc, f"other {n}", 0.03 - n / 1000) for n, doc in enumerate(rest, start=1)]

    sources = chat.select_sources([*weak, *best], k=8, per_doc_min=2)

    assert [s.text for s in sources] == ["lead 1", "lead 2", "lead 3", *(f"other {n}" for n in range(1, 6))]


def test_a_document_keeps_its_strong_passages_when_the_chat_holds_as_many_documents_as_slots():
    lead, *rest = documents(8)
    best = [candidate(lead, f"lead {n}", 0.05 - n / 1000) for n in range(1, 4)]
    weak = [candidate(doc, f"other {n}", 0.03 - n / 1000) for n, doc in enumerate(rest, start=1)]

    sources = chat.select_sources([*weak, *best], k=8, per_doc_min=2)

    assert [s.text for s in sources] == ["lead 1", "lead 2", "lead 3", *(f"other {n}" for n in range(1, 6))]


def test_the_per_document_guarantee_takes_at_most_half_the_slots():
    first, second, third = documents(3)
    dominant = [candidate(first, f"first {n}", 0.05 - n / 1000) for n in range(1, 11)]
    trailing = [candidate(second, f"second {n}", 0.02 - n / 1000) for n in range(1, 4)]
    trailing += [candidate(third, f"third {n}", 0.0195 - n / 1000) for n in range(1, 4)]

    sources = chat.select_sources([*trailing, *dominant], k=8, per_doc_min=2)

    assert [s.text for s in sources] == [*(f"first {n}" for n in range(1, 7)), "second 1", "third 1"]


def test_each_opening_passage_follows_the_k_sources_once_in_a_chat_of_up_to_three_documents():
    first, second, third, fourth = documents(4)
    ranked = [candidate(first, f"first {n}", 0.05 - n / 1000) for n in range(1, 4)]
    openings = [ranked[0], candidate(second, "second opening", 0.0), candidate(third, "third opening", 0.0)]

    three = chat.select_sources(ranked, k=2, per_doc_min=2, ends=openings)
    four = chat.select_sources(ranked, k=2, per_doc_min=2, ends=[*openings, candidate(fourth, "fourth opening", 0.0)])

    assert [(s.label, s.text) for s in three] == [
        ("S1", "first 1"), ("S2", "first 2"), ("S3", "second opening"), ("S4", "third opening")]
    assert [s.text for s in four] == ["first 1", "first 2"]


def test_page_passages_lead_and_only_a_chunk_they_hold_whole_is_not_sent_again():
    first, second = documents(2)
    ranked = [candidate(first, f"first {n}", 0.05 - n / 1000) for n in range(1, 4)]
    cut = dataclasses.replace(ranked[0], text="first 1, cut to the page asked about")
    ends = [candidate(second, "second opening", 0.0), candidate(second, "second closing", 0.0)]

    sources = chat.select_sources(ranked, k=2, per_doc_min=2, on_pages=[cut, ranked[1]], ends=[ranked[1], *ends])

    assert [(s.label, s.text) for s in sources] == [
        ("S1", "first 1, cut to the page asked about"), ("S2", "first 2"), ("S3", "first 1"), ("S4", "first 3"),
        ("S5", "second opening"), ("S6", "second closing")]


@pytest.mark.parametrize(("question", "expected"), [
    ("What does page 45 say?", (PageRequest(45, 45),)),
    ("Summarise p. 45 for me", (PageRequest(45, 45),)),
    ("What is on pg 45?", (PageRequest(45, 45),)),
    ("Compare PAGE 3 with Page 3", (PageRequest(3, 3),)),
    ("What do pages 10-12 cover?", (PageRequest(10, 12),)),
    ("Read pages 10 to 12", (PageRequest(10, 12),)),
    ("Read pages 10 through 12", (PageRequest(10, 12),)),
    ("Summarise pp. 10-12", (PageRequest(10, 12),)),
    ("Read pages 10\u201312", (PageRequest(10, 12),)),
    ("Summarise pages 12-10", (PageRequest(10, 12),)),
    ("Read pages 1-500", (PageRequest(1, 10),)),
    ("Compare pages 10 and 11", (PageRequest(10, 10), PageRequest(11, 11))),
    ("What is on the first page?", (PageRequest(1, 1),)),
    ("Who signs the cover page?", (PageRequest(1, 1),)),
    ("What does the opening page say?", (PageRequest(1, 1),)),
    ("What is on the last page?", (PageRequest(-1, -1),)),
    ("What does the final page list?", (PageRequest(-1, -1),)),
    ("Is the closing page signed?", (PageRequest(-1, -1),)),
    ("Does the last page repeat page 2?", (PageRequest(-1, -1), PageRequest(2, 2))),
    ("Is the notice period 30 days?", ()),
    ("Is Form CG 21 47 attached?", ()),
    ("How does this handbook end?", ()),
    ("Which pages list exclusions?", ()),
    ("What does page 0 say?", ()),
])
def test_a_question_names_pages_by_number_by_range_or_by_end_and_nothing_else_names_one(question, expected):
    assert chat.page_requests(question) == expected


def test_a_page_request_resolves_against_the_page_count_and_a_page_past_the_end_to_nothing():
    requests = [PageRequest(-1, -1), PageRequest(1, 1), PageRequest(3, 3), PageRequest(5, 14), PageRequest(12, 12)]

    assert [request.resolve(7) for request in requests] == [(7, 7), (1, 1), (3, 3), (5, 7), None]


def test_each_source_tag_names_its_pages_and_its_documents_page_count():
    handbook = DocRef(DocumentId(UUID(int=3)), "handbook.pdf", PageBasis.PHYSICAL, 922)
    notes = DocRef(DocumentId(UUID(int=4)), "notes.docx", PageBasis.SYNTHETIC, 3)

    prompt = prompts.answer("What does page 45 say?", "What does page 45 say?", [], [
        source("S1", handbook, "Page forty-five.", PageMap(((0, 45),)), chunk=1),
        source("S2", handbook, "Forty-five. Forty-six.", PageMap(((0, 45), (12, 46))), chunk=2),
        source("S3", notes, "Section two.", PageMap(((0, 2),)), chunk=3)])

    assert re.findall(r"<source [^>]*>", prompt.messages[0].content) == [
        '<source id="S1" document="handbook.pdf" pages="p. 45 of 922">',
        '<source id="S2" document="handbook.pdf" pages="p. 45-46 of 922">',
        '<source id="S3" document="notes.docx" pages="section 2 of 3">']


def test_ground_cites_a_quote_in_a_page_passage_on_its_page_and_in_its_chunk():
    text = "End of five.\n\nAll of six.\n\nThis insurance does not apply to roofing."
    part, page_map = pages.part_on_pages(text, PageMap(((0, 5), (14, 6), (27, 7))), 6, 7)

    answer = chat.ground(draft("It excludes roofing [E1].", ("E1", "S1", "This insurance does not apply to roofing")),
                         [source("S1", POLICY_DOC, part, page_map, chunk=9)])

    assert answer == Answered("It excludes roofing [1].", (Citation(
        POLICY_DOC.id, POLICY, 7, PageBasis.PHYSICAL, "This insurance does not apply to roofing", ChunkId(UUID(int=9))),))


def test_the_gate_passes_on_similarity_or_an_all_terms_match_and_refuses_nothing_found():
    near = dataclasses.replace(candidate(POLICY_DOC, "near", 0.03), similarity=0.2)
    far = dataclasses.replace(candidate(POLICY_DOC, "far", 0.03), similarity=0.19)

    assert [chat.gate(found, 0.2) for found in ([near], [far], [dataclasses.replace(far, lexical_hit=True)], [])] == [
        True, False, True, False]


def test_a_nul_in_a_question_or_a_chat_title_is_an_invalid_request(client, ctx):
    policy = ready(ctx, POLICY)
    titled = client.post("/chats", json={"document_ids": [str(policy)], "title": "CGL\x00 policy"})
    chat_id = client.post("/chats", json={"document_ids": [str(policy)]}).json()["id"]
    asked = client.post(f"/chats/{chat_id}/messages", json={"question": "What is the\x00 limit?"})

    assert [(r.status_code, r.json()["error"]["code"], r.json()["error"]["details"]["errors"][0]["field"])
            for r in (titled, asked)] == [(422, "invalid_request", "body.title"), (422, "invalid_request", "body.question")]
    assert scalar(ctx.db, "SELECT count(*) FROM chats") == 1


def test_a_chat_takes_at_most_50_documents_and_a_200_character_title(client, ctx):
    policy = str(ready(ctx, POLICY))
    too_many = client.post("/chats", json={"document_ids": [str(uuid4()) for _ in range(51)]})
    too_long = client.post("/chats", json={"document_ids": [policy], "title": "t" * 201})
    longest = client.post("/chats", json={"document_ids": [policy], "title": "t" * 200})

    assert [(r.status_code, r.json().get("error", {}).get("code")) for r in (too_many, too_long)] == [
        (422, "invalid_request"), (422, "invalid_request")]
    assert (longest.status_code, len(longest.json()["title"])) == (201, 200)
    schema = client.get("/openapi.json").json()["components"]["schemas"]["StartChatIn"]["properties"]
    assert (schema["document_ids"]["maxItems"], schema["title"]["anyOf"][0]["maxLength"]) == (50, 200)
    assert "At most 50." in schema["document_ids"]["description"]


def test_a_blank_question_is_an_invalid_request_and_calls_no_model(client, ctx):
    chat_id = client.post("/chats", json={"document_ids": [str(ready(ctx, POLICY))]}).json()["id"]
    calls = scalar(ctx.db, "SELECT count(*) FROM llm_calls")

    response = client.post(f"/chats/{chat_id}/messages", json={"question": " \n\t "})

    assert (response.status_code, response.json().get("error", {}).get("code")) == (422, "invalid_request")
    assert scalar(ctx.db, "SELECT count(*) FROM llm_calls") == calls


def test_a_provider_failure_that_retrying_cannot_fix_sends_no_retry_after(client, ctx):
    chat_id = client.post("/chats", json={"document_ids": [str(ready(ctx, POLICY))]}).json()["id"]
    app.state.ctx = with_chat(ctx, provider_down("insufficient_quota", retryable=False, prompt="answer"))

    response = client.post(f"/chats/{chat_id}/messages", json={"question": "What is the each occurrence limit?"})

    assert (response.status_code, response.json()["error"]["retryable"], response.headers.get("retry-after")) == (
        503, False, None)


def test_the_model_writes_the_answer_before_its_follow_ups_and_four_of_them_are_valid_but_the_turn_offers_three(
        client, ctx):
    chat_id = client.post("/chats", json={"document_ids": [str(ready(ctx, POLICY))]}).json()["id"]
    sent: list[tuple[str, dict]] = []
    provider = openai_client(httpx2.Response(200, json=completion({
        "supported": False, "answer": "The selected documents do not say.", "evidence": [], "follow_ups": [
            "Who is the insurer?", "Is there a deductible?", "What is the premium?", "What is the policy period?"]})),
        sent=sent)
    app.state.ctx = with_chat(ctx, openai_chat(provider))

    turn = ask(client, chat_id, "What is the each occurrence limit?")

    schema = sent[0][1]["response_format"]["json_schema"]["schema"]
    assert (turn["reason"], turn["suggestions"]) == (
        "model_declined", ["Who is the insurer?", "Is there a deductible?", "What is the premium?"])
    assert ledger(ctx) == {turn["id"]: ["embed_query", "answer"]}
    assert list(schema["properties"]) == ["supported", "answer", "evidence", "follow_ups"]
    assert "maxItems" not in schema["properties"]["follow_ups"]


# ------------------------------------------------------------------ questions that name a place


class Recording(FakeChat):
    """Keeps every prompt, and declines what its sources do not answer as the OpenAI backend does."""

    declines_unanswerable = True

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[prompts.Prompt] = []

    def complete_json(self, prompt, output, **kwargs):
        self.prompts.append(prompt)
        return super().complete_json(prompt, output, **kwargs)


def asked(ctx, document_ids: list[DocumentId], question: str, *, k: int = 16) -> tuple[TurnOutcome, list[tuple], list[Source]]:
    """Ask in a new chat. Returns the outcome, each source tag the answer prompt rendered as (id, document, pages),
    and the sources behind them."""
    recording = Recording()
    settings = ctx.settings.model_copy(update={"retrieval_k": k})
    turn = chat.ask(with_chat(dataclasses.replace(ctx, settings=settings), recording),
                    chat.start(ctx.db, document_ids).id, question)
    answers = [prompt for prompt in recording.prompts if prompt.name == "answer"]
    tags = re.findall(r'<source id="(S\d+)" document="([^"]+)" pages="([^"]+)">', answers[0].messages[0].content)
    return turn.answer.outcome, tags, list(answers[0].inputs["sources"])


def searched(ctx, document_ids: list[DocumentId], question: str, *, k: int = 16) -> chat.Found:
    chat_id = chat.start(ctx.db, document_ids).id
    [q_vec], _ = ctx.ai.embedder.embed([question], timeout_s=5)
    with ctx.db.connection() as conn:
        return chat.search(conn, chat.scope(conn, chat_id), q_vec, question, ctx.ai.embed_model, per_doc=2, k=k)


def test_a_question_about_the_last_page_or_a_page_number_sends_that_page_first_and_once(ctx):
    policy = ready(ctx, POLICY)

    _, last_tags, last = asked(ctx, [policy], "What is on the last page?")
    _, third_tags, third = asked(ctx, [policy], "What does page 3 list?")

    assert [tag for tag in last_tags if tag[2] == "p. 7 of 7"] == [("S1", POLICY, "p. 7 of 7")]
    assert [tag for tag in third_tags if tag[2] == "p. 3 of 7"] == [("S1", POLICY, "p. 3 of 7")]
    with ctx.db.connection() as conn:
        ords = {row["id"]: row["ord"] for row in conn.execute("SELECT id, ord FROM chunks WHERE document_id = %s",
                                                              (policy,))}
    assert [ords[last[0].chunk_id], ords[third[0].chunk_id]] == [5, 1]
    employment, form = FACTS["policy-employment-exclusion"]["text"], FACTS["policy-form-cg-21-47"]["text"]
    cited = [chat.ground(draft("It says this [E1].", ("E1", "S1", quote)), sent[:1]).citations
             for quote, sent in ((employment, last), (form, third))]
    assert [[(c.page, c.chunk_id) for c in citations] for citations in cited] == [
        [(7, last[0].chunk_id)], [(3, third[0].chunk_id)]]


def test_each_page_a_question_names_gets_its_own_share_of_passages(ctx):
    policy = ready(ctx, POLICY)

    _, page_and_last, _ = asked(ctx, [policy], "What do page 2 and the last page say?")
    _, range_and_last, _ = asked(ctx, [policy], "What do pages 1 to 6 and the last page say?")

    assert page_and_last[:3] == [("S1", POLICY, "p. 2 of 7"), ("S2", POLICY, "p. 2 of 7"), ("S3", POLICY, "p. 7 of 7")]
    assert range_and_last[:3] == [
        ("S1", POLICY, "p. 1-2 of 7"), ("S2", POLICY, "p. 2-4 of 7"), ("S3", POLICY, "p. 7 of 7")]


def test_page_passages_across_a_chat_stop_at_k_in_scope_order(ctx):
    policy, quote = ready(ctx, POLICY), ready(ctx, QUOTE)

    found = {k: searched(ctx, [policy, quote], "What is on the last page?", k=k).on_pages for k in (16, 1)}

    assert {k: [(p.doc.filename, p.page_map.first_page, p.page_map.last_page) for p in passages]
            for k, passages in found.items()} == {16: [(POLICY, 7, 7), (QUOTE, 3, 3)], 1: [(POLICY, 7, 7)]}


def test_a_question_about_a_page_is_answered_from_that_page_when_the_gate_finds_nothing(ctx):
    policy = ready(ctx, POLICY)
    question = "Summarise page 6."

    found = searched(ctx, [policy], question)
    outcome, tags, _ = asked(ctx, [policy], question)

    assert (chat.gate(found.retrieved, ctx.ai.min_similarity), found.matched(ctx.ai.min_similarity)) == (False, True)
    assert (outcome, tags[0]) == (TurnOutcome.ANSWERED, ("S1", POLICY, "p. 6 of 7"))


def test_a_page_the_document_does_not_have_sends_only_its_opening_and_closing_passages(ctx):
    policy = ready(ctx, POLICY)

    outcome, tags, _ = asked(ctx, [policy], "What does page 12 say?")

    assert (outcome, tags) == (TurnOutcome.NO_EVIDENCE, [("S1", POLICY, "p. 1-2 of 7"), ("S2", POLICY, "p. 6-7 of 7")])


def test_a_two_document_chat_sends_each_documents_opening_then_closing_passage_after_the_k(ctx):
    policy, quote = ready(ctx, POLICY), ready(ctx, QUOTE)

    _, tags, _ = asked(ctx, [policy, quote], "Do defence costs reduce the limits?", k=1)

    assert tags == [("S1", POLICY, "p. 4-6 of 7"), ("S2", POLICY, "p. 1-2 of 7"), ("S3", POLICY, "p. 6-7 of 7"),
                    ("S4", QUOTE, "p. 1-3 of 3"), ("S5", QUOTE, "p. 2-3 of 3")]
