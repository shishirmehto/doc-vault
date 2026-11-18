"""pages.py against the sample documents. Expected values come from samples/facts.yaml."""

from __future__ import annotations

import re
import time
import zipfile

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from docvault import pages
from docvault.domain import (
    DocKind,
    ErrorCode,
    ExtractedText,
    Failure,
    OutlineEntry,
    Page,
    PageBasis,
    PageMap,
    VaultError,
)

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


def test_the_part_of_a_chunk_on_some_pages_is_a_substring_with_its_page_map_counted_from_its_start():
    text = "End of five.\n\nAll of six.\n\nStart of seven."
    page_map = PageMap(((0, 5), (14, 6), (27, 7)))

    assert pages.part_on_pages(text, page_map, 7, 7) == ("Start of seven.", PageMap(((0, 7),)))
    assert pages.part_on_pages(text, page_map, 6, 9) == ("All of six.\n\nStart of seven.", PageMap(((0, 6), (13, 7))))
    assert pages.part_on_pages(text, page_map, 4, 5) == ("End of five.", PageMap(((0, 5),)))
    assert pages.part_on_pages(text, PageMap(((0, 5), (27, 7))), 6, 6) is None
    assert pages.part_on_pages(text, page_map, 8, 9) is None


def test_the_word_samples_headings_are_its_outline_and_the_pdf_samples_have_none():
    assert {name: extracted(name).outline for name in TRUTH["documents"]} == {
        POLICY: (), QUOTE: (),
        ENDORSEMENT: (OutlineEntry("ENDORSEMENT SM 40 03", 0, 1),
                      OutlineEntry("ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS", 1, 1))}


def bookmarked_pdf(tmp_path, pages_of_lines, bookmarks):
    """bookmarks: (title, page index or None for no destination, index of the parent bookmark or None)."""
    writer = PdfWriter(clone_from=PdfReader(text_pdf(tmp_path / "plain.pdf", *pages_of_lines)))
    made = []
    for title, page, parent in bookmarks:
        made.append(writer.add_outline_item(title, page, parent=None if parent is None else made[parent]))
    path = tmp_path / "bookmarked.pdf"
    writer.write(path)
    return path


def test_a_pdf_outline_gives_each_bookmark_its_title_depth_and_page_and_skips_one_that_goes_nowhere(tmp_path):
    path = bookmarked_pdf(tmp_path, [["Contents"], ["Part 9"], ["903 High-Risk Land"]], [
        ("  Part 9:  Endorsements and\nOptions ", 1, None), ("903 High-Risk Land Exclusion Option", 2, 0),
        ("Ghost", None, None), ("Exhibits", 2, None)])

    assert pages.extract(DocKind.PDF, path, max_pages=10).outline == (
        OutlineEntry("Part 9: Endorsements and Options", 0, 2), OutlineEntry("903 High-Risk Land Exclusion Option", 1, 3),
        OutlineEntry("Exhibits", 0, 3))


def test_a_pdf_outline_keeps_only_its_first_entries_up_to_the_bound(tmp_path, monkeypatch):
    path = bookmarked_pdf(tmp_path, [["Contents"], ["Part 9"]], [
        ("Ghost", None, None), ("Contents", 0, None), ("Part 9", 1, None), ("903", 1, 2), ("Exhibits", 1, None)])
    monkeypatch.setattr(pages, "MAX_OUTLINE_ENTRIES", 2, raising=False)

    assert pages.extract(DocKind.PDF, path, max_pages=10).outline == (
        OutlineEntry("Contents", 0, 1), OutlineEntry("Part 9", 0, 2))


def test_word_headings_past_the_bound_are_left_out_of_the_outline(tmp_path, monkeypatch):
    from docx import Document

    word = Document()
    for title in ("Coverage", "Exclusions", "Conditions"):
        word.add_heading(title, 1)
        word.add_paragraph(f"{title} text.")
    path = tmp_path / "policy.docx"
    word.save(str(path))
    monkeypatch.setattr(pages, "MAX_OUTLINE_ENTRIES", 2, raising=False)

    assert pages.extract(DocKind.DOCX, path, max_pages=10).outline == (
        OutlineEntry("Coverage", 0, 1), OutlineEntry("Exclusions", 0, 1))


