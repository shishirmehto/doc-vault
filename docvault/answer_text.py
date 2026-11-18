"""Provisional answer text from a streaming answer-model reply. Pure, no I/O.

The answer model returns JSON shaped like prompts.AnswerDraft, and the server
shows the answer while that JSON is still arriving. This module owns turning
raw output chunks into displayable text, in two incremental pieces a caller
composes as ``marker_filter.feed(answer_field.feed(chunk))``:

- AnswerField reads the decoded value of the top-level "answer" string out of
  JSON split at any character.
- MarkerFilter removes [E#] marker runs, and stray [S#] ones, with the
  grammar of chat._MARKERS, holding back text that could still turn into a marker.
"""

from __future__ import annotations

from enum import Enum, auto

_JSON_WHITESPACE = frozenset(" \t\n\r")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_LITERAL_CHARS = frozenset("0123456789+-.eEtrufalsn")
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class _Scan(Enum):
    DOCUMENT = auto()
    KEY_OR_CLOSE = auto()
    KEY = auto()
    COLON = auto()
    ANSWER_COLON = auto()
    ANSWER_VALUE = auto()
    VALUE_OR_CLOSE = auto()
    VALUE = auto()
    COMMA_OR_CLOSE = auto()
    LITERAL = auto()
    STRING = auto()
    ESCAPE = auto()
    HEX = auto()
    ANSWERED = auto()
    STOPPED = auto()


class _Role(Enum):
    TOP_KEY = auto()
    KEY = auto()
    VALUE = auto()
    ANSWER = auto()


class AnswerField:
    """The top-level "answer" string of one JSON text, decoded as its chunks arrive.

    Each feed returns the answer characters that chunk made known. A high
    surrogate escape is held back until the next escape shows whether its low
    half follows. Only the first top-level "answer" value is read; once its
    closing quote is seen, done is true. At the first character that cannot
    occur in JSON at that point, the scanner stops for good.
    """

    def __init__(self) -> None:
        self._state = _Scan.DOCUMENT
        self._closers: list[str] = []
        self._role = _Role.VALUE
        self._decoded: list[str] = []
        self._hex = ""
        self._high = ""

    @property
    def done(self) -> bool:
        return self._state is _Scan.ANSWERED

    def feed(self, chunk: str) -> str:
        for ch in chunk:
            if self._state in (_Scan.ANSWERED, _Scan.STOPPED):
                break
            self._step(ch)
        if self._role is not _Role.ANSWER:
            return ""
        text = "".join(self._decoded)
        self._decoded.clear()
        return text

    def _step(self, ch: str) -> None:
        if self._state in (_Scan.STRING, _Scan.ESCAPE, _Scan.HEX):
            self._string_step(ch)
            return
        if self._state is _Scan.LITERAL:
            if ch in _LITERAL_CHARS:
                return
            self._state = _Scan.COMMA_OR_CLOSE
        if ch in _JSON_WHITESPACE:
            return
        match self._state, ch:
            case _Scan.DOCUMENT, "{":
                self._open("}", _Scan.KEY_OR_CLOSE)
            case (_Scan.KEY_OR_CLOSE | _Scan.KEY), '"':
                self._begin(_Role.TOP_KEY if len(self._closers) == 1 else _Role.KEY)
            case (_Scan.KEY_OR_CLOSE | _Scan.VALUE_OR_CLOSE | _Scan.COMMA_OR_CLOSE), _ if ch == self._closers[-1]:
                self._closers.pop()
                self._state = _Scan.COMMA_OR_CLOSE if self._closers else _Scan.STOPPED
            case _Scan.COLON, ":":
                self._state = _Scan.VALUE
            case _Scan.ANSWER_COLON, ":":
                self._state = _Scan.ANSWER_VALUE
            case _Scan.ANSWER_VALUE, '"':
                self._begin(_Role.ANSWER)
            case (_Scan.VALUE | _Scan.VALUE_OR_CLOSE), '"':
                self._begin(_Role.VALUE)
            case (_Scan.VALUE | _Scan.VALUE_OR_CLOSE), "{":
                self._open("}", _Scan.KEY_OR_CLOSE)
            case (_Scan.VALUE | _Scan.VALUE_OR_CLOSE), "[":
                self._open("]", _Scan.VALUE_OR_CLOSE)
            case (_Scan.VALUE | _Scan.VALUE_OR_CLOSE), _ if ch in _LITERAL_CHARS:
                self._state = _Scan.LITERAL
            case _Scan.COMMA_OR_CLOSE, ",":
                self._state = _Scan.KEY if self._closers[-1] == "}" else _Scan.VALUE
            case _:
                self._state = _Scan.STOPPED

    def _string_step(self, ch: str) -> None:
        match self._state, ch:
            case _Scan.STRING, '"':
                self._end()
            case _Scan.STRING, "\\":
                self._state = _Scan.ESCAPE
            case _Scan.STRING, _ if ch >= " ":
                self._put(ch)
            case _Scan.ESCAPE, "u":
                self._hex = ""
                self._state = _Scan.HEX
            case _Scan.ESCAPE, _ if ch in _ESCAPES:
                self._put(_ESCAPES[ch])
                self._state = _Scan.STRING
            case _Scan.HEX, _ if ch in _HEX_DIGITS:
                self._hex += ch
                if len(self._hex) == 4:
                    self._put_code_point(int(self._hex, 16))
                    self._state = _Scan.STRING
            case _:
                self._state = _Scan.STOPPED

    def _open(self, closer: str, state: _Scan) -> None:
        self._closers.append(closer)
        self._state = state

    def _begin(self, role: _Role) -> None:
        self._role = role
        self._state = _Scan.STRING

    def _end(self) -> None:
        self._put("")  # a high surrogate still held is a lone one
        match self._role:
            case _Role.ANSWER:
                self._state = _Scan.ANSWERED
                return
            case _Role.TOP_KEY:
                self._state = _Scan.ANSWER_COLON if "".join(self._decoded) == "answer" else _Scan.COLON
            case _Role.KEY:
                self._state = _Scan.COLON
            case _Role.VALUE:
                self._state = _Scan.COMMA_OR_CLOSE
        self._decoded.clear()

    def _put(self, text: str) -> None:
        # Anything but a low-surrogate escape after a held high surrogate leaves it lone, kept as json.loads keeps it.
        self._decoded.append(self._high + text)
        self._high = ""

    def _put_code_point(self, code: int) -> None:
        if self._high and 0xDC00 <= code <= 0xDFFF:
            self._decoded.append(chr(0x10000 + ((ord(self._high) - 0xD800) << 10) + code - 0xDC00))
            self._high = ""
        elif 0xD800 <= code <= 0xDBFF:
            self._decoded.append(self._high)
            self._high = chr(code)
        else:
            self._put(chr(code))


