"""Document text and page provenance. Pure functions, no database, no network.

This module owns every rule about where text came from: format sniffing,
per-format extraction into numbered pages, chunking that remembers page
boundaries, and locating a quote back to an exact page. The pipeline, the
insights step and chat all ask this module; none of them does offset math.
"""

from __future__ import annotations

import re
import zipfile
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from pathlib import Path

from docx import Document as open_docx
from docx.table import Table
from pypdf import PdfReader

from .domain import ChunkDraft, DocKind, ErrorCode, ExtractedText, Page, PageBasis, PageMap, VaultError

PAGE_JOINER = "\n\n"   # placed between non-empty pages in the document text stream
TABLE_CELL = " | "     # joins the cells of a DOCX table row on one line
SYNTHETIC_PAGE_CHARS = 3000
SNIFF_BYTES = 8192
MAX_DOCX_XML_BYTES = 200_000_000   # word/document.xml inflated; a zip bomb compresses far past this

_KNOWN_UNSUPPORTED = {
    b"\xd0\xcf\x11\xe0": "a legacy Office file (.doc, .xls or .ppt)",
    b"\x89PNG": "a PNG image",
    b"\xff\xd8\xff": "a JPEG image",
    b"GIF8": "a GIF image",
}
_TEXT_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f]")


def sniff_kind(path: Path) -> DocKind:
    """Decide the format from the stored bytes, not the extension or Content-Type.

    b"%PDF-" -> PDF. A zip that contains word/document.xml -> DOCX. Text that
    decodes as UTF-8 or cp1252 with no control characters -> TXT. Anything
    else raises VaultError(UNSUPPORTED_TYPE) naming what was detected.
    """
    with path.open("rb") as f:
        head = f.read(SNIFF_BYTES)
    if head.startswith(b"%PDF-"):
        return DocKind.PDF
    if head.startswith(b"PK\x03\x04"):
        if _is_docx(path):
            return DocKind.DOCX
        raise _unsupported("a zip archive that is not a Word document")
    for magic, name in _KNOWN_UNSUPPORTED.items():
        if head.startswith(magic):
            raise _unsupported(name)
    if not _TEXT_CONTROL_CHARS.search(_decode(head)):
        return DocKind.TXT
    raise _unsupported("a binary file of an unknown type")


def _is_docx(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return "word/document.xml" in archive.namelist()
    except zipfile.BadZipFile:
        return False


def _unsupported(detected: str) -> VaultError:
    return VaultError(ErrorCode.UNSUPPORTED_TYPE,
                      f"This looks like {detected}. Upload a PDF, a Word .docx file or a plain text file.")


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def extract(kind: DocKind, path: Path, *, max_pages: int) -> ExtractedText:
    """Read the stored file into numbered pages. Dispatches through _EXTRACTORS.

    Raises VaultError, never a library exception:
      ENCRYPTED_PDF (decrypt with "" fails), UNREADABLE_FILE (parser error),
      TOO_MANY_PAGES (> max_pages), NO_EXTRACTABLE_TEXT (every page empty after
      strip; a scanned PDF). Pages with some text and pages without are both
      kept, so page numbers never shift.
    """
    try:
        doc = _EXTRACTORS[kind](path, max_pages)
    except VaultError:
        raise
    except Exception as exc:
        raise VaultError(ErrorCode.UNREADABLE_FILE,
                         f"The file could not be read as {kind.value.upper()}. It may be damaged.") from exc
    _check_page_count(len(doc.pages), max_pages)
    if not any(page.text for page in doc.pages):
        if kind is DocKind.PDF:
            raise VaultError(ErrorCode.NO_EXTRACTABLE_TEXT,
                             f"None of the {len(doc.pages)} pages has a text layer. This looks like a scanned "
                             "PDF, and OCR is not supported.")
        raise VaultError(ErrorCode.NO_EXTRACTABLE_TEXT, "The file contains no text.")
    return doc


def _check_page_count(count: int, max_pages: int) -> None:
    if count > max_pages:
        raise VaultError(ErrorCode.TOO_MANY_PAGES, f"The document has {count} pages. The limit is {max_pages}.")


def _extract_pdf(path: Path, max_pages: int) -> ExtractedText:
    """pypdf, one Page per physical page, basis PHYSICAL."""
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        raise VaultError(ErrorCode.ENCRYPTED_PDF,
                         "This PDF is password-protected. Remove the password and upload it again.")
    _check_page_count(len(reader.pages), max_pages)   # before extraction, which is the slow part
    texts = _join_line_end_hyphens([_undouble(_tidy(page.extract_text())) for page in reader.pages])
    return ExtractedText(PageBasis.PHYSICAL, tuple(Page(number, text) for number, text in enumerate(texts, start=1)))


_TOKEN = re.compile(r"\S{4,}")


def _undouble(text: str) -> str:
    """Some PDFs draw bold by printing each glyph twice, and pypdf then reads "PPoolliiccyy" for "Policy"."""
    return _TOKEN.sub(_undouble_token, text)


def _undouble_token(match: re.Match[str]) -> str:
    token = match.group()
    return token[::2] if _printed_twice(token) else token


def _printed_twice(token: str) -> bool:
    letters = sum(ch.isalpha() for ch in token)
    # One repeated character ("XXXX", "YYYY") is a template placeholder, not a glyph printed twice.
    return len(token) % 2 == 0 and token[::2] == token[1::2] and letters >= 2 and len(set(token)) >= 2


_WORD = re.compile(r"[^\W\d_]+")
_LINE_END_HYPHEN = re.compile(r"([^\W_]+)-\n([^\W\d_]+)")


def _join_line_end_hyphens(page_texts: list[str]) -> list[str]:
    letter_words = {word.lower() for text in page_texts for word in _WORD.findall(text)}

    def unwrap(match: re.Match[str]) -> str:
        head, tail = match.groups()
        if not tail[0].islower():
            return match.group()
        return head + tail if (head + tail).lower() in letter_words else f"{head}-{tail}"

    return [_LINE_END_HYPHEN.sub(unwrap, text) for text in page_texts]


def _tidy(text: str) -> str:
    """Drop NUL (Postgres text refuses it), collapse runs of spaces and drop blank edges. Newlines stay, so
    sentence and paragraph ends survive."""
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.replace("\x00", "").splitlines())
    return "\n".join(lines).strip()


