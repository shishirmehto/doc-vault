"""Each turn's retrieval log: what search found, with scores, and which passages the answer step was sent.
Fake provider, real Postgres.

The fake embedder scores many chunks exactly 0.0, and Postgres ranks equal distances in any order, so a test pins
a passage's chunk and rank only where its similarity is unique."""

from __future__ import annotations

import dataclasses
from collections import Counter
from uuid import UUID, uuid4

import pytest

from docvault import chat
from docvault.chat import Candidate, Found
from docvault.domain import ChunkId, DocRef, DocumentId, PageBasis, PageMap, PassageKind, TurnOutcome
from docvault.fake_ai import FakeChat

from .conftest import ENDORSEMENT, scalar
from .test_cache import memory_cache, new_chat
from .test_chat import Recording, ask, handbook, ledger, ready
from .test_insights import with_chat

RETRIEVED, PAGE, SECTION, OPENING = PassageKind.RETRIEVED, PassageKind.PAGE, PassageKind.SECTION, PassageKind.OPENING
INSURED = "Who is the additional insured on this endorsement?"


def ask_logged(ctx, document_ids, question: str, backend: FakeChat):
    """Ask in a new chat on `backend`; returns the turn, its log, and the sources the answer prompt got, if any."""
    chat_id = chat.start(ctx.db, document_ids).id
    turn = chat.ask(with_chat(ctx, backend), chat_id, question)
    answers = [prompt.inputs["sources"] for prompt in getattr(backend, "prompts", []) if prompt.name == "answer"]
    return turn, chat.turn_retrieval(ctx.db, chat_id, turn.id), list(answers[0]) if answers else []


def rebuilt(ctx, turn, log):
    with ctx.db.connection() as conn:
        return chat.sent_sources(conn, chat.scope(conn, turn.chat_id), log)


def rows(ctx, log) -> list[tuple]:
    with ctx.db.connection() as conn:
        ords = {row["id"]: row["ord"] for row in conn.execute("SELECT id, ord FROM chunks")}
    return [(p.kind, ords[p.chunk_id], p.first_page, p.last_page, round(p.similarity, 4), round(p.rrf, 4),
             p.lexical_hit, p.rank, p.label) for p in log.passages]


def test_an_answered_turn_logs_every_retrieved_passage_and_the_label_each_was_sent_under(ctx):
    endorsement = ready(ctx, ENDORSEMENT)

    turn, log, sent = ask_logged(ctx, [endorsement], INSURED, Recording())

    assert turn.answer.outcome is TurnOutcome.ANSWERED
    assert (log.query, log.matched, log.min_similarity, log.k, log.omitted) == (INSURED, True, 0.184, 16, 1)
    assert rows(ctx, log) == [
        (RETRIEVED, 0, 1, 1, 0.3348, 0.0246, True, 1, "S1"), (RETRIEVED, 2, 2, 2, 0.2953, 0.0241, True, 2, "S2"),
        (RETRIEVED, 1, 1, 2, 0.2887, 0.0239, True, 3, "S3")]
    assert {p.document_id for p in log.passages} == {endorsement}
    assert rebuilt(ctx, turn, log) == sent


@pytest.mark.parametrize(("question", "outcome", "logged", "sent_pages"), [
    ("How does the endorsement end?", TurnOutcome.MODEL_DECLINED, [
        (RETRIEVED, 0, 1, 1, 0.102, 0.0246, False, 1, "S3"), (RETRIEVED, 2, 2, 2, 0.0782, 0.0242, True, 2, None),
        (RETRIEVED, 1, 1, 2, 0.0689, 0.0238, False, 3, "S4"), (PAGE, 1, 2, 2, 0.0689, 0.0, False, None, "S1"),
        (PAGE, 2, 2, 2, 0.0782, 0.0, False, None, "S2")], [(2, 2), (2, 2), (1, 1), (1, 2)]),
    ("Who does Endorsement SM 40 03 add?", TurnOutcome.ANSWERED, [
        (RETRIEVED, 0, 1, 1, 0.2019, 0.0246, False, 1, None), (RETRIEVED, 2, 2, 2, 0.1056, 0.0242, False, 2, "S3"),
        (RETRIEVED, 1, 1, 2, 0.0487, 0.0238, False, 3, None), (SECTION, 0, 1, 1, 0.2019, 0.0, False, None, "S1"),
        (SECTION, 1, 1, 2, 0.0487, 0.0, False, None, "S2")], [(1, 1), (1, 2), (2, 2)]),
])
def test_a_named_page_or_section_logs_its_passages_first_in_the_prompt_and_rebuilds_as_sent(
        ctx, question, outcome, logged, sent_pages):
    endorsement = ready(ctx, ENDORSEMENT)

    turn, log, sent = ask_logged(ctx, [endorsement], question, Recording())

    assert (turn.answer.outcome, log.matched, log.omitted) == (outcome, True, 1)
    assert rows(ctx, log) == logged
    assert [(s.page_map.first_page, s.page_map.last_page) for s in rebuilt(ctx, turn, log)] == sent_pages
    assert rebuilt(ctx, turn, log) == sent


