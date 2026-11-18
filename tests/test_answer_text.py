from __future__ import annotations

import json

import pytest

from docvault import chat
from docvault.answer_text import AnswerField, MarkerFilter

ONE_DOCUMENT = (
    '{"supported": true, '
    '"answer": "The each occurrence limit is $1,000,000 [E1]. Defence costs do not reduce it [E2].", '
    '"evidence": [{"id": "E1", "source": "S1", "quote": "Each Occurrence Limit $1,000,000"}, '
    '{"id": "E2", "source": "S1", "quote": "Defence costs are payable in addition to the limits"}], '
    '"follow_ups": ["Is there a general aggregate limit?"]}'
)
TWO_DOCUMENTS = (
    '{"supported": true, '
    '"answer": "The policy deductible is $5,000 per claim [E1]. The renewal quote lowers it to $2,500 [E2, E3].", '
    '"evidence": [{"id": "E1", "source": "S1", "quote": "Deductible: $5,000 per claim"}, '
    '{"id": "E2", "source": "S2", "quote": "Deductible $2,500"}, '
    '{"id": "E3", "source": "S3", "quote": "a reduced deductible"}], '
    '"follow_ups": ["Does the quote change the premium?", "Which limits differ?"]}'
)
ESCAPED = (
    r'{"supported": true, '
    r'"answer": "The café clause defines \"insured\" [E1]. Claims go to C:\\Claims\\2026 [E2].\n'
    r'Approved with a smile \ud83d\ude00 [E1][E2]", '
    r'"evidence": [{"id": "E1", "source": "S1", "quote": "\"insured\" means"}, '
    r'{"id": "E2", "source": "S2", "quote": "C:\\Claims"}], '
    r'"follow_ups": []}'
)
MARKDOWN_LIST = (
    '{"supported": true, '
    '"answer": "Three terms change:\\n\\n- The aggregate is **$3,000,000** [E1]\\n'
    '- The deductible is **$5,000** [E2, E3]\\n- A silica exclusion is added [E4]", '
    '"evidence": [{"id": "E1", "source": "S1", "quote": "General Aggregate Limit $3,000,000"}, '
    '{"id": "E2", "source": "S1", "quote": "Deductible $5,000"}, {"id": "E3", "source": "S2", "quote": "$5,000"}, '
    '{"id": "E4", "source": "S2", "quote": "Form SM 30 11, Silica or Silica-Related Dust Exclusion, is added"}], '
    '"follow_ups": []}'
)
STRAY_SOURCE_MARKER = (
    '{"supported": true, "answer": "The aggregate is $2,000,000 [E1]. The deductible is $5,000 [S2].", '
    '"evidence": [{"id": "E1", "source": "S1", "quote": "Aggregate Limit $2,000,000"}], "follow_ups": []}'
)


def chunkings(text: str) -> list[list[str]]:
    return [[text[:at], text[at:]] for at in range(len(text) + 1)] + [list(text)]


def answer_of(chunks: list[str]) -> str:
    field = AnswerField()
    return "".join(field.feed(chunk) for chunk in chunks)


def displayed(chunks: list[str]) -> str:
    field, markers = AnswerField(), MarkerFilter()
    text = "".join(markers.feed(field.feed(chunk)) for chunk in chunks)
    assert field.done
    return text + markers.flush()


def filtered(chunks: list[str]) -> str:
    markers = MarkerFilter()
    return "".join(markers.feed(chunk) for chunk in chunks) + markers.flush()


@pytest.mark.parametrize(("raw", "answer", "display"), [
    (ONE_DOCUMENT,
     "The each occurrence limit is $1,000,000 [E1]. Defence costs do not reduce it [E2].",
     "The each occurrence limit is $1,000,000. Defence costs do not reduce it."),
    (TWO_DOCUMENTS,
     "The policy deductible is $5,000 per claim [E1]. The renewal quote lowers it to $2,500 [E2, E3].",
     "The policy deductible is $5,000 per claim. The renewal quote lowers it to $2,500."),
    (ESCAPED,
     'The café clause defines "insured" [E1]. Claims go to C:\\Claims\\2026 [E2].\n'
     "Approved with a smile \U0001f600 [E1][E2]",
     'The café clause defines "insured". Claims go to C:\\Claims\\2026.\nApproved with a smile \U0001f600'),
    (MARKDOWN_LIST,
     "Three terms change:\n\n- The aggregate is **$3,000,000** [E1]\n- The deductible is **$5,000** [E2, E3]\n"
     "- A silica exclusion is added [E4]",
     "Three terms change:\n\n- The aggregate is **$3,000,000**\n- The deductible is **$5,000**\n"
     "- A silica exclusion is added"),
    (STRAY_SOURCE_MARKER,
     "The aggregate is $2,000,000 [E1]. The deductible is $5,000 [S2].",
     "The aggregate is $2,000,000. The deductible is $5,000."),
], ids=["one-document", "two-documents", "escapes", "markdown-list", "stray-source-marker"])
def test_a_model_reply_streams_the_same_answer_and_display_text_at_every_split(raw, answer, display):
    assert json.loads(raw)["answer"] == answer
    for chunks in chunkings(raw):
        assert answer_of(chunks) == answer, chunks
        assert displayed(chunks) == display, chunks


