"""Deterministic stand-ins for the model provider, used when no OpenAI key is set and in the tests.

FakeEmbedder hashes words into vectors, so retrieval tests mean something. FakeChat answers each
prompt with simple text rules over its sources, so the whole pipeline runs offline and the same
way on every machine. ai.build picks them when no key is set, and the tests build them directly.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence

from pydantic import BaseModel

from .ai import RawCompletion, Tokens, Vector
from .domain import (
    DocumentId,
    KeyFact,
    Page,
    Source,
    SummaryLength,
    SummaryOptions,
    SummaryTone,
    Turn,
)
from .pages import SENTENCE_OR_PARAGRAPH_END, TABLE_CELL, collapse_whitespace
from .prompts import (
    DOCUMENT_TYPES,
    AnswerDraft,
    CellDraft,
    CompareDoc,
    ComparisonDraft,
    CondensedQuery,
    EvidenceDraft,
    InsightsDraft,
    KeyFactDraft,
    Prompt,
    RowDraft,
    SummaryDraft,
    SummarySourceDraft,
    Tier,
)


class FakeEmbedder:
    """Deterministic content-dependent embeddings, so retrieval tests mean something.

    Hashing-trick bag of words: lowercase, strip punctuation, drop English
    stopwords, crude suffix stemming; each token adds 1 + log(tf) to bucket
    blake2b(token) % dimensions with a hash-derived sign; L2-normalise. Texts
    sharing content words are near; unrelated texts are near-orthogonal. Same
    text, same vector, on every machine.
    """

    provider = "fake"
    model = "fake-hash"
    # Calibrated by `python -m docvault.evals --calibrate` on evals/cases.yaml.
    # tests/test_evals.py fails when the best value on the case set moves.
    min_similarity = 0.184

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str], *, timeout_s: float) -> tuple[list[Vector], Tokens]:
        return [self._vector(text) for text in texts], Tokens(sum(len(text.split()) for text in texts), 0, 0)

    def _vector(self, text: str) -> Vector:
        counts = Counter(_tokens(text))
        vector = [0.0] * self.dimensions
        # A text with no content words still needs a non-zero vector: cosine distance to zero is undefined.
        for token, count in (counts or {"": 1}).items():
            digest = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest())
            vector[digest % self.dimensions] += (1 if digest >> 63 else -1) * (1 + math.log(count))
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]


_STOPWORDS = frozenset(
    "a an and are as at be by for from has have if in is it its of on or that the this to was we were what "
    "when which will with you your our us not no any all does do".split())


def _tokens(text: str) -> list[str]:
    return [_stem(word) for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in _STOPWORDS]


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


_ENDS_SENTENCE = re.compile(r"""[.!?]["')]?$""")


def _segments(text: str) -> list[str]:
    """Extracted page text cut into the passages a reader sees, each a verbatim span of `text`.

    PDF extraction ends a line wherever the layout wrapped, and puts a label and its
    value on separate lines. A line joins the passage before it when it starts with a
    lowercase letter, '$' or a digit, when the line before ends with ':', or when the
    line before is 60 characters or longer and does not end a sentence. A DOCX table
    row stands alone: the long 'Location ... | address' row would otherwise swallow
    the row after it. Passages are then split at sentence ends.
    """
    passages: list[list[str]] = []
    for line in (raw.strip() for raw in text.split("\n")):
        previous = passages[-1][-1] if passages and passages[-1] else None
        if not line:
            passages.append([])
        elif previous is not None and _continues(previous, line):
            passages[-1].append(line)
        else:
            passages.append([line])
    return [part for lines in passages if lines
            for part in SENTENCE_OR_PARAGRAPH_END.split("\n".join(lines)) if part]


def _continues(previous: str, line: str) -> bool:
    if TABLE_CELL in previous or TABLE_CELL in line:
        return False
    return line[0].islower() or line[0] == "$" or line[0].isdigit() or previous.endswith(":") or _wrapped(previous)


def _wrapped(line: str) -> bool:
    """A long line that stops mid-sentence: the layout wrapped it."""
    return len(line) >= 60 and not _ENDS_SENTENCE.search(line)


def _key_fact(page: int, passage: str) -> KeyFactDraft | None:
    """A short label line ending in ':' over its value, a short label line over a '$' value, or a 'Label | value' row."""
    first, _, rest = passage.partition("\n")
    if rest and not _wrapped(first) and not _ENDS_SENTENCE.search(first) and (first.endswith(":") or rest.startswith("$")):
        label, value = first.removesuffix(":"), rest
    elif not rest and TABLE_CELL in first:
        label, _, value = first.partition(TABLE_CELL)
    else:
        return None
    return KeyFactDraft(label=label, value=collapse_whitespace(value), page=page, quote=collapse_whitespace(passage))


