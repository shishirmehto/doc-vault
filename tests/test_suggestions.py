"""Follow-up suggestions: cleaned from the answer model, or built from key-fact labels when the gate refuses."""

from __future__ import annotations

from docvault import insights
from docvault.chat import fact_suggestions, suggestions

from .conftest import POLICY, QUOTE
from .test_chat import ask, ready


def analysed(ctx, name: str):
    doc_id = ready(ctx, name)
    insights.run_analyze(ctx, doc_id)
    return doc_id


def test_suggestions_keep_three_distinct_questions_and_drop_the_one_just_asked():
    too_long = "What " + "very " * 40 + "long question?"
    assert len(too_long) > 160

    kept = suggestions(["", "   ", "what is the  EACH occurrence limit?", "Name the insurer.", too_long,
                        "Who is the  insurer?", "who is the insurer?", "Is there a deductible?",
                        "What is the premium?", "What is the policy period?"],
                       "What is the each occurrence limit?")

    assert kept == ("Who is the insurer?", "Is there a deductible?", "What is the premium?")


def test_refused_turns_suggest_labels_of_two_words_or_more_round_by_round_across_documents():
    policy = ["Insurer", "Policy Number", "Named Insured", "Mailing Address"]
    quote = ["Insurer", "Quote Number", "Named Insured"]

    assert fact_suggestions([policy, quote], "Is flood damage covered?") == (
        "What is the Policy Number?", "What is the Quote Number?", "What is the Named Insured?")
    assert fact_suggestions([policy], "what is the policy number?") == (
        "What is the Named Insured?", "What is the Mailing Address?")
    assert fact_suggestions([[], ["Insurer"]], "Is flood damage covered?") == ()


def test_an_answer_and_a_refusal_both_offer_questions_that_come_back_answered(client, ctx):
    policy, quote = analysed(ctx, POLICY), analysed(ctx, QUOTE)
    chat_id = client.post("/chats", json={"document_ids": [str(policy), str(quote)]}).json()["id"]

    answered = ask(client, chat_id, "What is the each occurrence limit?")
    refused = ask(client, chat_id, "Is flood damage covered?")

    assert (answered["status"], answered["suggestions"]) == ("answered", [
        "What is the Policy Number?", "What is the Named Insured?", "What is the Mailing Address?"])
    assert (refused["reason"], refused["suggestions"]) == ("no_evidence", [
        "What is the Policy Number?", "What is the Quote Number?", "What is the Named Insured?"])
    assert [turn["suggestions"] for turn in client.get(f"/chats/{chat_id}/messages").json()["turns"]] == [
        answered["suggestions"], refused["suggestions"]]
    for question in answered["suggestions"] + refused["suggestions"]:
        followed = ask(client, chat_id, question)
        assert (question, followed["status"]) == (question, "answered")