def _extract_docx(path: Path, max_pages: int) -> ExtractedText:
    """python-docx, body paragraphs and tables in document order, cut into
    SYNTHETIC sections of ~SYNTHETIC_PAGE_CHARS at paragraph boundaries.
    Tables become one line per row, cells joined by " | ".
    """
    with zipfile.ZipFile(path) as archive:
        inflated = archive.getinfo("word/document.xml").file_size   # zipfile reads no more than this
    if inflated > MAX_DOCX_XML_BYTES:
        raise VaultError(ErrorCode.UNREADABLE_FILE,
                         f"The Word file's text would expand to more than {MAX_DOCX_XML_BYTES // 1_000_000} MB. "
                         "Split the document and upload the parts.")
    paragraphs: list[str] = []
    for item in open_docx(str(path)).iter_inner_content():
        if isinstance(item, Table):
            paragraphs += [TABLE_CELL.join(_tidy(cell.text) for cell in row.cells) for row in item.rows]
        else:
            paragraphs.append(_tidy(item.text))
    return _sections(paragraphs, joiner="\n")


def _extract_txt(path: Path, max_pages: int) -> ExtractedText:
    """UTF-8 (with or without BOM) or cp1252 text, cut into SYNTHETIC sections."""
    text = _decode(path.read_bytes()).replace("\r\n", "\n").replace("\r", "\n")
    return _sections([_tidy(p) for p in re.split(r"\n[ \t]*\n", text)], joiner="\n\n")


def _sections(paragraphs: Sequence[str], *, joiner: str) -> ExtractedText:
    """Pack paragraphs into sections of at most SYNTHETIC_PAGE_CHARS.

    A paragraph longer than a section is first split at line ends, then at
    spaces, so a file with no blank lines still gets sections of bounded size.
    """
    pieces = [piece for p in paragraphs if p for piece in _split_long(p, SYNTHETIC_PAGE_CHARS)]
    sections: list[list[str]] = [[]]
    size = 0
    for piece in pieces:
        if sections[-1] and size + len(joiner) + len(piece) > SYNTHETIC_PAGE_CHARS:
            sections.append([])
            size = 0
        size += len(piece) + (len(joiner) if sections[-1] else 0)
        sections[-1].append(piece)
    return ExtractedText(PageBasis.SYNTHETIC, tuple(
        Page(number, joiner.join(parts)) for number, parts in enumerate(sections, start=1)))


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    bounds = _cut_points(text, limit, [m.end() for m in re.finditer(r"\n+", text)])
    return [piece for a, b in zip(bounds, bounds[1:], strict=False) if (piece := text[a:b].strip())]


_EXTRACTORS: dict[DocKind, Callable[[Path, int], ExtractedText]] = {
    DocKind.PDF: _extract_pdf,
    DocKind.DOCX: _extract_docx,
    DocKind.TXT: _extract_txt,
}

# Two word characters before the full stop, so list labels such as "a." and "2." stay with their text.
SENTENCE_OR_PARAGRAPH_END = re.compile(r"""(?<=\w\w[.!?])\s+|(?<=\w[.!?]["')])\s+|\n\s*\n\s*""")
_WHITESPACE = re.compile(r"\s+")


def page_stream(doc: ExtractedText) -> tuple[str, list[tuple[int, int]]]:
    """The document as one string, and (offset, page_number) for where each
    non-empty page starts in it. Chunk offsets refer to this string."""
    parts: list[str] = []
    starts: list[tuple[int, int]] = []
    offset = 0
    for page in doc.pages:
        if not page.text:
            continue
        starts.append((offset, page.number))
        parts.append(page.text)
        offset += len(page.text) + len(PAGE_JOINER)
    return PAGE_JOINER.join(parts), starts