class _Marker(Enum):
    TEXT = auto()
    OPEN = auto()
    LABEL = auto()
    NUMBER = auto()
    BEFORE_COMMA = auto()
    AFTER_COMMA = auto()


class MarkerFilter:
    """Text with every [E#] or [S#] marker run and the spaces and tabs before it removed, as it arrives.

    Uses exactly the grammar of chat._MARKERS. Trailing text that could still
    become a marker is held back and released the moment it cannot. Adjacent
    markers ("[E1][E2]") are removed one at a time, which removes the same
    text as removing their run in one match.
    """

    def __init__(self) -> None:
        self._state = _Marker.TEXT
        self._pad = ""
        self._marker = ""

    def feed(self, text: str) -> str:
        return "".join(self._step(ch) for ch in text)

    def flush(self) -> str:
        """What is still held: an unfinished marker stays text, a trailing pad goes."""
        held = (self._pad + self._marker).rstrip(" \t")
        self._state, self._pad, self._marker = _Marker.TEXT, "", ""
        return held

    def _step(self, ch: str) -> str:
        match self._state:
            case _Marker.TEXT if ch in " \t":
                self._pad += ch
                return ""
            case _Marker.TEXT if ch == "[":
                self._marker = ch
                self._state = _Marker.OPEN
                return ""
            case _Marker.TEXT:
                released, self._pad = self._pad, ""
                return released + ch
        following = _following(self._state, ch)
        if following is None:
            # Every match attempt from the pad through this "[" fails at ch, but a marker can still start
            # after the "[" (a space inside "[E1 [E2]" is the next marker's pad), so the rest is scanned again.
            released, rescan = self._pad + "[", self._marker[1:] + ch
            self._state, self._pad, self._marker = _Marker.TEXT, "", ""
            return released + "".join(self._step(c) for c in rescan)
        if following is _Marker.TEXT:
            self._pad, self._marker = "", ""
        else:
            self._marker += ch
        self._state = following
        return ""


def _following(state: _Marker, ch: str) -> _Marker | None:
    match state, ch:
        case _Marker.OPEN, "E" | "S":
            return _Marker.LABEL
        case (_Marker.LABEL | _Marker.NUMBER), _ if ch.isdecimal():
            return _Marker.NUMBER
        case _Marker.NUMBER, "]":
            return _Marker.TEXT
        case (_Marker.NUMBER | _Marker.BEFORE_COMMA), ",":
            return _Marker.AFTER_COMMA
        case (_Marker.NUMBER | _Marker.BEFORE_COMMA), _ if ch.isspace():
            return _Marker.BEFORE_COMMA
        case _Marker.AFTER_COMMA, "E" | "S":
            return _Marker.LABEL
        case _Marker.AFTER_COMMA, _ if ch.isspace():
            return _Marker.AFTER_COMMA
    return None