@pytest.mark.parametrize(("raw", "answer"), [
    (r'{"answer": "\ud83d\u0041"}', "\ud83dA"),
    (r'{"answer": "\ud83dx"}', "\ud83dx"),
    (r'{"answer": "\ud83d\n"}', "\ud83d\n"),
    (r'{"answer": "ends high \ud83d"}', "ends high \ud83d"),
    (r'{"answer": "\ud83d\ud83d\ude00"}', "\ud83d\U0001f600"),
    (r'{"answer": "\ude00 low first"}', "\ude00 low first"),
])
def test_a_lone_surrogate_is_kept_as_json_loads_keeps_it(raw, answer):
    assert json.loads(raw)["answer"] == answer
    for chunks in chunkings(raw):
        assert answer_of(chunks) == answer, chunks


def test_every_json_escape_decodes_at_every_split():
    raw = r'{"answer": "\"\\\/\b\f\n\r\t 😀 é"}'
    assert json.loads(raw)["answer"] == '"\\/\b\f\n\r\t \U0001f600 é'
    for chunks in chunkings(raw):
        assert answer_of(chunks) == '"\\/\b\f\n\r\t \U0001f600 é', chunks


def test_a_high_surrogate_is_held_back_until_its_low_half_arrives():
    field = AnswerField()
    assert [field.feed(chunk) for chunk in (r'{"answer": "a\ud83d', r'\ude00b"}')] == ["a", "\U0001f600b"]


def test_an_unsupported_answer_streams_like_any_other():
    field = AnswerField()
    raw = '{"supported": false, "answer": "The documents do not cover flood damage.", "evidence": []}'
    assert (field.feed(raw), field.done) == ("The documents do not cover flood damage.", True)


def test_the_answer_is_read_when_it_comes_after_evidence_and_supported():
    raw = ('{"evidence": [{"id": "E1", "source": "S1", "quote": "Flood is excluded"}, [1, -2.5e3, null, true]], '
           '"supported": false, "answer": "Flood is not covered [E1]."}')
    for chunks in chunkings(raw):
        assert answer_of(chunks) == "Flood is not covered [E1].", chunks


def test_an_answer_key_below_the_top_level_is_ignored():
    raw = ('{"evidence": [{"source": "S1", "answer": "inside an evidence item"}], '
           '"meta": {"answer": "inside an object", "more": {"answer": "deeper"}}, '
           '"answer": "the top-level answer"}')
    for chunks in chunkings(raw):
        assert answer_of(chunks) == "the top-level answer", chunks


def test_keys_are_decoded_before_they_are_compared():
    assert answer_of([r'{"\u0061nswer": "an escaped key"}']) == "an escaped key"
    assert answer_of(['{"Answer": "a", "answer ": "b", "answers": "c", "answer": "d"}']) == "d"


@pytest.mark.parametrize(("raw", "answer", "done"), [
    ('{"answer": "a string", "supported": true}', "a string", True),
    ('{"answer": null, "supported": false}', "", False),
    ('{"answer": 42}', "", False),
    ('{"answer": ["a list"]}', "", False),
    ('{"answer": {"answer": "nested"}}', "", False),
    ('{"answer": false, "answer": "a later duplicate"}', "", False),
])
def test_only_a_string_answer_is_read_and_only_the_first(raw, answer, done):
    field = AnswerField()
    assert (field.feed(raw), field.done) == (answer, done)


@pytest.mark.parametrize(("raw", "answer"), [
    (' \r\n\t{"answer": "after leading whitespace"}', "after leading whitespace"),
    ('Sorry, here it is: {"answer": "prose before the object"}', ""),
    ('[{"answer": "an array, not an object"}]', ""),
    ('"answer"', ""),
    ('{"supported" true, "answer": "no colon"}', ""),
    ('{"supported": true "answer": "no comma"}', ""),
    ('{"supported": true,, "answer": "two commas"}', ""),
    ('{"evidence": [1, 2}, "answer": "mismatched bracket"}', ""),
    ('{"evidence": [], } "answer": "trailing comma"}', ""),
    ('{"supported": true} {"answer": "a second document"}', ""),
])
def test_text_that_is_not_json_before_the_answer_yields_nothing(raw, answer):
    for chunks in chunkings(raw):
        assert answer_of(chunks) == answer, chunks


@pytest.mark.parametrize("raw", [
    r'{"answer": "Partial text \q and more", "supported": true}',
    '{"answer": "Partial text \n and more"}',
    r'{"answer": "Partial text \u12G4 and more"}',
    r'{"answer": "Partial text \ud83d\u12G4"}',
])
def test_text_returned_before_invalid_json_stays_and_nothing_more_comes(raw):
    for chunks in chunkings(raw):
        field = AnswerField()
        assert ["".join(field.feed(chunk) for chunk in chunks), field.feed('", "answer": "more"}'), field.done] == [
            "Partial text ", "", False], chunks