def chunk(doc: ExtractedText, *, max_chars: int, overlap_chars: int) -> list[ChunkDraft]:
    """Cut the document into overlapping chunks that may cross page boundaries.

    Invariants (each has a test):
      - chunk.text == stream[chunk.start:chunk.end] for the stream from
        page_stream(). Offsets are produced by the cut itself, never recovered
        with str.find, so boilerplate repeated on many pages (common in
        policies) cannot be mislocated.
      - every non-whitespace character of the stream is in at least one chunk.
      - chunk.page_map.page_at(i) is the page that character i of chunk.text came from.
      - len(chunk.text) <= max_chars.

    A chunk ends at the last sentence or paragraph end that fits. The next
    chunk starts at the first sentence start inside the last overlap_chars of
    this one, or at a word start there when no sentence starts that late, so
    a clause cut by a chunk end is whole in one of the two chunks whenever it
    is shorter than the overlap.
    """
    stream, page_starts = page_stream(doc)
    bounds = _cut_points(stream, max_chars, [m.end() for m in SENTENCE_OR_PARAGRAPH_END.finditer(stream)])
    page_offsets = [offset for offset, _ in page_starts]
    drafts: list[ChunkDraft] = []
    start = previous_end = 0
    while start < len(stream):
        end = bounds[bisect_right(bounds, start + max_chars) - 1]
        if end <= previous_end:
            start = previous_end   # the next sentence does not fit after the overlap, so drop the overlap
            continue
        raw = stream[start:end]
        text = raw.strip()
        if text:
            at = start + len(raw) - len(raw.lstrip())
            first = bisect_right(page_offsets, at) - 1
            last = bisect_right(page_offsets, at + len(text) - 1) - 1
            page_map = PageMap(tuple((max(offset - at, 0), number) for offset, number in page_starts[first:last + 1]))
            drafts.append(ChunkDraft(len(drafts), at, at + len(text), text, page_map))
        previous_end = end
        start = _next_start(stream, bounds, start, end, overlap_chars)
    return drafts


def _next_start(stream: str, bounds: Sequence[int], start: int, end: int, overlap_chars: int) -> int:
    earliest = max(end - overlap_chars, start + 1)
    sentence_start = bounds[bisect_left(bounds, earliest)]
    if sentence_start < end:
        return sentence_start
    word_gap = _WHITESPACE.search(stream, earliest, end)
    return word_gap.end() if word_gap and word_gap.end() < end else end


def _cut_points(text: str, limit: int, preferred: Sequence[int]) -> list[int]:
    """Sorted offsets, from 0 to len(text), no two further apart than `limit`.

    Uses the preferred offsets, and where two of them are too far apart adds
    cuts after whitespace, or mid-token when there is no whitespace at all.
    """
    points = [0]
    for target in [*sorted(p for p in preferred if 0 < p < len(text)), len(text)]:
        while target - points[-1] > limit:
            window_end = points[-1] + limit
            gaps = [m.end() for m in _WHITESPACE.finditer(text, points[-1] + 1, window_end)]
            points.append(gaps[-1] if gaps else window_end)
        if target > points[-1]:
            points.append(target)
    return points


_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
})


def _normalise(text: str) -> tuple[str, list[int]]:
    """Lower-case, fold Unicode quotes and dashes to ASCII, collapse each
    whitespace run to one space. Returns the result and, for each of its
    characters, the offset in `text` it came from."""
    out: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            if out and out[-1] != " ":
                out.append(" ")
                origin.append(i)
            continue
        for folded in ch.translate(_FOLD).lower():
            out.append(folded)
            origin.append(i)
    return "".join(out), origin


def locate_quote(quote: str, text: str) -> tuple[int, int] | None:
    """Find the span of `text` that the model's `quote` was copied from.

    Both sides are normalised (whitespace runs, Unicode quotes and dashes,
    case) and the quote must then occur exactly. Returns (start, end) offsets
    into the original `text` for the first occurrence, or None. Callers cite
    text[start:end], the real source span, never the model's copy.
    """
    needle = _normalise(quote)[0].strip()
    if not needle:
        return None
    haystack, origin = _normalise(text)
    at = haystack.find(needle)
    if at < 0:
        return None
    return origin[at], origin[at + len(needle) - 1] + 1


def page_label(basis: PageBasis, number: int) -> str:
    """'p. 12' for PHYSICAL, 'section 12' for SYNTHETIC. The API never calls a synthetic section a page."""
    return f"section {number}" if basis is PageBasis.SYNTHETIC else f"p. {number}"


def collapse_whitespace(text: str) -> str:
    """'Each Occurrence Limit\\n$1,000,000' -> 'Each Occurrence Limit $1,000,000', for quotes shown to people."""
    return _WHITESPACE.sub(" ", text).strip()


def find_on_page(quote: str, pages: Sequence[Page], page: int) -> str | None:
    """The source text of `quote` on the page it cites, whitespace collapsed, or None.

    A quote found only on another page is a wrong citation and is rejected. A
    quote that runs across a page break is rejected too: the running footer
    and header sit between the two pages, so it is not contiguous source text.
    """
    text = next((p.text for p in pages if p.number == page), "")
    span = locate_quote(quote, text)
    return None if span is None else collapse_whitespace(text[span[0]:span[1]])
