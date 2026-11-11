"""pages.py against the sample documents. Expected values come from samples/facts.yaml."""

from __future__ import annotations

import re
import zipfile

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from docvault import pages
from docvault.domain import DocKind, ErrorCode, ExtractedText, Failure, Page, PageBasis, VaultError

from .conftest import ENDORSEMENT, FACTS, POLICY, QUOTE, SAMPLES, TRUTH


def extracted(name: str) -> ExtractedText:
    path = SAMPLES / name
    return pages.extract(pages.sniff_kind(path), path, max_pages=2000)


def chunks_of(name: str):
    return pages.chunk(extracted(name), max_chars=1800, overlap_chars=250)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def claims():
    for fact in TRUTH["facts"]:
        yield fact["id"], fact["document"], fact["page"], fact["text"]
        for page in fact.get("repeated_on", []):
            yield fact["id"], fact["document"], page, fact["text"]
        if "continues" in fact:
            yield fact["id"], fact["document"], fact["continues"]["page"], fact["continues"]["text"]


def test_samples_extract_to_the_recorded_pages():
    shapes = {name: (doc.basis.value, len(doc.pages)) for name in TRUTH["documents"] if (doc := extracted(name))}
    assert shapes == {POLICY: ("physical", 7), QUOTE: ("physical", 3), ENDORSEMENT: ("synthetic", 2)}
    assert shapes == {name: (meta["page_basis"], meta["pages"]) for name, meta in TRUTH["documents"].items()}


@pytest.mark.parametrize(("fact_id", "document", "page", "text"), list(claims()))
def test_every_planted_fact_is_on_its_page_word_for_word(fact_id, document, page, text):
    page_text = extracted(document).pages[page - 1].text
    span = pages.locate_quote(text, page_text)
    assert span is not None, f"{fact_id} not found on page {page}"
    assert squash(page_text[span[0]:span[1]]) == text


def test_every_page_is_marked_as_a_sample():
    for name in TRUTH["documents"]:
        assert all("SAMPLE - fictitious" in page.text for page in extracted(name).pages), name


@pytest.mark.parametrize("name", [POLICY, QUOTE, ENDORSEMENT])
def test_chunks_are_exact_slices_with_a_correct_page_for_every_character(name):
    doc = extracted(name)
    stream, _ = pages.page_stream(doc)
    page_of_char: list[int] = []
    for page in doc.pages:
        page_of_char += [page.number] * (len(page.text) + len(pages.PAGE_JOINER))
    covered = [False] * len(stream)

    for chunk in pages.chunk(doc, max_chars=1800, overlap_chars=250):
        assert chunk.text == stream[chunk.start:chunk.end]
        assert len(chunk.text) <= 1800
        assert [chunk.page_map.page_at(i) for i in range(len(chunk.text))] == page_of_char[chunk.start:chunk.end]
        covered[chunk.start:chunk.end] = [True] * len(chunk.text)

    assert [ch for ch, seen in zip(stream, covered, strict=True) if not seen and not ch.isspace()] == []


def test_a_clause_that_crosses_a_page_break_cites_each_half_on_its_own_page():
    clause = FACTS["policy-defence-clause"]
    [chunk] = [c for c in chunks_of(POLICY)
               if pages.locate_quote(clause["text"], c.text) and pages.locate_quote(clause["continues"]["text"], c.text)]
    first_half = pages.locate_quote(clause["text"], chunk.text)
    second_half = pages.locate_quote(clause["continues"]["text"], chunk.text)

    assert [page for _, page in chunk.page_map.starts] == [3, 4, 5]
    assert chunk.page_map.page_at(first_half[0]) == 4
    assert chunk.page_map.page_at(second_half[0]) == 5
    assert chunk.text[second_half[0]:second_half[1]] == "property damage to which this policy does not apply"


def test_repeated_boilerplate_resolves_to_the_page_of_the_chunk_it_was_found_in():
    footer = FACTS["policy-footer-boilerplate"]["text"]
    stream, starts = pages.page_stream(extracted(POLICY))
    cited = [chunk.page_map.page_at(span[0]) for chunk in chunks_of(POLICY)
             if (span := pages.locate_quote(footer, chunk.text))]

    assert cited == [2, 3, 5, 6, 7]
    # A search of the whole document finds only the page 2 copy, whichever chunk asks.
    assert starts[1][1] == 2 and starts[1][0] < stream.find(footer) < starts[2][0]


def test_same_limit_wording_in_two_documents_keeps_its_own_page():
    text = FACTS["policy-each-occurrence-limit"]["text"]
    assert text == FACTS["quote-each-occurrence-limit"]["text"] == "Each Occurrence Limit $1,000,000"
    for name in (POLICY, QUOTE):
        [chunk] = [c for c in chunks_of(name) if pages.locate_quote(text, c.text)][:1]
        start, end = pages.locate_quote(text, chunk.text)
        assert chunk.page_map.page_at(start) == 2
        assert chunk.text[start:end] == "Each Occurrence Limit\n$1,000,000"


def test_locate_quote_ignores_case_whitespace_and_typographic_quotes_and_dashes():
    source = 'The "Each  Occurrence\nLimit" is $1,000,000 - per the Declarations.'
    assert pages.locate_quote("the “each occurrence limit” is $1,000,000 – per", source) == (0, 48)
    assert source[0:48] == 'The "Each  Occurrence\nLimit" is $1,000,000 - per'
    assert pages.locate_quote("each occurrence limit is $2,000,000", source) is None
    assert pages.locate_quote("  ", source) is None