def test_a_pdf_whose_outline_cannot_be_read_keeps_its_text_and_has_no_outline(tmp_path, monkeypatch):
    path = bookmarked_pdf(tmp_path, [["Contents"], ["Part 9"]], [("Part 9", 1, None)])
    monkeypatch.setattr(PdfReader, "outline", property(lambda reader: 1 / 0))

    doc = pages.extract(DocKind.PDF, path, max_pages=10)

    assert ([page.text for page in doc.pages], doc.outline) == (["Contents", "Part 9"], ())


def test_word_headings_are_the_outline_each_on_the_section_its_text_lands_in(tmp_path):
    from docx import Document

    word = Document()
    word.add_heading("Commercial Lines Manual", 0)
    word.add_heading("Part 1  General Rules", 1)
    word.add_paragraph("x" * 2960)
    word.add_heading("Rule 5 Premium Determination", 2)
    word.add_heading("", 1)
    word.add_paragraph("Premium is determined per location.")
    path = tmp_path / "manual.docx"
    word.save(str(path))

    doc = pages.extract(DocKind.DOCX, path, max_pages=10)

    assert [page.text[:30] for page in doc.pages] == [
        "Commercial Lines Manual\nPart 1", "x" * 30, "Premium is determined per loca"]
    assert doc.outline == (OutlineEntry("Commercial Lines Manual", 0, 1), OutlineEntry("Part 1 General Rules", 0, 1),
                           OutlineEntry("Rule 5 Premium Determination", 1, 2))


def test_a_title_is_found_whatever_its_case_spacing_and_typographic_dashes():
    text = "902 Catastrophic Risk Protection (Continued)\n903 H igh -Risk Land Exclusion Option\nA. General Information"

    assert [pages.has_title(title, text) for title in (
        "903 High-Risk Land Exclusion Option", "903 high–risk land exclusion option", "a. general information",
        "904 Hail and Fire Exclusion Option", "")] == [True, True, True, False, False]


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


def text_pdf(path, *pages_of_lines: list[str]):
    canvas = Canvas(str(path), pagesize=letter)
    for lines in pages_of_lines:
        for row, line in enumerate(lines):
            canvas.drawString(72, 700 - 20 * row, line)
        canvas.showPage()
    canvas.save()
    return path


def aes_pdf(tmp_path, user_password: str):
    plain = text_pdf(tmp_path / "plain.pdf", ["Each Occurrence Limit: $1,000,000"], ["Second page text."])
    writer = PdfWriter(clone_from=PdfReader(plain))
    writer.encrypt(user_password=user_password, owner_password="owner-secret", algorithm="AES-256")
    path = tmp_path / "aes.pdf"
    writer.write(path)
    return path


def test_an_aes_pdf_that_opens_without_a_password_is_read(tmp_path):
    doc = pages.extract(DocKind.PDF, aes_pdf(tmp_path, user_password=""), max_pages=10)
    assert [(p.number, p.text) for p in doc.pages] == [(1, "Each Occurrence Limit: $1,000,000"), (2, "Second page text.")]


def test_an_aes_pdf_that_needs_a_password_to_open_is_refused_as_encrypted(tmp_path):
    with pytest.raises(VaultError) as raised:
        pages.extract(DocKind.PDF, aes_pdf(tmp_path, user_password="reader-secret"), max_pages=10)
    assert raised.value.failure.code is ErrorCode.ENCRYPTED_PDF


def test_fake_bold_in_a_pdf_is_read_once_and_amounts_keep_their_digits(tmp_path):
    lines = ["PPoolliiccyy NNuummbbeerr:: SAMPLE-0001", "11sstt Named Insured", "Committee balloon 1100 $5500 Page 22 of 22",
             "Loan No. XXXX-XXXX Year YYYY Code XXXX AA))"]
    pdf = pages.extract(DocKind.PDF, text_pdf(tmp_path / "bold.pdf", lines), max_pages=10)
    assert pdf.pages[0].text == ("Policy Number: SAMPLE-0001\n1st Named Insured\nCommittee balloon 1100 $5500 Page 22 of 22\n"
                                 "Loan No. XXXX-XXXX Year YYYY Code XXXX A)")

    txt = tmp_path / "bold.txt"
    txt.write_text("\n".join(lines), encoding="utf-8")
    assert pages.extract(DocKind.TXT, txt, max_pages=10).pages[0].text == "\n".join(lines)