def _fake_insights(prompt: Prompt) -> InsightsDraft:
    """Title: the first all-capitals line of three or more words, else the filename. Type: the
    first document type named in the title, else "other". Summary: the first three passages.
    Key facts: label-and-value passages in page order, at most 12, each quoting its passage.
    Tags: up to six title words longer than three letters."""
    pages: tuple[Page, ...] = prompt.inputs["pages"]
    lines = [line.strip() for page in pages for line in page.text.split("\n")]
    title = next((line for line in lines if line.isupper() and len(line.split()) >= 3), prompt.inputs["filename"])
    words = re.findall(r"[a-z]+", title.lower())
    passages = [(page.number, passage) for page in pages for passage in _segments(page.text)]
    return InsightsDraft(
        title=title,
        document_type=next((word for word in words if word in DOCUMENT_TYPES), "other"),
        summary=" ".join(collapse_whitespace(passage) for _, passage in passages[:3]),
        key_facts=[fact for page, passage in passages if (fact := _key_fact(page, passage))][:12],
        tags=list(dict.fromkeys(word for word in words if len(word) > 3))[:6])


def _fake_compare(prompt: Prompt) -> ComparisonDraft:
    documents: tuple[CompareDoc, ...] = prompt.inputs["documents"]
    focus: str | None = prompt.inputs["focus"]
    fields: list[dict[str, KeyFact | KeyFactDraft]] = []
    for doc in documents:
        found = [*doc.key_facts, *(fact for page in doc.pages for passage in _segments(page.text)
                                   if (fact := _key_fact(page.number, passage)))]
        fields.append({})
        for fact in found:
            fields[-1].setdefault(" ".join(re.findall(r"[a-z0-9]+", fact.label.lower())), fact)
    wanted = set(_tokens(focus or ""))
    rows: list[RowDraft] = []
    for label in dict.fromkeys(label for by_label in fields for label in by_label):
        facts = [by_label.get(label) for by_label in fields]
        named = [fact for fact in facts if fact is not None]
        if len(named) < 2 or (focus and not wanted & set(_tokens(named[0].label))):
            continue
        rows.append(RowDraft(field=named[0].label, cells=[
            CellDraft(document=doc.label, value=None, page=None, quote=None) if fact is None else
            CellDraft(document=doc.label, value=fact.value, page=fact.page, quote=fact.quote)
            for doc, fact in zip(documents, facts, strict=True)]))
    return ComparisonDraft(rows=rows)


_FAKE_SUMMARY_PASSAGES: dict[SummaryLength, int] = {
    SummaryLength.BRIEF: 2, SummaryLength.STANDARD: 4, SummaryLength.DETAILED: 8}

_FAKE_SUMMARY_OPENINGS: dict[SummaryTone, str] = {
    SummaryTone.PLAIN: "{filename} in brief.",
    SummaryTone.FORMAL: "This note summarises {filename}.",
    SummaryTone.CLIENT: "Here is what {filename} means for you.",
}


def _fake_summary(prompt: Prompt) -> SummaryDraft:
    """The tone's opening sentence, naming the file, then the length's number of passages (_segments of the
    included pages), whitespace collapsed. With a focus, passages sharing more of its content words come first,
    ties and passages sharing none in document order. A focus word and a passage word are shared when their stems
    are equal or the shorter, of 4 letters or more, starts the longer: _stem leaves "deductibles" as "deductibl" but
    "deductible" whole, and a list marker such as "e." must not match "earthquake". Sources: the passages used, at
    most 6, each quoting itself on its page."""
    pages: tuple[Page, ...] = prompt.inputs["pages"]
    options: SummaryOptions = prompt.inputs["options"]
    passages = [(page.number, collapse_whitespace(passage)) for page in pages for passage in _segments(page.text)]
    wanted = set(_tokens(options.focus or ""))

    def same_stem(a: str, b: str) -> bool:
        shorter, longer = sorted((a, b), key=len)
        return shorter == longer or (len(shorter) >= 4 and longer.startswith(shorter))

    def shared(passage: str) -> int:
        words = set(_tokens(passage))
        return sum(any(same_stem(focus, word) for word in words) for focus in wanted)

    chosen = sorted(passages, key=lambda item: -shared(item[1]))[:_FAKE_SUMMARY_PASSAGES[options.length]]
    opening = _FAKE_SUMMARY_OPENINGS[options.tone].format(filename=prompt.inputs["filename"])
    return SummaryDraft(summary=" ".join([opening, *(passage for _, passage in chosen)]),
                        sources=[SummarySourceDraft(page=page, quote=passage) for page, passage in chosen[:6]])


_REFERENCE_WORD = re.compile(r"\b(?:it|its|that|this|these|those|they|them|one|same|other)\b", re.IGNORECASE)
_CONTINUATION = re.compile(r"^\W*(?:and|also|but|or|what about|how about)\b", re.IGNORECASE)


