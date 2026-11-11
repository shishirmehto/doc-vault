"""GET /events/documents against a real uvicorn server, a real LISTEN connection and the fake provider.

A real server rather than TestClient, so a client that leaves closes a real socket and the slot is freed through
uvicorn and the HTTP middleware, as in production. vault_test is shared with other test runs whose commits also
reach this listener, so assertions look only at this test's document ids and skip resync frames they cause.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
import uvicorn

from docvault import documents, events, insights
from docvault.api import app
from docvault.db import PipelineSteps
from docvault.domain import DocumentId

from .conftest import POLICY, SAMPLES

type Frame = tuple[str, object]
LIFECYCLE = ["queued", "parsing", "embedding", "ready"]


class Stream:
    """One open event stream, read a frame at a time. A comment reads as (":", text)."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self._lines = response.iter_lines()

    def next(self) -> Frame:
        lines: list[str] = []
        for line in self._lines:
            if line:
                lines.append(line)
            elif lines:
                break
        else:
            raise AssertionError(f"the stream ended after {lines}")
        if lines[0].startswith(":"):
            return ":", lines[0][1:].strip()
        fields = dict(line.split(": ", 1) for line in lines)
        return fields["event"], json.loads(fields["data"])

    def read_until(self, wanted: Callable[[Frame], bool], within: float = 10) -> list[Frame]:
        """Frames up to and including the first wanted one, or every frame read until `within` passed."""
        deadline = time.monotonic() + within
        seen: list[Frame] = []
        while time.monotonic() < deadline:
            seen.append(self.next())
            if wanted(seen[-1]):
                break
        return seen


@dataclass
class Live:
    client: httpx.Client
    hub: events.Hub
    database_url: str

    @contextmanager
    def stream(self) -> Iterator[Stream]:
        with self.client.stream("GET", "/events/documents") as response:
            assert response.status_code == 200, response.read()
            stream = Stream(response)
            assert stream.next() == (":", "connected")
            yield stream

    def notify(self, payload: str) -> None:
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute("SELECT pg_notify('documents', %s)", (payload,))

    def wait_until_no_stream_is_open(self, within: float = 5) -> None:
        deadline = time.monotonic() + within
        while self.hub.open and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.hub.open == 0, "every stream's slot was freed"


@pytest.fixture
def live(ctx, database_url: str, monkeypatch) -> Iterator[Live]:
    """The real app on a free port, its lifespan swapped for one that uses the test ctx and a small hub.
    The listener's liveness probe runs every 0.2 s, so every test crosses several probe cycles."""
    monkeypatch.setattr(events, "PROBE_S", 0.2)
    hubs: list[events.Hub] = []

    @asynccontextmanager
    async def test_lifespan(_app):
        app.state.ctx = ctx
        async with events.running(database_url, ctx.db, cap=2, keepalive_s=0.3) as hub:
            app.state.events = hub
            hubs.append(hub)
            yield

    production_lifespan = app.router.lifespan_context
    app.router.lifespan_context = test_lifespan
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="warning", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "the server started"
        with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=5) as client:
            live = Live(client, hubs[0], database_url)
            with live.stream() as probe:
                probe_id = str(uuid4())
                heard: list[Frame] = []
                while ("deleted", {"id": probe_id}) not in heard and time.monotonic() < deadline:
                    live.notify(probe_id)
                    heard = probe.read_until(lambda frame: frame == ("deleted", {"id": probe_id}), within=0.5)
                assert ("deleted", {"id": probe_id}) in heard, "the listener is up"
            live.wait_until_no_stream_is_open()
            yield live
    finally:
        server.should_exit = True
        thread.join(10)
        app.router.lifespan_context = production_lifespan
        sock.close()


def for_doc(doc_id: str) -> Callable[[Frame], bool]:
    return lambda frame: frame[0] in ("document", "deleted") and frame[1]["id"] == doc_id