def test_after_invalid_json_every_feed_returns_nothing():
    field = AnswerField()
    chunks = ['{"answer": "Partial ', r'text \q tail', '"}', '{"answer": "again"}']
    assert [field.feed(chunk) for chunk in chunks] == ["Partial ", "text ", "", ""]
    assert field.done is False


def test_done_flips_on_the_feed_that_closes_the_answer_string():
    field = AnswerField()
    chunks = ['{"answer": "say \\', '"hi\\"', '", "evidence": [', "]}", '{"answer": "ignored"}']
    assert [(field.feed(chunk), field.done) for chunk in chunks] == [
        ("say ", False), ('"hi"', False), ("", True), ("", True), ("", True)]


def test_done_flips_on_the_closing_quote_itself():
    field, done_after = AnswerField(), []
    for ch in '{"answer": "ok"}':
        field.feed(ch)
        done_after.append(field.done)
    assert done_after == [False] * 14 + [True, True]


def test_an_empty_answer_is_done_with_no_text():
    field = AnswerField()
    assert [(field.feed(chunk), field.done) for chunk in ('{"answer": "', '"}')] == [("", False), ("", True)]


@pytest.mark.parametrize(("text", "expected"), [
    ("limit [E1].", "limit."),
    ("a [E1][E12] b", "a b"),
    ("a [E2, E3] b", "a b"),
    ("a [E2,E3] b", "a b"),
    ("a [E2 ,E3] b", "a b"),
    ("limit [S2].", "limit."),
    ("a [E1][S2] b", "a b"),
    ("a [E1, S2] b", "a b"),
    ("a [E] b", "a [E] b"),
    ("a [1] b", "a [1] b"),
    ("a [e1] b", "a [e1] b"),
    ("a [E1 x] b", "a [E1 x] b"),
    ("a\n[E1] b", "a\n b"),
    ("a \t[E12]", "a"),
    ("a [E1 [E2] b", "a [E1 b"),
    ("ends in spaces  ", "ends in spaces"),
    ("a [E1", "a [E1"),
    ("a [E1][E2", "a[E2"),
    ("- **$1,000,000** [E1]\n- $2,500 [E2]", "- **$1,000,000**\n- $2,500"),
    ("1. limit [E1]\n2. deductible [E2]\n", "1. limit\n2. deductible\n"),
])
def test_markers_and_the_spaces_before_them_vanish_at_every_split(text, expected):
    for chunks in chunkings(text):
        assert filtered(chunks) == expected, chunks


def test_a_possible_marker_is_held_back_until_it_cannot_be_one():
    markers = MarkerFilter()
    chunks = ["limit [E", "1", "].", " see [E2 ", "x]", " and [S", "2]."]
    assert [markers.feed(chunk) for chunk in chunks] == ["limit", "", ".", " see", " [E2 x]", " and", "."]


def test_flush_returns_an_unfinished_marker_drops_a_trailing_pad_and_empties_the_filter():
    markers = MarkerFilter()
    assert [markers.feed("a [E1"), markers.flush(), markers.feed("b \t"), markers.flush(), markers.flush()] == [
        "a", " [E1", "b", "", ""]


def test_flush_drops_the_trailing_spaces_of_an_unfinished_marker():
    markers = MarkerFilter()
    assert [markers.feed("x [E1, "), markers.flush()] == ["x", " [E1,"]


GRAMMAR_CASES = [
    "", " ", "[", "[E1]", "[E1] starts the answer", "limit [E1].", "x[E1]y",
    "a [E1] [E2] b", "a [E1][E2][E3] b", "a [E1]\t[E2]", "a  \t [E12]", "a [E1]\t",
    "a\n[E1] b", "a\u00a0[E1] b", "trailing pad \t ",
    "a [E2, E3] b", "a [E2,E3][E4] b", "a [E2 ,\n E3] b", "a [E2,\nE3] b", "a [E1\u00a0,\u2003E2] b",
    "a [E\u0661] b", "a [E\u00b2] b",
    "a [E] b", "a [1] b", "a [e1] b", "a [E1 x] b", "a [E1 ] b", "a [E1,] b", "a [E1, E] b",
    "a [E1 [E2] b", "a [E1,  [E2] b", "a [E1 \n[E2] b", "a [ [E1] b", "a [E1[E2] b", "[[E1]]", "nested [E[E1]]",
    "a [E1][E2 x] b", "a [E1][", "a [E1", "a [E1][E2", "a [E1, ",
    "a [S2] b", "a [E1][S2] b", "a [S1, E2] b", "a [ES1] b", "a [SE1] b", "a [E1 [S2] b", "a [S1][E", "a [D1] b",
]


@pytest.mark.parametrize("text", GRAMMAR_CASES)
def test_removal_matches_the_marker_grammar_ground_uses(text):
    expected = chat._MARKERS.sub("", text).rstrip(" \t")
    for chunks in chunkings(text):
        assert filtered(chunks) == expected, chunks
