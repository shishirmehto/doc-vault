"""The retrieval-only eval: hit@k, ranks and MRR, the frozen manifest, and one run over the samples."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from uuid import UUID

import pytest

from docvault import chat, retrieval_evals
from docvault.config import Settings
from docvault.domain import ChunkId, DocRef, DocumentId, PageBasis, PageMap, Purpose
from docvault.evals import Ratio
from docvault.retrieval_evals import CaseFile, Document, Question, Scored

from .conftest import SAMPLES, TEST_DATABASE_URL

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "retrieval"
MANIFEST = FIXTURES / "documents.yaml"
POLICY = DocRef(DocumentId(UUID(int=1)), "policy.pdf", PageBasis.PHYSICAL, 9)
QUOTE = DocRef(DocumentId(UUID(int=2)), "quote.pdf", PageBasis.PHYSICAL, 9)


def passage(n: int, doc: DocRef, pages: tuple[int, ...], similarity: float, rrf: float) -> chat.Candidate:
    return chat.Candidate(ChunkId(UUID(int=100 + n)), doc, f"passage {n}",
                          PageMap(tuple((at * 100, page) for at, page in enumerate(pages))), similarity, rrf, False)


def found(retrieved: list[chat.Candidate], **named: list[chat.Candidate]) -> chat.Found:
    return chat.Found(retrieved, named.get("on_pages", []), named.get("in_sections", []), named.get("openings", []))


# ------------------------------------------------------------------ scoring


def score(retrieved: list[chat.Candidate], doc: DocRef, pages: set[int], **named: list[chat.Candidate]) -> Scored:
    return retrieval_evals.score(found(retrieved, **named), doc.id, frozenset(pages), per_doc_min=2,
                                 min_similarity=0.3)


def test_a_passage_ranked_fifth_by_fusion_misses_at_4_though_it_is_first_by_similarity():
    retrieved = [passage(1, POLICY, (1,), 0.50, 0.030), passage(2, POLICY, (2,), 0.40, 0.029),
                 passage(3, POLICY, (3,), 0.45, 0.028), passage(4, POLICY, (4,), 0.30, 0.027),
                 passage(5, POLICY, (8, 9), 0.90, 0.020), passage(6, POLICY, (6,), 0.20, 0.010)]

    scored = score(retrieved, POLICY, {9}, openings=[retrieved[0]])

    assert scored == Scored((False, True, True), fused_rank=5, vector_rank=1,
                            top=(("policy.pdf", (1,)), ("policy.pdf", (2,)), ("policy.pdf", (3,))), gate=True)


def test_the_expected_pages_count_only_in_the_expected_document():
    retrieved = [passage(1, QUOTE, (9,), 0.60, 0.030), passage(2, QUOTE, (9,), 0.55, 0.029),
                 passage(3, QUOTE, (3,), 0.50, 0.028), passage(4, POLICY, (1,), 0.45, 0.027),
                 passage(5, QUOTE, (4,), 0.40, 0.026), passage(6, POLICY, (9,), 0.58, 0.025)]

    scored = score(retrieved, POLICY, {9})

    assert (scored.hits, scored.fused_rank, scored.vector_rank) == ((False, True, True), 6, 2)


def test_a_named_page_is_a_hit_at_every_k_but_has_no_rank():
    retrieved = [passage(1, POLICY, (1,), 0.50, 0.030), passage(2, POLICY, (2,), 0.40, 0.029)]

    scored = score(retrieved, POLICY, {7}, on_pages=[passage(9, POLICY, (7,), 0.10, 0.0)])

    assert (scored.hits, scored.fused_rank, scored.vector_rank) == ((True, True, True), None, None)


def test_an_opening_passage_is_a_hit_at_every_k_but_has_no_rank():
    retrieved = [passage(1, POLICY, (2,), 0.50, 0.030), passage(2, POLICY, (3,), 0.40, 0.029)]

    scored = score(retrieved, POLICY, {1}, openings=[passage(0, POLICY, (1,), 0.10, 0.0)])

    assert (scored.hits, scored.fused_rank, scored.vector_rank) == ((True, True, True), None, None)


def test_an_rrf_tie_goes_to_the_higher_similarity():
    retrieved = [passage(1, POLICY, (1,), 0.40, 0.030), passage(2, POLICY, (2,), 0.50, 0.030)]

    assert score(retrieved, POLICY, {2}).fused_rank == 1


@pytest.mark.parametrize(("before", "rank"), [(49, 50), (50, None)])
def test_ranks_stop_at_50(before, rank):
    retrieved = [passage(n, POLICY, (1,), 0.9 - n / 100, 0.05 - n / 10_000) for n in range(before)]
    retrieved.append(passage(before, POLICY, (2,), 0.01, 0.0001))

    scored = score(retrieved, POLICY, {2})

    assert (scored.hits, scored.fused_rank, scored.vector_rank) == ((False, False, False), rank, rank)


def test_a_question_below_the_relevance_gate_still_scores_its_hits():
    scored = score([passage(1, POLICY, (1,), 0.25, 0.030)], POLICY, {1})

    assert (scored.hits, scored.fused_rank, scored.gate) == ((True, True, True), 1, False)


def test_mrr_counts_a_miss_as_zero():
    assert retrieval_evals.mrr([1, 2, None, 4]) == 0.4375


def test_a_summary_counts_hits_per_k_both_mrrs_and_the_gate():
    summary = retrieval_evals.summarise([Scored((False, True, True), 5, 1, (), True),
                                         Scored((True, True, True), 1, 2, (), False),
                                         Scored((False, False, False), None, 40, (), True)])

    assert (summary.n, summary.hits, summary.gate) == (3, (Ratio(1, 3), Ratio(2, 3), Ratio(2, 3)), Ratio(2, 3))
    assert (summary.mrr_fused, summary.mrr_vector) == (pytest.approx(0.4), pytest.approx(1.525 / 3))


# ------------------------------------------------------------------ the manifest and the case files


def test_the_loader_reads_scopes_from_the_question_or_the_manifest():
    documents, [case_file] = retrieval_evals.load(MANIFEST)

    assert sorted(documents) == ["sample-cgl-policy.pdf", "sample-renewal-quote.pdf"]
    assert (case_file.name, len(case_file.questions)) == ("cases.yaml", 7)
    roofing, *_, silica = case_file.questions
    assert roofing == Question("roofing", "Is roofing work on tall buildings excluded?", ("sample-cgl-policy.pdf",),
                               "sample-cgl-policy.pdf", frozenset({5}),
                               "roofing work performed on any building more than three storeys or 40 feet in height, "
                               "whichever is lower", ("40 feet",))
    assert (silica.scope, silica.doc, silica.pages) == (
        ("sample-cgl-policy.pdf", "sample-renewal-quote.pdf"), "sample-renewal-quote.pdf", frozenset({3}))


def write_case_set(folder: Path, questions: str, *, sha256: str | None = None) -> Path:
    cases = folder / "cases.yaml"
    cases.write_text(questions)
    manifest = folder / "documents.yaml"
    manifest.write_text(
        "documents:\n"
        "- {file: a.pdf, sha256: aa, pages: 1, url: u, licence: l}\n"
        "- {file: b.pdf, sha256: bb, pages: 1, url: u, licence: l}\n"
        f"cases:\n- {{file: cases.yaml, sha256: '{sha256 or hashlib.sha256(cases.read_bytes()).hexdigest()}'}}\n")
    return manifest


def test_a_case_file_edited_after_it_was_frozen_is_refused(tmp_path):
    manifest = write_case_set(tmp_path, "questions: []\n", sha256="0" * 64)

    with pytest.raises(ValueError, match=r"cases.yaml: sha256 [0-9a-f]{64} is not the frozen 0{64}"):
        retrieval_evals.load(manifest)


def test_the_loader_lists_every_scope_problem(tmp_path):
    manifest = write_case_set(tmp_path, "questions:\n"
                              "- {id: q1, ask: x, pages: [1]}\n"
                              "- {id: q2, ask: x, docs: [a.pdf, b.pdf], pages: [1]}\n"
                              "- {id: q3, ask: x, docs: [a.pdf, c.pdf], doc: b.pdf, pages: []}\n"
                              "- {id: q4, ask: x, docs: [a.pdf], pages: ['1', 2]}\n")

    with pytest.raises(ValueError) as raised:
        retrieval_evals.load(manifest)

    assert str(raised.value) == (
        "cases.yaml q1: no docs, and the manifest gives the case file none; "
        "cases.yaml q2: docs holds 2 documents, so doc must name the one with the expected pages; "
        "cases.yaml q3: c.pdf is not in the manifest; cases.yaml q3: doc b.pdf is not in docs; "
        "cases.yaml q3: no expected pages; "
        "cases.yaml q4: pages ['1', 2] are not all page numbers of a.pdf (1 to 1)")


def test_a_case_file_missing_from_the_manifest_is_refused(tmp_path):
    with pytest.raises(ValueError, match="is not listed in"):
        retrieval_evals.load(MANIFEST, [tmp_path / "other.yaml"])


def test_a_document_whose_bytes_differ_from_the_manifest_is_refused(tmp_path):
    shutil.copy(SAMPLES / "sample-renewal-quote.pdf", tmp_path / "sample-cgl-policy.pdf")
    documents, _ = retrieval_evals.load(MANIFEST)

    with pytest.raises(ValueError) as raised:
        retrieval_evals.check_documents(tmp_path, documents.values())

    assert str(raised.value) == (
        f"sample-cgl-policy.pdf: sha256 b4bbeb9a702455f888bec0c125d582eb02a88dfaf2e15b1245144a618646b58b in {tmp_path}, "
        "the manifest says 6957841dc0c2ce00c707b198528725ff3ffe28e313db8a873104122497f7330e; "
        f"sample-renewal-quote.pdf: not in {tmp_path}")


def test_verify_finds_a_quote_on_the_wrong_page():
    documents, _ = retrieval_evals.load(MANIFEST)
    paths = retrieval_evals.check_documents(SAMPLES, documents.values())
    quote = "roofing work performed on any building more than three storeys"
    case_file = CaseFile("cases.yaml", "", (
        Question("right", "?", ("sample-cgl-policy.pdf",), "sample-cgl-policy.pdf", frozenset({5}), quote, ("40 feet",)),
        Question("wrong", "?", ("sample-cgl-policy.pdf",), "sample-cgl-policy.pdf", frozenset({4}), quote, ("40 feet",)),
        Question("no quote", "?", ("sample-cgl-policy.pdf",), "sample-cgl-policy.pdf", frozenset({4}))))

    failures = retrieval_evals.verify(paths, documents, [case_file], max_pages=2000)

    assert failures == ["cases.yaml wrong: quote not on pages [4]", "cases.yaml wrong: 40 feet not on pages [4]"]


def test_verify_checks_the_page_count_against_the_manifest():
    documents, case_files = retrieval_evals.load(MANIFEST)
    paths = retrieval_evals.check_documents(SAMPLES, documents.values())
    documents["sample-cgl-policy.pdf"] = Document("sample-cgl-policy.pdf", "", 8, "")

    assert retrieval_evals.verify(paths, documents, case_files, max_pages=2000) == [
        "sample-cgl-policy.pdf: 7 pages, the manifest says 8"]


# ------------------------------------------------------------------ a run


def test_a_run_over_the_samples_reports_hits_ranks_and_spend(tmp_path):
    documents, case_files = retrieval_evals.load(MANIFEST)
    paths = retrieval_evals.check_documents(SAMPLES, documents.values())
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, chat_provider="fake", embed_provider="fake",
                        chunk_max_chars=400, chunk_overlap_chars=50)

    result = retrieval_evals.run(settings, paths, case_files)
    report = retrieval_evals.render(result, "the command")

    assert [(usage.purpose, usage.model, usage.calls) for usage in result.spend] == [
        (Purpose.EMBED_CHUNKS, "fake-hash", 2), (Purpose.EMBED_QUERY, "fake-hash", 1)]
    assert "chunk_max_chars 400, chunk_overlap_chars 50, retrieval_k 16" in report
    assert "**The fake embedder ran: these numbers say nothing about retrieval quality.**" in report
    assert "\n".join([
        "| Case file | n | hit@4 | hit@8 | hit@16 | MRR@50 fused | MRR@50 vector only | Gate passed |",
        "|---|---|---|---|---|---|---|---|",
        "| cases.yaml | 7 | 3/7 (43%) | 5/7 (71%) | 6/7 (86%) | 0.434 | 0.458 | 6/7 (86%) |",
        "| All | 7 | 3/7 (43%) | 5/7 (71%) | 6/7 (86%) | 0.434 | 0.458 | 6/7 (86%) |",
        "",
        "Misses at 16:",
        "",
        "| Case file | Question | Expected pages | Top 3 by fused rank | Fused rank | Vector rank |",
        "|---|---|---|---|---|---|",
        "| cases.yaml | off-page | 7 | p. 2; p. 4-5; p. 4 | 17 | 12 |"]) in report
    path = tmp_path / "runs" / "report.md"
    retrieval_evals.write(result, "the command", path)
    by_id = {row["id"]: row for row in json.loads(path.with_suffix(".json").read_text())["questions"]}
    assert [(row["id"], row["fused_rank"], row["vector_rank"]) for row in by_id.values()] == [
        ("roofing", 1, 1), ("cancellation", 1, 1), ("limits", 6, 6), ("policy", 5, 3), ("insured", 9, 8),
        ("off-page", 17, 12), ("silica", 2, 2)]
    assert by_id["silica"] == {
        "case_file": "cases.yaml", "id": "silica", "doc": "sample-renewal-quote.pdf", "pages": [3],
        "hit@4": True, "hit@8": True, "hit@16": True, "fused_rank": 2, "vector_rank": 2, "gate": False,
        "top3": [{"doc": "sample-renewal-quote.pdf", "pages": [1]}, {"doc": "sample-renewal-quote.pdf", "pages": [3]},
                 {"doc": "sample-renewal-quote.pdf", "pages": [3]}]}