def test_scanned_style_pdf_fails_with_a_reason(tmp_path):
    path = tmp_path / "scan.pdf"
    canvas = Canvas(str(path), pagesize=letter)
    for _ in range(3):
        canvas.rect(72, 72, 400, 600, fill=1)
        canvas.showPage()
    canvas.save()

    with pytest.raises(VaultError) as raised:
        pages.extract(pages.sniff_kind(path), path, max_pages=2000)
    assert raised.value.failure.code is ErrorCode.NO_EXTRACTABLE_TEXT
    assert raised.value.failure.message.startswith("None of the 3 pages has a text layer.")
    assert raised.value.failure.retryable is False


def test_a_blank_page_keeps_its_number_and_is_never_in_a_page_map(tmp_path):
    path = tmp_path / "gap.pdf"
    canvas = Canvas(str(path), pagesize=letter)
    canvas.drawString(72, 700, "First page text.")
    canvas.showPage()
    canvas.rect(72, 72, 100, 100)
    canvas.showPage()
    canvas.drawString(72, 700, "Third page text.")
    canvas.showPage()
    canvas.save()

    doc = pages.extract(DocKind.PDF, path, max_pages=10)
    assert [(p.number, p.text) for p in doc.pages] == [(1, "First page text."), (2, ""), (3, "Third page text.")]
    [chunk] = pages.chunk(doc, max_chars=1800, overlap_chars=250)
    assert chunk.page_map.starts == ((0, 1), (18, 3))


def test_other_pdf_failures_are_vault_errors(tmp_path):
    locked = tmp_path / "locked.pdf"
    canvas = Canvas(str(locked), pagesize=letter, encrypt="owner-and-user-secret")
    canvas.drawString(72, 700, "Hidden.")
    canvas.save()
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\nnot really a pdf")

    codes = {}
    for path, limit in [(locked, 10), (broken, 10), (SAMPLES / POLICY, 6)]:
        with pytest.raises(VaultError) as raised:
            pages.extract(DocKind.PDF, path, max_pages=limit)
        codes[path.name] = raised.value.failure.code.value
    assert codes == {"locked.pdf": "encrypted_pdf", "broken.pdf": "unreadable_file", POLICY: "too_many_pages"}


def test_kind_comes_from_the_bytes_not_the_name(tmp_path):
    disguised = tmp_path / "notes.txt"
    disguised.write_bytes((SAMPLES / POLICY).read_bytes())
    text = tmp_path / "report.pdf"
    text.write_text("Plain text with a café in it.\n", encoding="utf-8")
    latin = tmp_path / "latin.txt"
    latin.write_bytes("Limit: \xa3500".encode("cp1252"))
    assert [pages.sniff_kind(p) for p in (disguised, text, latin, SAMPLES / ENDORSEMENT)] == [
        DocKind.PDF, DocKind.TXT, DocKind.TXT, DocKind.DOCX]

    png = tmp_path / "scan.pdf"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
    sheet = tmp_path / "sheet.docx"
    with zipfile.ZipFile(sheet, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    messages = {}
    for path in (png, sheet):
        with pytest.raises(VaultError) as raised:
            pages.sniff_kind(path)
        assert raised.value.failure.code is ErrorCode.UNSUPPORTED_TYPE
        messages[path.name] = raised.value.failure.message.split(".")[0]
    assert messages == {"scan.pdf": "This looks like a PNG image",
                        "sheet.docx": "This looks like a zip archive that is not a Word document"}


def test_text_file_becomes_sections_of_at_most_3000_characters(tmp_path):
    paragraphs = [f"Paragraph {n}. " + "word " * 180 for n in range(1, 8)]
    path = tmp_path / "notes.txt"
    path.write_text("\r\n\r\n".join(paragraphs), encoding="utf-8")

    doc = pages.extract(DocKind.TXT, path, max_pages=10)
    assert doc.basis is PageBasis.SYNTHETIC
    assert [p.number for p in doc.pages] == [1, 2, 3]
    assert [p.text[:12] for p in doc.pages] == ["Paragraph 1.", "Paragraph 4.", "Paragraph 7."]
    assert max(len(p.text) for p in doc.pages) <= 3000
    assert pages.page_label(doc.basis, 2) == "section 2"


def test_text_with_no_breaks_at_all_is_still_cut_to_size():
    doc = ExtractedText(PageBasis.SYNTHETIC, (Page(1, "x" * 4000),))
    assert [(c.start, c.end) for c in pages.chunk(doc, max_chars=1800, overlap_chars=250)] == [
        (0, 1800), (1800, 3600), (3600, 4000)]


def test_a_docx_whose_text_would_inflate_past_the_bound_is_refused_before_it_is_parsed(tmp_path, monkeypatch):
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(SAMPLES / ENDORSEMENT) as sample, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bomb:
        for item in sample.infolist():
            data = sample.read(item)
            bomb.writestr(item, data + b" " * 2_000_000 if item.filename == "word/document.xml" else data)
    monkeypatch.setattr(pages, "MAX_DOCX_XML_BYTES", 1_000_000, raising=False)

    with pytest.raises(VaultError) as raised:
        pages.extract(DocKind.DOCX, path, max_pages=100)

    assert raised.value.failure == Failure(ErrorCode.UNREADABLE_FILE,
                                           "The Word file's text would expand to more than 1 MB. Split the document "
                                           "and upload the parts.", retryable=False)
