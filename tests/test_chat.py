"""Chat: start, ask, follow up, refuse, compare, history. Fake provider, real Postgres."""

from __future__ import annotations

import dataclasses
from uuid import UUID, uuid4

from docvault import chat
from docvault.ai import FakeChat
from docvault.api import app
from docvault.chat import Candidate
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
from docvault.prompts import AnswerDraft, EvidenceDraft

from .conftest import FACTS, POLICY, QUOTE, SAMPLES, TRUTH, scalar
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

POLICY_DOC = DocRef(DocumentId(UUID(int=1)), POLICY, PageBasis.PHYSICAL)
QUOTE_DOC = DocRef(DocumentId(UUID(int=2)), QUOTE, PageBasis.PHYSICAL)


def source(label: str, doc: DocRef, text: str, page_map: PageMap, chunk: int) -> Source:
    return Source(label, ChunkId(UUID(int=chunk)), doc, text, page_map, similarity=0.5, rrf=0.03)


def draft(answer: str, *evidence: tuple[str, str], supported: bool = True) -> AnswerDraft:
    return AnswerDraft(supported=supported, answer=answer,
                       evidence=[EvidenceDraft(source=label, quote=quote) for label, quote in evidence], follow_ups=[])


def limits() -> list[Source]:
    policy, quote = FACTS["policy-each-occurrence-limit"], FACTS["quote-each-occurrence-limit"]
    return [source("S1", POLICY_DOC, f"Limits of Insurance\n{policy['text']}", PageMap(((0, 2),)), chunk=11),
            source("S2", QUOTE_DOC, f"Proposed Limits\n{quote['text']}", PageMap(((0, 2),)), chunk=21)]


def test_ground_cites_the_page_each_quote_sits_on_inside_a_page_spanning_chunk():
    clause = FACTS["policy-defence-clause"]
    text = f"{clause['text']}\n\n{clause['continues']['text']}"
    spanning = source("S1", POLICY_DOC, text, PageMap(((0, 4), (len(clause["text"]) + 2, 5))), chunk=7)

    answer = chat.ground(draft("There is no duty to defend uncovered suits [S1].",
                               ("S1", clause["continues"]["text"].upper()), ("S1", clause["text"])), [spanning])

    assert answer == Answered("There is no duty to defend uncovered suits [1][2].", (
        Citation(POLICY_DOC.id, POLICY, 5, PageBasis.PHYSICAL, clause["continues"]["text"], ChunkId(UUID(int=7))),
        Citation(POLICY_DOC.id, POLICY, 4, PageBasis.PHYSICAL, clause["text"], ChunkId(UUID(int=7)))))


def test_ground_drops_unknown_labels_and_paraphrases_and_removes_their_markers():
    limit = FACTS["policy-each-occurrence-limit"]["text"]

    answer = chat.ground(draft("The limit is $1,000,000 [S1]. Defence costs reduce it [S9]. The quote keeps it [S2].",
                               ("S1", limit), ("S9", limit), ("S2", "the quote keeps the limit at one million")),
                         limits())

    assert answer == Answered("The limit is $1,000,000 [1]. Defence costs reduce it. The quote keeps it.", (
        Citation(POLICY_DOC.id, POLICY, 2, PageBasis.PHYSICAL, limit, ChunkId(UUID(int=11))),))


def test_ground_rewrites_a_marker_naming_two_sources_and_cites_a_repeated_quote_once():
    limit = FACTS["quote-each-occurrence-limit"]["text"]

    answer = chat.ground(draft("Both documents set it at $1,000,000 [S1, S2].",
                               ("S1", limit), ("S2", limit), ("S1", limit)), limits())

    assert answer.text == "Both documents set it at $1,000,000 [1][2]."
    assert [(c.filename, c.quote) for c in answer.citations] == [(POLICY, limit), (QUOTE, limit)]


def test_ground_refuses_a_declined_draft_and_a_draft_with_nothing_verified():
    declined = chat.ground(draft("The documents do not mention flood cover [S1].", supported=False), limits())
    unverified = chat.ground(draft("The limit is one million [S1].", ("S1", "a limit of one million dollars")),
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