def _fake_condense(prompt: Prompt) -> CondensedQuery:
    """A follow-up is appended to the previous turn's standalone query, so it carries the earlier topic's words.
    A question is a follow-up when it has a reference word ("does it", "that limit") or opens by continuing the
    last one ("And the premium?", "What about flood?"); any other question already stands alone."""
    history: tuple[Turn, ...] = prompt.inputs["history"]
    question: str = prompt.inputs["question"]
    if _REFERENCE_WORD.search(question) or _CONTINUATION.search(question):
        return CondensedQuery(standalone=f"{history[-1].standalone_query} {question}")
    return CondensedQuery(standalone=question)


def _fake_answer(prompt: Prompt) -> AnswerDraft:
    """Per document, the segment sharing the most content words with the standalone query. Ties go to the
    segment with fewer distinct content words (the tighter match), then the earlier source, then the earlier
    segment. Every document whose best segment shares at least two words is cited, with that segment as both
    the sentence and the quote; with none, the draft is unsupported."""
    query = set(_tokens(prompt.inputs["standalone"]))
    sources: tuple[Source, ...] = prompt.inputs["sources"]
    best: dict[DocumentId, tuple[tuple[int, int, int, int], Source, str]] = {}
    for at, source in enumerate(sources):
        for position, segment in enumerate(_segments(source.text)):
            words = set(_tokens(segment))
            rank = (-len(words & query), len(words), at, position)
            if source.doc.id not in best or rank < best[source.doc.id][0]:
                best[source.doc.id] = (rank, source, segment)
    cited = sorted((rank, source, segment) for rank, source, segment in best.values() if -rank[0] >= 2)
    follow_ups = _fake_follow_ups(sources, query)
    if not cited:
        return AnswerDraft(supported=False, answer="The selected documents do not say.", evidence=[],
                           follow_ups=follow_ups)
    return AnswerDraft(
        supported=True,
        answer=" ".join(f"{collapse_whitespace(segment)} [E{n}]" for n, (_, _, segment) in enumerate(cited, 1)),
        evidence=[EvidenceDraft(id=f"E{n}", source=source.label, quote=segment)
                  for n, (_, source, segment) in enumerate(cited, 1)],
        follow_ups=follow_ups)


def _fake_follow_ups(sources: Sequence[Source], query: set[str]) -> list[str]:
    """"What is the <label>?" for the first three labels of label-and-value passages in the sources, in source
    order. A label needs two content words, because _fake_answer cites only a segment sharing two words with
    the question, and a label whose words the query already holds would ask the same question again."""
    labels = (fact.label for source in sources for segment in _segments(source.text)
              if (fact := _key_fact(0, segment)) and len(words := set(_tokens(fact.label))) >= 2
              and not words <= query)
    return [f"What is the {label}?" for label in list(dict.fromkeys(labels))[:3]]


class FakeChat:
    """Deterministic stand-in that reads Prompt.inputs, never the rendered text.

    complete_json looks the prompt's name up in `handlers`. A handler turns the
    prompt into an output model, which goes back as its JSON; a name without a
    handler raises NotImplementedError. Tests subclass FakeChat and replace
    `handlers` to script other output or a provider failure.

    Handlers: insights (_fake_insights), condense (_fake_condense), answer (_fake_answer),
    compare (_fake_compare), summary (_fake_summary).

    Token counts are whitespace word counts (system and messages in, JSON out),
    so ledger and metrics tests have real numbers.
    """

    provider = "fake"
    models: Mapping[Tier, str] = {"main": "fake-chat", "small": "fake-chat"}
    declines_unanswerable = False
    handlers: Mapping[str, Callable[[Prompt], BaseModel]] = {
        "insights": _fake_insights, "condense": _fake_condense, "answer": _fake_answer, "compare": _fake_compare,
        "summary": _fake_summary}

    def __init__(self, stream_delay_ms: int = 0) -> None:
        self.stream_delay_ms = stream_delay_ms

    def complete_json(self, prompt: Prompt, output: type[BaseModel], *, timeout_s: float,
                      on_text: Callable[[str], None] | None = None) -> tuple[RawCompletion, Tokens]:
        handler = self.handlers.get(prompt.name)
        if handler is None:
            raise NotImplementedError(f"FakeChat has no handler for the {prompt.name!r} prompt")
        text = handler(prompt).model_dump_json()
        if on_text is not None:
            for word in _WORD_PIECES.split(text):
                if self.stream_delay_ms:
                    time.sleep(self.stream_delay_ms / 1000)
                on_text(word)
        sent = [prompt.system, *(message.content for message in prompt.messages)]
        return RawCompletion(text), Tokens(sum(len(part.split()) for part in sent), 0, len(text.split()))


_WORD_PIECES = re.compile(r"(?<=\s)(?=\S)")