def test_a_document_streams_each_status_until_ready_then_its_insights_then_its_deletion(live, ctx):
    with live.stream() as stream:
        uploaded = live.client.post("/documents", files={"file": (POLICY, (SAMPLES / POLICY).read_bytes())})
        doc_id = uploaded.json()["id"]
        on_upload = [data for _, data in filter(for_doc(doc_id), stream.read_until(for_doc(doc_id)))]

        documents.run_index(ctx, DocumentId(UUID(doc_id)))
        indexing = stream.read_until(lambda frame: for_doc(doc_id)(frame) and frame[1]["processing_ms"] is not None)
        settling = stream.read_until(lambda frame: False, within=0.6)
        indexed = [data for _, data in filter(for_doc(doc_id), indexing + settling)]

        insights.run_analyze(ctx, DocumentId(UUID(doc_id)))
        analysed = stream.read_until(lambda frame: for_doc(doc_id)(frame) and frame[1]["insights_status"] == "ready")
        document_type = live.client.get(f"/documents/{doc_id}").json()["document_type"]

        assert live.client.delete(f"/documents/{doc_id}").status_code == 204
        deleted = stream.read_until(lambda frame: frame[0] == "deleted" and frame[1]["id"] == doc_id)

    statuses = [data["status"] for data in indexed]
    assert (uploaded.status_code, [data["status"] for data in on_upload]) == (202, ["queued"])
    assert statuses == sorted(statuses, key=LIFECYCLE.index) and statuses[-1] == "ready", statuses
    assert indexed[-1]["processing_ms"] is not None, indexed[-1]
    assert document_type is not None
    assert (analysed[-1][1]["insights_status"], analysed[-1][1]["document_type"]) == ("ready", document_type)
    assert deleted[-1] == ("deleted", {"id": doc_id})


def test_a_ready_event_read_before_the_embed_step_closed_is_followed_by_one_with_processing_ms(live, ctx,
                                                                                               monkeypatch):
    close = PipelineSteps._close

    def late_close(steps, *args):
        time.sleep(0.5)
        close(steps, *args)

    monkeypatch.setattr(PipelineSteps, "_close", late_close)
    with live.stream() as stream:
        doc_id = live.client.post("/documents", files={"file": (POLICY, (SAMPLES / POLICY).read_bytes())}).json()["id"]
        documents.run_index(ctx, DocumentId(UUID(doc_id)))
        seen = stream.read_until(lambda frame: for_doc(doc_id)(frame) and frame[1]["processing_ms"] is not None)

    ready = [data["processing_ms"] is None for _, data in filter(for_doc(doc_id), seen) if data["status"] == "ready"]
    assert ready == [True, False], seen


def test_version_1_hears_the_latest_version_change_when_version_2_arrives_and_when_it_goes(live):
    def post(text: str) -> str:
        return live.client.post("/documents", files={"file": ("terms.txt", text.encode())}).json()["id"]

    v1 = post("Premium: $1,000")
    with live.stream() as stream:
        v2 = post("Premium: $1,200")
        on_insert = stream.read_until(lambda frame: for_doc(v1)(frame) and frame[1]["latest_version"] == 2)
        assert live.client.delete(f"/documents/{v2}").status_code == 204
        on_delete = stream.read_until(lambda frame: for_doc(v1)(frame) and frame[1]["latest_version"] == 1)

    def latest(frames: list[Frame], doc_id: str) -> list[tuple[int, int]]:
        return [(data["version"], data["latest_version"]) for event, data in frames
                if event == "document" and data["id"] == doc_id]

    assert latest(on_insert, v1)[-1] == (1, 2)
    assert latest(on_insert, v2) == [(2, 2)]
    assert ("deleted", {"id": v2}) in on_delete
    assert latest(on_delete, v1)[-1] == (1, 1)


def test_past_the_cap_a_stream_is_refused_with_503_until_a_client_leaves(live):
    with live.stream():
        with live.stream(), live.client.stream("GET", "/events/documents") as refused:
            refused_body = refused.read() if refused.status_code == 503 else b""
        left_at = time.monotonic()
        reopened = None
        while time.monotonic() - left_at < 5:
            with live.client.stream("GET", "/events/documents") as response:
                if response.status_code == 200:
                    reopened = time.monotonic() - left_at
                    break
            time.sleep(0.05)

    assert (refused.status_code, refused.headers.get("retry-after"), refused_body and json.loads(refused_body)) == (
        503, "5", {"error": {"code": "too_many_streams", "retryable": True, "details": {"retry_after_s": 5},
                             "message": "Too many live document streams are open. Try again shortly."}})
    assert reopened is not None and reopened < 1.5, reopened
    live.wait_until_no_stream_is_open()


def test_a_stream_with_nothing_to_send_sends_keepalive_comments(live):
    with live.stream() as stream:
        seen = stream.read_until(lambda frame: frame == (":", "keepalive"))

    assert seen[-1] == (":", "keepalive"), seen


def test_a_stream_gets_resync_after_the_listener_connection_is_killed(live):
    with live.stream() as stream:
        with psycopg.connect(live.database_url, autocommit=True) as conn:
            killed = conn.execute(
                "SELECT count(*) FROM (SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE application_name = 'docvault-events' AND datname = current_database()) AS t").fetchone()[0]
        seen = stream.read_until(lambda frame: frame == ("resync", {}), within=5)

    assert killed >= 1
    assert seen[-1] == ("resync", {}), seen