def test_a_word_split_at_a_line_end_joins_when_the_document_uses_it_whole_and_never_across_pages(tmp_path):
    path = text_pdf(
        tmp_path / "columns.pdf",
        ["We rely on the infor-", "mation you gave us, on the single-", "family dwelling and on the Policy-",
         "Holder named above. Give us 30-", "day notice. See the defini-"],
        ["tions of these words: information, definitions."],
    )
    doc = pages.extract(DocKind.PDF, path, max_pages=10)
    assert [p.text for p in doc.pages] == [
        "We rely on the information you gave us, on the single-family dwelling and on the Policy-\nHolder named above. "
        "Give us 30-day notice. See the defini-",
        "tions of these words: information, definitions.",
    ]


def test_a_word_with_a_stray_space_before_its_line_end_hyphen_joins_when_the_document_uses_it_whole(tmp_path):
    path = text_pdf(
        tmp_path / "spaced.pdf",
        ["Pollution means the di s-", "charge of fuels, l u-", "bricants or a re-", "port, and a non-",
         "profit body. Words used whole: discharge, lubricants, report."],
    )
    assert pages.extract(DocKind.PDF, path, max_pages=10).pages[0].text == (
        "Pollution means the discharge of fuels, lubricants or a report, and a non-profit body. "
        "Words used whole: discharge, lubricants, report.")


def test_a_page_of_one_long_unbroken_word_is_read_in_linear_time(tmp_path):
    word = "a" * 40_000
    path = text_pdf(tmp_path / "long.pdf", [f"{word} end", "a re-", "port, one report"])
    started = time.perf_counter()
    doc = pages.extract(DocKind.PDF, path, max_pages=10)
    assert time.perf_counter() - started < 2
    assert doc.pages[0].text == f"{word} end\na report, one report"


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


def test_a_pdf_header_after_leading_bytes_utf16_text_and_rtf_are_told_apart(tmp_path):
    late = tmp_path / "late-header.pdf"
    late.write_bytes(b"\r\n" + (SAMPLES / POLICY).read_bytes())
    wide = tmp_path / "notepad-unicode.txt"
    wide.write_bytes("Premium: €1,200 for the café.".encode("utf-16"))
    rtf = tmp_path / "letter.txt"
    rtf.write_bytes(rb"{\rtf1\ansi Dear client,\par}")

    assert [pages.sniff_kind(p) for p in (late, wide)] == [DocKind.PDF, DocKind.TXT]
    assert pages.extract(DocKind.PDF, late, max_pages=10).pages[0].text.startswith("SAMPLE")
    assert pages.extract(DocKind.TXT, wide, max_pages=10).pages[0].text == "Premium: €1,200 for the café."
    with pytest.raises(VaultError) as raised:
        pages.sniff_kind(rtf)
    assert raised.value.failure.message.split(".")[0] == "This looks like an RTF document"