def test_past_a_failed_gate_the_log_labels_the_opening_passage_the_answer_step_was_sent(ctx):
    endorsement = ready(ctx, ENDORSEMENT)

    turn, log, sent = ask_logged(ctx, [endorsement], "Who is added as an insured?", Recording())

    assert (turn.answer.outcome, log.matched, log.omitted) == (TurnOutcome.ANSWERED, False, 0)
    assert rows(ctx, log) == [
        (RETRIEVED, 0, 1, 1, 0.1641, 0.0245, False, 1, None), (RETRIEVED, 1, 1, 2, 0.1344, 0.0243, False, 2, None),
        (RETRIEVED, 2, 2, 2, 0.0712, 0.0238, False, 3, None), (OPENING, 0, 1, 1, 0.1641, 0.0, False, None, "S1")]
    assert rebuilt(ctx, turn, log) == sent


@pytest.mark.parametrize(("question", "outcome", "matched", "labels"), [
    ("Who won the 2022 football World Cup?", TurnOutcome.NO_EVIDENCE, False, [None, None, None]),
    ("Insured?", TurnOutcome.MODEL_DECLINED, True, ["S1", "S2", "S3"]),
])
def test_a_refused_and_a_declined_turn_keep_their_log(ctx, question, outcome, matched, labels):
    endorsement = ready(ctx, ENDORSEMENT)

    turn, log, _ = ask_logged(ctx, [endorsement], question, FakeChat())

    assert (turn.answer.outcome, log.query, log.matched, log.omitted) == (outcome, question, matched, 1)
    assert [(p.kind, p.rank, p.label) for p in log.passages] == [
        (RETRIEVED, rank, label) for rank, label in enumerate(labels, start=1)]


def test_a_cached_answer_logs_the_retrieval_its_answer_was_built_from_with_no_model_call(client, ctx):
    memory_cache(ctx, client)
    endorsement = ready(ctx, ENDORSEMENT)
    first_chat, again_chat = new_chat(client, endorsement), new_chat(client, endorsement)

    first = ask(client, first_chat, INSURED)
    again = ask(client, again_chat, "who is the ADDITIONAL insured on this   endorsement?")
    logged = [client.get(f"/chats/{chat_id}/messages/{turn['id']}/retrieval").json()
              for chat_id, turn in ((first_chat, first), (again_chat, again))]

    assert (first["cached"], again["cached"]) == (False, True)
    assert logged[1] == logged[0]
    assert (logged[1]["query"], [p["label"] for p in logged[1]["passages"]]) == (INSURED, ["S1", "S2", "S3"])
    assert ledger(ctx) == {first["id"]: ["embed_query", "answer"]}


