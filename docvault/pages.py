"""Document text and page provenance. Pure functions, no database, no network.

This module owns every rule about where text came from: format sniffing,
per-format extraction into numbered pages, chunking that remembers page
boundaries, and locating a quote back to an exact page. The pipeline, the
insights step and chat all ask this module; none of them does offset math.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from .domain import ChunkDraft, DocKind, ExtractedText, Page, PageBasis

PAGE_JOINER = "\n\n"   # placed between non-empty pages in the document text stream
SYNTHETIC_PAGE_CHARS = 3000


def sniff_kind(head: bytes, filename: str) -> DocKind:
    """Decide the format from the first bytes, not the extension or Content-Type.

    b"%PDF-" -> PDF. A zip whose names include word/document.xml -> DOCX
    (needs the whole file, so the caller passes the stored path when head is a
    zip signature). Otherwise UTF-8 / UTF-8-BOM / cp1252-decodable text with no
    NUL bytes -> TXT. Anything else (legacy .doc, images, xlsx) raises
    VaultError(UNSUPPORTED_TYPE) with the detected type in the message.
    """
    raise NotImplementedError


def extract(kind: DocKind, path: Path, *, max_pages: int) -> ExtractedText:
    """Read the stored file into numbered pages. Dispatches through _EXTRACTORS.

    Raises VaultError, never a library exception:
      ENCRYPTED_PDF (decrypt with "" fails), UNREADABLE_FILE (parser error),
      TOO_MANY_PAGES (> max_pages), NO_EXTRACTABLE_TEXT (every page empty after
      strip; a scanned PDF). Pages with some text and pages without are both
      kept, so page numbers never shift.
    """
    raise NotImplementedError


def _extract_pdf(path: Path) -> ExtractedText:
    """pypdf, one Page per physical page, basis PHYSICAL.

    TODO: normalise per page: join hyphenated line breaks ("cover-\\nage" ->
    "coverage"), collapse runs of spaces, keep newlines between lines so the
    chunker can find paragraph breaks.
    """
    raise NotImplementedError


def _extract_docx(path: Path) -> ExtractedText:
    """python-docx, body paragraphs and tables in document order, cut into
    SYNTHETIC sections of ~SYNTHETIC_PAGE_CHARS at paragraph boundaries.
    Tables become one line per row, cells joined by " | ".
    """
    raise NotImplementedError


def _extract_txt(path: Path) -> ExtractedText:
    """UTF-8 (with or without BOM) or cp1252 text, cut into SYNTHETIC sections."""
    raise NotImplementedError


_EXTRACTORS: dict[DocKind, Callable[[Path], ExtractedText]] = {
    DocKind.PDF: _extract_pdf,
    DocKind.DOCX: _extract_docx,
    DocKind.TXT: _extract_txt,
}


def chunk(doc: ExtractedText, *, max_chars: int, overlap_chars: int) -> list[ChunkDraft]:
    """Cut the document into overlapping chunks that may cross page boundaries.

    Invariants (each has a test):
      - chunk.text == stream[chunk.start:chunk.end] where
        stream = PAGE_JOINER.join(non-empty page texts). Offsets are produced by
        construction, never recovered with str.find, so boilerplate repeated on
        many pages (common in policies) cannot be mislocated.
      - every non-empty page's text is covered by at least one chunk.
      - chunk.page_map.page_at(i) is the page that character i of chunk.text came from.
      - len(chunk.text) <= max_chars unless a single unsplittable token run is longer.

    TODO:
      page_starts = [(stream_offset, page.number) for each non-empty page]
      units = paragraph spans of the stream (split on blank lines); a unit longer
              than max_chars is split at sentence ends, then at whitespace
      pack units greedily into [start, end) while end - start <= max_chars
      the next chunk starts at the first unit boundary >= end - overlap_chars
      page_map = [(max(ps - start, 0), page) for (ps, page) in page_starts
                  if ps < end and next_page_start(ps) > start]
    """
    raise NotImplementedError


def locate_quote(quote: str, text: str) -> tuple[int, int] | None:
    """Find the span of `text` that the model's `quote` was copied from.

    Both sides are normalised (whitespace runs, Unicode quotes and dashes,
    case) and the quote must then occur exactly. Returns (start, end) offsets
    into the original `text`, or None. Callers cite text[start:end], the real
    source span, never the model's copy.
    """
    raise NotImplementedError


def page_label(basis: PageBasis, number: int) -> str:
    """'p. 12' for PHYSICAL, 'section 12' for SYNTHETIC. The API never calls a synthetic section a page."""
    return f"section {number}" if basis is PageBasis.SYNTHETIC else f"p. {number}"


def find_on_page(quote: str, pages: Sequence[Page], page: int) -> tuple[int, str] | None:
    """Verify an insight's quote on its claimed page, allowing the next page
    (facts that straddle a break). Returns (actual_page, matched_span) or None.
    """
    raise NotImplementedError
