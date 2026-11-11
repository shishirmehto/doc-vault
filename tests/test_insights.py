"""Insights: generation, quote verification, coverage, failure and recovery. Fake provider, real Postgres."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from docvault import documents, insights, pages, tasks
from docvault.ai import FakeChat, ProviderError
from docvault.db import Lease, pipeline_steps
from docvault.domain import DocKind, DocumentId, ErrorCode, Failure, InsightsPending, VaultError
from docvault.prompts import InsightsDraft, KeyFactDraft

from .conftest import ENDORSEMENT, FACTS, POLICY, QUOTE, SAMPLES, scalar, step_rows
from .test_documents import FailingEmbedder, scanned_pdf, upload, with_embedder


def post(client: TestClient, path: Path) -> DocumentId:
    return DocumentId(UUID(client.post("/documents", files={"file": (path.name, path.read_bytes())}).json()["id"]))


def indexed(ctx, path: Path) -> DocumentId:
    doc_id = upload(ctx, path).document.id
    documents.run_index(ctx, doc_id)
    return doc_id


def with_chat(ctx, chat):
    return dataclasses.replace(ctx, ai=dataclasses.replace(ctx.ai, chat=chat))


def provider_down(code: str, *, retryable: bool, prompt: str = "insights") -> FakeChat:
    """A FakeChat whose `prompt` handler fails the way a provider does; the other prompts still work."""
    def raise_error(_prompt):
        raise ProviderError(code, retryable=retryable)

    class DownChat(FakeChat):
        handlers = {**FakeChat.handlers, prompt: raise_error}

    return DownChat()


def analyze_steps(ctx, doc_id) -> list[tuple]:
    return [(attempt, outcome, error) for step, attempt, outcome, error in step_rows(ctx.db, doc_id) if step == "analyze"]


def test_indexing_enqueues_analysis_and_the_policy_gets_verified_insights(client, ctx):
    doc_id = post(client, SAMPLES / POLICY)
    documents.run_index(ctx, doc_id)
    assert ctx.jobs.analyzed == [doc_id]
    assert client.get(f"/documents/{doc_id}/insights").json() == {"status": "pending"}

    insights.run_analyze(ctx, doc_id)

    body = client.get(f"/documents/{doc_id}/insights").json()
    assert {k: body[k] for k in ("status", "title", "document_type", "tags", "coverage", "dropped_key_facts", "model",
                                 "prompt_version")} == {
        "status": "ready", "title": "COMMERCIAL GENERAL LIABILITY POLICY", "document_type": "policy",
        "tags": ["commercial", "general", "liability", "policy"], "coverage": {"pages_read": 7, "page_count": 7},
        "dropped_key_facts": 0, "model": "fake-chat", "prompt_version": "insights-v1"}
    cited = {(fact["page"], fact["quote"]) for fact in body["key_facts"]}
    for fact_id in ("policy-number", "policy-each-occurrence-limit"):
        assert (FACTS[fact_id]["page"], " ".join(FACTS[fact_id]["text"].split())) in cited
    limit = FACTS["policy-each-occurrence-limit"]
    assert [fact for fact in body["key_facts"] if fact["quote"] == limit["text"]] == [{
        "label": "Each Occurrence Limit", "value": "$1,000,000", "page": limit["page"], "page_label": "p. 2",
        "quote": limit["text"], "page_url": f"/documents/{doc_id}/pages/2"}]
    assert client.get(f"/documents/{doc_id}").json()["insights_status"] == "ready"
    with ctx.db.connection() as conn:
        ledger = conn.execute("SELECT document_id, prompt_name, prompt_version, model, ok FROM llm_calls"
                              " WHERE purpose = 'insights'").fetchall()
    assert ledger == [{"document_id": doc_id, "prompt_name": "insights", "prompt_version": "insights-v1",
                       "model": "fake-chat", "ok": True}]


def test_verify_keeps_quotes_found_on_the_cited_page_and_drops_the_rest():
    policy = pages.extract(DocKind.PDF, SAMPLES / POLICY, max_pages=10).pages
    limit, deductible, clause = (FACTS[i] for i in ("policy-each-occurrence-limit", "policy-deductible",
                                                     "policy-defence-clause"))
    across_the_break = f"{clause['text']} {clause['continues']['text']}"
    draft = InsightsDraft(title="CGL policy", document_type="policy", summary="A liability policy.", tags=["cgl"],
                          key_facts=[
        KeyFactDraft(label="Each Occurrence Limit", value="$1,000,000", page=limit["page"], quote=limit["text"]),
        KeyFactDraft(label="Property Damage Deductible", value="$2,500", page=deductible["page"] - 1,
                     quote=deductible["text"]),
        KeyFactDraft(label="Each Occurrence Limit", value="$5,000,000", page=2,
                     quote="Each Occurrence Limit $5,000,000"),
        KeyFactDraft(label="No duty to defend", value="uncovered suits", page=clause["page"], quote=across_the_break),
    ])

    body, dropped = insights.verify(draft, policy)

    assert [(fact.label, fact.page, fact.quote) for fact in body.key_facts] == [
        ("Each Occurrence Limit", limit["page"], limit["text"])]
    assert dropped == 3


def test_a_document_over_the_budget_reports_the_pages_it_read(ctx):
    doc_id = indexed(ctx, SAMPLES / POLICY)
    small = dataclasses.replace(ctx, settings=ctx.settings.model_copy(update={"insights_max_chars": 2000}))

    insights.run_analyze(small, doc_id)

    ready = documents.get(ctx.db, doc_id).insights
    assert (ready.pages_read, ready.page_count) == (1, 7)
    assert {fact.page for fact in ready.body.key_facts} == {1}
    assert " ".join(FACTS["policy-number"]["text"].split()) in [fact.quote for fact in ready.body.key_facts]


def test_a_provider_failure_fails_only_the_insights_and_retry_reruns_them(client, ctx, monkeypatch):
    doc_id = post(client, SAMPLES / POLICY)
    documents.run_index(ctx, doc_id)
    monkeypatch.setattr(tasks, "_ctx", lambda: with_chat(ctx, provider_down("insufficient_quota", retryable=False)))

    tasks.analyze_document.apply(args=[str(doc_id)])

    failed = {"status": "failed", "error": {"code": "ai_unavailable", "retryable": False, "details": {},
                                            "message": "The AI provider could not complete the request "
                                                       "(insufficient_quota)."}}
    assert client.get(f"/documents/{doc_id}/insights").json() == failed
    assert {k: client.get(f"/documents/{doc_id}").json()[k] for k in ("status", "insights_status")} == {
        "status": "ready", "insights_status": "failed"}
    insights.run_analyze(ctx, doc_id)   # a redelivery does not undo a terminal failure
    assert client.get(f"/documents/{doc_id}/insights").json() == failed

    ctx.jobs.analyzed.clear()
    retried = client.post(f"/documents/{doc_id}/retry")
    assert (retried.status_code, retried.json()["status"], retried.json()["insights_status"]) == (202, "ready", "pending")
    assert ctx.jobs.analyzed == [doc_id]
    insights.run_analyze(ctx, doc_id)

    assert client.get(f"/documents/{doc_id}/insights").json()["status"] == "ready"
    assert analyze_steps(ctx, doc_id) == [(1, "failed", "ai_unavailable"), (1, "ok", None)]
    insights.fail(ctx, doc_id, Failure(ErrorCode.AI_UNAVAILABLE, "A late failure.", retryable=True))
    assert client.get(f"/documents/{doc_id}/insights").json()["status"] == "ready"


def test_a_document_that_failed_to_parse_has_insights_unavailable(client, ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "_ctx", lambda: ctx)
    doc_id = post(client, scanned_pdf(tmp_path))

    tasks.index_document.apply(args=[str(doc_id)])
    insights.run_analyze(ctx, doc_id)   # a stray delivery finds no pages to read

    assert client.get(f"/documents/{doc_id}/insights").json() == {
        "status": "unavailable", "reason": "This document failed to process, so it has no insights."}
    assert (ctx.jobs.analyzed, analyze_steps(ctx, doc_id)) == ([], [])


def test_analysis_runs_once_and_a_held_lease_makes_a_delivery_a_no_op(ctx):
    doc_id = indexed(ctx, SAMPLES / QUOTE)
    with pipeline_steps(ctx.db, doc_id, Lease.ANALYZE, 1):
        insights.run_analyze(ctx, doc_id)
    assert documents.get(ctx.db, doc_id).insights == InsightsPending()

    insights.run_analyze(ctx, doc_id)
    insights.run_analyze(ctx, doc_id)

    body = documents.get(ctx.db, doc_id).insights.body
    assert (body.title, body.document_type) == ("RENEWAL QUOTE - COMMERCIAL GENERAL LIABILITY", "quote")
    assert scalar(ctx.db, "SELECT count(*) FROM llm_calls WHERE purpose = 'insights'") == 1
    assert analyze_steps(ctx, doc_id) == [(1, "ok", None)]


def test_reconcile_enqueues_analysis_for_aged_documents_without_insights(ctx, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("Broker notes. The client asked about flood cover.")
    aged, fresh, analysed, trying = (indexed(ctx, path) for path in (SAMPLES / POLICY, SAMPLES / QUOTE,
                                                                      SAMPLES / ENDORSEMENT, notes))
    insights.run_analyze(ctx, analysed)
    with pytest.raises(VaultError, match="rate_limited"):
        insights.run_analyze(with_chat(ctx, provider_down("rate_limited", retryable=True)), trying)
    with ctx.db.connection() as conn:
        conn.execute("UPDATE documents SET updated_at = now() - interval '5 minutes' WHERE id <> %s", (fresh,))
    ctx.jobs.analyzed.clear()

    assert documents.reconcile(ctx) == 1
    assert ctx.jobs.analyzed == [aged]


def test_a_document_that_failed_after_parsing_keeps_insights_cited_by_section(client, ctx):
    doc_id = post(client, SAMPLES / ENDORSEMENT)
    documents.run_index(with_embedder(ctx, FailingEmbedder(0, ProviderError("timeout", retryable=True))), doc_id,
                        attempt=documents.MAX_RETRIES + 1)
    insights.run_analyze(ctx, doc_id)
    assert client.post(f"/documents/{doc_id}/retry").json()["status"] == "queued"

    facts = client.get(f"/documents/{doc_id}/insights").json()["key_facts"]
    premium = FACTS["endorsement-additional-premium"]
    assert [fact for fact in facts if fact["label"] == "Additional Premium"] == [{
        "label": "Additional Premium", "value": "$350", "page": premium["page"], "page_label": "section 1",
        "quote": premium["text"], "page_url": f"/documents/{doc_id}/pages/1"}]


def test_a_document_deleted_during_analysis_leaves_only_the_spend_behind(ctx):
    doc_id = indexed(ctx, SAMPLES / QUOTE)

    def delete_then_answer(prompt):
        documents.delete(ctx, doc_id)
        return FakeChat.handlers["insights"](prompt)

    class DeletingChat(FakeChat):
        handlers = {"insights": delete_then_answer}

    insights.run_analyze(with_chat(ctx, DeletingChat()), doc_id)
    insights.fail(ctx, doc_id, Failure(ErrorCode.AI_UNAVAILABLE, "A late failure.", retryable=True))

    assert scalar(ctx.db, "SELECT count(*) FROM document_insights") == 0
    assert scalar(ctx.db, "SELECT count(*) FROM llm_calls WHERE purpose = 'insights' AND document_id = %s",
                  (doc_id,)) == 1
    assert analyze_steps(ctx, doc_id) == [(1, "ok", None)]
