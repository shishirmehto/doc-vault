"""Request ids and logs: X-Request-ID, one line per request, the ledger, Celery and the pipeline. Real Postgres."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import logging.config
import re
from collections.abc import Callable, Iterator

import pytest
import uvicorn.config
from celery.worker.request import Request
from fastapi.testclient import TestClient

from docvault import api, documents, insights, logs, tasks
from docvault.api import app
from docvault.config import Settings
from docvault.domain import VaultError
from docvault.tasks import celery_app, index_document

from .conftest import POLICY
from .test_ask_stream import LIMIT_QUESTION, events, policy_chat
from .test_chat import ready
from .test_insights import provider_down, with_chat

MISSING = "/documents/00000000-0000-0000-0000-000000000000"
FRESH_ID = re.compile(r"[0-9a-f]{32}")
LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture(autouse=True)
def no_request_id() -> Iterator[None]:
    """Each test starts without a request id, and none it sets reaches the next test."""
    token = logs.request_id.set(None)
    yield
    logs.request_id.reset(token)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """logs.configure changes process-wide loggers; put back what pytest and uvicorn had."""
    saved = [(logging.getLogger(name), logging.getLogger(name).handlers[:], logging.getLogger(name).level,
              logging.getLogger(name).propagate) for name in LOGGERS]
    yield
    for logger, handlers, level, propagate in saved:
        logger.handlers, logger.propagate = handlers, propagate
        logger.setLevel(level)


@pytest.fixture
def json_log(restore_logging) -> Callable[[str], list[dict]]:
    """logs.configure("json") writing to a buffer. Returns a reader of the parsed lines of one logger."""
    logs.configure("json")
    [handler] = logging.getLogger().handlers
    buffer = io.StringIO()
    handler.setStream(buffer)
    return lambda name: [line for line in map(json.loads, buffer.getvalue().splitlines()) if line["logger"] == name]


def without(line: dict, *keys: str) -> dict:
    return {key: value for key, value in line.items() if key not in keys}


def test_a_safe_request_id_comes_back_unchanged_on_success_and_on_error(client):
    found = client.get("/documents", headers={"X-Request-ID": "abc.DEF-1_2"})
    missing = client.get(MISSING, headers={"X-Request-ID": "abc.DEF-1_2"})

    assert (found.status_code, found.headers["x-request-id"]) == (200, "abc.DEF-1_2")
    assert (missing.status_code, missing.headers["x-request-id"]) == (404, "abc.DEF-1_2")


@pytest.mark.parametrize("sent", ["a" * 65, "two words", "a/b", ""], ids=["65 chars", "space", "slash", "empty"])
def test_an_unsafe_request_id_is_replaced_by_a_fresh_one(client, sent):
    response = client.get("/documents", headers={"X-Request-ID": sent})

    assert FRESH_ID.fullmatch(response.headers["x-request-id"])


def test_a_request_without_an_id_gets_a_fresh_one_each_time(client):
    first, second = (client.get("/documents").headers["x-request-id"] for _ in range(2))

    assert FRESH_ID.fullmatch(first) and FRESH_ID.fullmatch(second) and first != second


def test_one_request_writes_one_line_with_its_route_template_and_id(client, json_log):
    client.get(MISSING, headers={"X-Request-ID": "abc.DEF-1_2"})
    client.get("/no/such/route", headers={"X-Request-ID": "nowhere"})

    lines = json_log("docvault.requests")
    assert [without(line, "time", "latency_ms") for line in lines] == [
        {"level": "INFO", "logger": "docvault.requests", "message": "request", "request_id": "abc.DEF-1_2",
         "method": "GET", "route": "/documents/{doc_id}", "status": 404, "client": "testclient"},
        {"level": "INFO", "logger": "docvault.requests", "message": "request", "request_id": "nowhere",
         "method": "GET", "route": None, "status": 404, "client": "testclient"}]
    assert all(type(line["latency_ms"]) is int and line["latency_ms"] >= 0 for line in lines)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00", lines[0]["time"])


def test_an_upload_refused_by_its_content_length_is_tagged_and_logged_too(client, json_log):
    """Driven over ASGI, because the HTTP client would correct a Content-Length that does not match the body."""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/documents", "raw_path": b"/documents", "root_path": "", "query_string": b"",
             "headers": [(b"content-length", str(10 * 1024 * 1024).encode()), (b"x-request-id", b"too-big"),
                         (b"content-type", b"multipart/form-data; boundary=x")],
             "client": ("10.0.0.7", 1), "server": ("test", 80), "app": app}
    asyncio.run(app(scope, receive, send))

    assert (sent[0]["status"], dict(sent[0]["headers"])[b"x-request-id"]) == (413, b"too-big")
    assert [(line["request_id"], line["route"], line["status"], line["client"])
            for line in json_log("docvault.requests")] == [("too-big", None, 413, "10.0.0.7")]


def test_the_json_and_text_formats_carry_the_message_the_request_id_and_every_extra(restore_logging):
    logger = logging.getLogger("docvault.test")
    logs.request_id.set("req-7")
    written = {}
    for fmt in ("json", "text"):
        logs.configure(fmt)
        [handler] = logging.getLogger().handlers
        handler.setStream(buffer := io.StringIO())
        logger.info("hello %s", "there", extra={"count": 3, "note": "two words", "route": None})
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed")
        written[fmt] = buffer.getvalue()

    hello, failed = map(json.loads, written["json"].splitlines())
    assert without(hello, "time") == {"level": "INFO", "logger": "docvault.test", "message": "hello there",
                                      "request_id": "req-7", "count": 3, "note": "two words", "route": None}
    assert without(failed, "time", "exc_info") == {"level": "ERROR", "logger": "docvault.test", "message": "failed",
                                                   "request_id": "req-7"}
    assert failed["exc_info"].startswith("Traceback (most recent call last):")
    assert failed["exc_info"].endswith("ValueError: boom")
    text_lines = written["text"].splitlines()
    assert text_lines[0] == 'INFO docvault.test hello there request_id=req-7 count=3 note="two words" route=null'
    assert (text_lines[1], text_lines[2], text_lines[-1]) == (
        "ERROR docvault.test failed request_id=req-7", "Traceback (most recent call last):", "ValueError: boom")


def test_configure_twice_leaves_one_handler_and_uvicorn_lines_in_the_same_format(restore_logging, capsys):
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)   # what uvicorn does before the lifespan runs

    logs.configure("text")
    logs.configure("json")
    logging.getLogger("uvicorn.error").info("Application startup complete.")

    [line] = capsys.readouterr().err.splitlines()
    assert without(json.loads(line), "time") == {"level": "INFO", "logger": "uvicorn.error", "request_id": None,
                                                 "message": "Application startup complete."}


def test_a_streamed_ask_is_logged_once_after_its_turn_event(client, ctx, json_log):
    chat_id = policy_chat(client, ctx)
    sent: list[tuple[str, object]] = []

    async def recorded(scope, receive, send):
        async def send_and_record(message):
            if message["type"] == "http.response.body":
                sent.append(("body", message.get("body", b"")))
            await send(message)
        await app(scope, receive, send_and_record)

    class Into(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            sent.append(("log", record))

    requests_logger = logging.getLogger("docvault.requests")
    requests_logger.addHandler(into := Into())
    try:
        response = TestClient(recorded).post(f"/chats/{chat_id}/messages/stream", json={"question": LIMIT_QUESTION})
    finally:
        requests_logger.removeHandler(into)

    assert events(response.text)[-1][0] == "turn"
    turn_at = next(at for at, (kind, body) in enumerate(sent) if kind == "body" and b"event: turn" in body)
    [(log_at, record)] = [(at, item) for at, (kind, item) in enumerate(sent) if kind == "log"]
    assert turn_at < log_at == len(sent) - 1
    assert (record.route, record.status) == ("/chats/{chat_id}/messages/stream", 200)
    assert [(line["route"], line["status"]) for line in json_log("docvault.requests")] == [
        ("/chats", 201), ("/chats/{chat_id}/messages/stream", 200)]


def test_a_plain_and_a_streamed_ask_bill_their_model_calls_to_the_request_id(client, ctx):
    chat_id = policy_chat(client, ctx)

    plain = client.post(f"/chats/{chat_id}/messages", json={"question": LIMIT_QUESTION},
                        headers={"X-Request-ID": "plain-1"})
    streamed = client.post(f"/chats/{chat_id}/messages/stream", json={"question": LIMIT_QUESTION},
                           headers={"X-Request-ID": "stream-1"})

    assert (plain.status_code, streamed.status_code) == (200, 200)
    with ctx.db.connection() as conn:
        rows = conn.execute("SELECT purpose, request_id FROM llm_calls WHERE turn_id IS NOT NULL ORDER BY id")
        assert [tuple(row.values()) for row in rows] == [
            ("embed_query", "plain-1"), ("answer", "plain-1"),
            ("condense", "stream-1"), ("embed_query", "stream-1"), ("answer", "stream-1")]
        assert [row["request_id"] for row in conn.execute(
            "SELECT DISTINCT request_id FROM llm_calls WHERE turn_id IS NULL")] == [None]   # indexed outside a request


def test_celery_carries_the_request_id_from_the_publisher_to_the_task_and_drops_it_after(monkeypatch):
    """Published on the in-memory broker and run by the worker's own Request, with the task body replaced."""
    ran: list[tuple[str, str | None]] = []
    monkeypatch.setattr(tasks, "_ctx", lambda: None)
    monkeypatch.setattr(documents, "run_index", lambda _ctx, doc_id, attempt: ran.append((str(doc_id),
                                                                                          logs.request_id.get())))
    first, second = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"

    with celery_app.connection_for_write("memory://") as conn:
        queue = conn.SimpleQueue("ingest")
        queue.clear()
        token = logs.request_id.set("req-42")
        index_document.apply_async(args=[first], connection=conn)
        logs.request_id.reset(token)
        index_document.apply_async(args=[second], connection=conn)
        messages = [queue.get(timeout=1) for _ in range(2)]
        for message in messages:
            Request(message, app=celery_app).execute()

    assert [message.headers.get("request_id") for message in messages] == ["req-42", None]
    assert ran == [(first, "req-42"), (second, None)]


