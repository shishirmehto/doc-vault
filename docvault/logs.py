"""Process logging: one stderr handler on the root logger, every line stamped with the request id.

The API middleware sets request_id from X-Request-ID (or a fresh id) for each
request. Celery carries it in a task header and sets it again before the task
runs, so the worker's lines and ledger rows name the request that caused them.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

# uvicorn passes an ANSI-coloured copy of its unformatted message as color_message; it is not a field.
_NOT_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "request_id", "color_message"}


def accept(header: str | None) -> str:
    """The client's X-Request-ID when it is safe to log and echo, otherwise a fresh 32-hex id."""
    return header if header is not None and SAFE_ID.fullmatch(header) else uuid4().hex


def _fields(record: logging.LogRecord) -> dict[str, object]:
    """request_id, then every field the caller passed through extra=."""
    return {"request_id": getattr(record, "request_id", None),
            **{key: value for key, value in record.__dict__.items() if key not in _NOT_FIELDS}}


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        line = {"time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
                "level": record.levelname, "logger": record.name, "message": record.getMessage(), **_fields(record)}
        if record.exc_info:
            line["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


class TextFormatter(logging.Formatter):
    """LEVEL logger message key=value ..., then the traceback on the lines below."""

    def format(self, record: logging.LogRecord) -> str:
        pairs = (f"{key}={_text(value)}" for key, value in _fields(record).items())
        line = " ".join([record.levelname, record.name, record.getMessage(), *pairs])
        return f"{line}\n{self.formatException(record.exc_info)}" if record.exc_info else line


def _text(value: object) -> str:
    if isinstance(value, str) and value and not re.search(r'[\s"=]', value):
        return value
    return json.dumps(value, default=str)


FORMATTERS: dict[str, type[logging.Formatter]] = {"json": JsonFormatter, "text": TextFormatter}


def _stamp(record: logging.LogRecord) -> bool:
    record.request_id = request_id.get()
    return True


def configure(fmt: Literal["json", "text"]) -> None:
    """Make the root logger's only handler a stderr handler in `fmt` at INFO. Safe to call again.

    uvicorn's own loggers lose their handlers and propagate to root, so the API's lines share one format.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(FORMATTERS[fmt]())
    handler.addFilter(_stamp)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