def test_the_retrieval_route_returns_the_stored_log_and_404_for_an_unknown_chat_or_turn_or_one_without_a_log(
        client, ctx):
    endorsement = ready(ctx, ENDORSEMENT)
    chat_id, other_chat = new_chat(client, endorsement), new_chat(client, endorsement)
    turn = ask(client, chat_id, INSURED)
    old = ask(client, other_chat, "Insured?")
    with ctx.db.connection() as conn:
        conn.execute("UPDATE chat_turns SET retrieval = NULL WHERE id = %s", (old["id"],))
    first_chunk = scalar(ctx.db, "SELECT id FROM chunks WHERE document_id = %s AND ord = 0", (endorsement,))
    nowhere, unknown = uuid4(), uuid4()

    found = client.get(f"/chats/{chat_id}/messages/{turn['id']}/retrieval")
    missing = [client.get(f"/chats/{chat}/messages/{turn_id}/retrieval") for chat, turn_id in (
        (nowhere, turn["id"]), (chat_id, unknown), (other_chat, turn["id"]), (other_chat, old["id"]))]

    assert found.status_code == 200
    assert found.json() == scalar(ctx.db, "SELECT retrieval FROM chat_turns WHERE id = %s", (turn["id"],))
    assert {key: value for key, value in found.json().items() if key != "passages"} == {
        "query": INSURED, "matched": True, "min_similarity": 0.184, "k": 16, "omitted": 1}
    assert found.json()["passages"][0] == {
        "chunk_id": str(first_chunk), "document_id": str(endorsement), "kind": "retrieved", "first_page": 1,
        "last_page": 1, "similarity": 0.3348196744918823, "rrf": 1 / 61 + 0.5 / 61, "lexical_hit": True,
        "rank": 1, "label": "S1"}
    assert [(r.status_code, r.json()["error"]["code"], r.json()["error"]["message"]) for r in missing] == [
        (404, "chat_not_found", f"No chat has the id {nowhere}."),
        (404, "turn_not_found", f"Chat {chat_id} has no turn with the id {unknown}."),
        (404, "turn_not_found", f"Chat {other_chat} has no turn with the id {turn['id']}."),
        (404, "turn_not_found", f"Turn {old['id']} has no retrieval log: it was asked before turns kept one.")]


def test_a_two_document_chat_logs_each_documents_passages_in_fused_order(ctx, tmp_path):
    book, endorsement = handbook(ctx, tmp_path), ready(ctx, ENDORSEMENT)
    name = {book: "handbook", endorsement: "endorsement"}

    turn, log, sent = ask_logged(ctx, [book, endorsement], INSURED, Recording())

    assert (log.matched, log.omitted, Counter(name[p.document_id] for p in log.passages)) == (
        True, 2, {"handbook": 11, "endorsement": 3})
    assert [(p.rank, p.label) for p in log.passages] == [(n, f"S{n}") for n in range(1, 15)]
    assert [(name[p.document_id], round(p.similarity, 4)) for p in log.passages[:6]] == [
        ("endorsement", 0.3348), ("endorsement", 0.2953), ("endorsement", 0.2887), ("handbook", 0.2382),
        ("handbook", 0.1213), ("handbook", 0.0943)]
    assert rebuilt(ctx, turn, log) == sent


# ------------------------------------------------------------------ the log builder, pure

DOC = DocRef(DocumentId(UUID(int=1)), ENDORSEMENT, PageBasis.SYNTHETIC, 2)


def passage(name: str, rrf: float, similarity: float = 0.1, *, lexical_hit: bool = False) -> Candidate:
    return Candidate(ChunkId(uuid4()), DOC, name, PageMap(((0, 3), (40, 4))), similarity, rrf, lexical_hit)


def test_the_log_keeps_what_was_sent_the_top_2k_by_fused_rank_and_the_passages_the_gate_decided_on():
    retrieved = [passage("r1", 0.10), passage("r2", 0.09), passage("r3", 0.08, 0.1), passage("r4", 0.08, 0.2),
                 passage("r5", 0.06), passage("r6", 0.05), passage("r7", 0.04, lexical_hit=True), passage("r8", 0.03),
                 passage("r9", 0.02, 0.9), passage("r10", 0.01, lexical_hit=True)]
    opening = passage("o1", 0.0)
    by_name = {c.chunk_id: c.text for c in [*retrieved, opening]}

    log = chat.retrieval_log("q", Found(retrieved, [], [], [opening]), [retrieved[0], retrieved[5]],
                             min_similarity=0.5, k=2)

    assert [(by_name[p.chunk_id], p.rank, p.label) for p in log.passages] == [
        ("r1", 1, "S1"), ("r2", 2, None), ("r4", 3, None), ("r3", 4, None), ("r6", 6, "S2"), ("r7", 7, None),
        ("r9", 9, None)]
    assert (log.query, log.matched, log.min_similarity, log.k, log.omitted) == ("q", True, 0.5, 2, 4)
    assert (log.passages[0].first_page, log.passages[0].last_page, log.passages[0].document_id) == (3, 4, DOC.id)


def test_a_page_passage_equal_to_an_opening_labels_only_the_one_that_was_sent():
    part = passage("page 1", 0.0)
    same = dataclasses.replace(part)

    log = chat.retrieval_log("q", Found([], [part], [], [same]), [part], min_similarity=0.5, k=16)

    assert [(p.kind, p.label) for p in log.passages] == [(PAGE, "S1")]
    assert log.omitted == 1