def test_closing_a_step_ok_or_failed_logs_one_pipeline_line_with_the_request_id(ctx, json_log):
    logs.request_id.set("req-steps")
    doc_id = ready(ctx, POLICY)
    with pytest.raises(VaultError):
        insights.run_analyze(with_chat(ctx, provider_down("insufficient_quota", retryable=False)), doc_id)

    lines = json_log("docvault.pipeline")
    step = {"level": "INFO", "logger": "docvault.pipeline", "message": "step", "request_id": "req-steps",
            "document_id": str(doc_id), "attempt": 1}
    assert [without(line, "time", "duration_ms") for line in lines] == [
        {**step, "step": "parse", "outcome": "ok", "error_code": None},
        {**step, "step": "embed", "outcome": "ok", "error_code": None},
        {**step, "step": "analyze", "outcome": "failed", "error_code": "ai_unavailable"}]
    assert all(type(line["duration_ms"]) is int and line["duration_ms"] >= 0 for line in lines)


def test_an_unhandled_exception_is_a_500_envelope_with_the_request_id_and_a_logged_traceback(ctx, json_log,
                                                                                             monkeypatch):
    app.state.ctx = ctx

    def broken(*_: object) -> None:
        raise RuntimeError("the bug")

    monkeypatch.setattr(documents, "get", broken)
    response = TestClient(app, raise_server_exceptions=False).get(MISSING, headers={"X-Request-ID": "oops-1"})

    assert (response.status_code, response.headers["x-request-id"], response.json()) == (
        500, "oops-1", {"error": {"code": "internal_error", "message": "The request failed unexpectedly.",
                                  "retryable": True, "details": {}}})
    [error] = json_log("docvault.api")
    assert (error["level"], error["request_id"], error["message"]) == (
        "ERROR", "oops-1", f"GET {MISSING} failed unexpectedly")
    assert error["exc_info"].endswith("RuntimeError: the bug")
    assert [(line["request_id"], line["route"], line["status"]) for line in json_log("docvault.requests")] == [
        ("oops-1", "/documents/{doc_id}", 500)]


def test_the_lifespan_configures_logging_in_the_configured_format_before_building_the_context(monkeypatch):
    calls: list[str] = []

    def build_ctx(*_: object) -> None:
        calls.append("build_ctx")
        raise RuntimeError("stopped after build_ctx")

    monkeypatch.setattr(api, "load_settings", lambda: Settings(_env_file=None, log_format="json"))
    monkeypatch.setattr(logs, "configure", calls.append)
    monkeypatch.setattr(api, "build_ctx", build_ctx)
    with pytest.raises(RuntimeError, match="stopped after build_ctx"), TestClient(app):
        pass

    assert calls == ["json", "build_ctx"]