def test_utf8_text_with_one_stray_byte_keeps_its_utf8_characters(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes("Prämie: 1.000 € ".encode() + b"\xff" + " Selbstbehalt: 250 €".encode())
    assert pages.extract(pages.sniff_kind(path), path, max_pages=10).pages[0].text == (
        "Prämie: 1.000 € � Selbstbehalt: 250 €")



def test_cp1252_text_with_an_accent_before_a_curly_quote_reads_as_cp1252(tmp_path):
    path = tmp_path / "avenant.txt"
    path.write_bytes("Le «contrat» dit “Résilié”. Prime réglée en été.".encode("cp1252"))
    assert pages.extract(pages.sniff_kind(path), path, max_pages=10).pages[0].text == (
        "Le «contrat» dit “Résilié”. Prime réglée en été.")


def test_a_text_file_that_mentions_the_pdf_header_is_text(tmp_path):
    path = tmp_path / "formats.txt"
    path.write_text("Notes on file formats.\nA file without a %PDF-1.4 header is not a PDF.\n")
    assert pages.sniff_kind(path) is DocKind.TXT


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


@pytest.mark.parametrize("inflated_part", ["word/document.xml", "word/styles.xml"])
def test_a_docx_that_would_inflate_past_the_bound_is_refused_before_it_is_parsed(tmp_path, monkeypatch, inflated_part):
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(SAMPLES / ENDORSEMENT) as sample, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bomb:
        for item in sample.infolist():
            data = sample.read(item)
            bomb.writestr(item, data + b" " * 2_000_000 if item.filename == inflated_part else data)
    monkeypatch.setattr(pages, "MAX_DOCX_INFLATED_BYTES", 1_000_000, raising=False)

    with pytest.raises(VaultError) as raised:
        pages.extract(DocKind.DOCX, path, max_pages=100)

    assert raised.value.failure == Failure(ErrorCode.UNREADABLE_FILE,
                                           "The Word file would expand to more than 1 MB. Split the document "
                                           "and upload the parts.", retryable=False)


def test_a_docx_table_reads_a_merged_cell_once_and_keeps_a_table_nested_in_a_cell(tmp_path):
    from docx import Document

    word = Document()
    word.add_paragraph("Schedule")
    table = word.add_table(rows=2, cols=3)
    merged = table.cell(0, 0).merge(table.cell(0, 1))
    merged.text, table.cell(0, 2).text = "General Aggregate", "$2,000,000"
    for col, text in enumerate(["Deductible", "per claim", "$2,500"]):
        table.cell(1, col).text = text
    nested = table.cell(1, 1).add_table(rows=1, cols=2)
    nested.cell(0, 0).text, nested.cell(0, 1).text = "Waiting period", "30 days"
    path = tmp_path / "schedule.docx"
    word.save(str(path))

    assert pages.extract(DocKind.DOCX, path, max_pages=10).pages[0].text == (
        "Schedule\nGeneral Aggregate | $2,000,000\nDeductible | per claim | $2,500\nWaiting period | 30 days")


def test_a_docx_with_no_default_paragraph_style_is_read(tmp_path):
    from docx import Document

    word = Document()
    word.add_heading("Coverage", level=1)
    word.add_paragraph("Each Occurrence Limit: $1,000,000")
    made = tmp_path / "made.docx"
    word.save(str(made))
    path = tmp_path / "no-default-style.docx"
    with zipfile.ZipFile(made) as source, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item)
            target.writestr(item, data.replace(b' w:default="1"', b"") if item.filename == "word/styles.xml" else data)

    doc = pages.extract(DocKind.DOCX, path, max_pages=10)
    assert ([p.text for p in doc.pages], [(e.title, e.level) for e in doc.outline]) == (
        ["Coverage\nEach Occurrence Limit: $1,000,000"], [("Coverage", 0)])


def test_a_table_nested_in_a_vertically_merged_cell_is_read_once(tmp_path):
    from docx import Document

    word = Document()
    table = word.add_table(rows=3, cols=2)
    coverage = table.cell(0, 0).merge(table.cell(2, 0))
    coverage.text = "Coverage A"
    nested = coverage.add_table(rows=1, cols=2)
    nested.cell(0, 0).text, nested.cell(0, 1).text = "Sublimit", "$50,000"
    for row, limit in enumerate(["$1,000,000", "$2,000,000", "$3,000,000"]):
        table.cell(row, 1).text = limit
    path = tmp_path / "coverage.docx"
    word.save(str(path))

    assert pages.extract(DocKind.DOCX, path, max_pages=10).pages[0].text == (
        "Coverage A | $1,000,000\nSublimit | $50,000\nCoverage A | $2,000,000\nCoverage A | $3,000,000")


def test_a_text_file_over_the_limit_is_measured_in_characters_not_pages(tmp_path):
    path = tmp_path / "long.txt"
    path.write_text("\n\n".join("Paragraph. " + "word " * 500 for _ in range(4)))
    with pytest.raises(VaultError) as raised:
        pages.extract(DocKind.TXT, path, max_pages=3)
    assert raised.value.failure == Failure(
        ErrorCode.TOO_MANY_PAGES, "The text is 10,040 characters long, more than one file can hold (about 9,000). "
        "Split it and upload the parts.", retryable=False)


def test_a_text_file_under_the_character_limit_is_kept_however_its_paragraphs_fall_into_sections(tmp_path):
    path = tmp_path / "short-paragraphs.txt"
    path.write_text("\n\n".join("x" * 1600 for _ in range(4)))
    doc = pages.extract(DocKind.TXT, path, max_pages=3)
    assert [len(page.text) for page in doc.pages] == [1600, 1600, 1600, 1600]
