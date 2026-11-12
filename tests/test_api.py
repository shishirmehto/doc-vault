"""The /documents routes over HTTP. The pipeline is run in-process between requests."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from docvault import documents
from docvault.ai import ProviderError
from docvault.api import app
from docvault.domain import DocumentId

from .conftest import ENDORSEMENT, POLICY, QUOTE, SAMPLES
from .test_documents import FailingEmbedder, scanned_pdf, with_embedder


def post(client: TestClient, name: str, content: bytes | None = None):
    return client.post("/documents", files={"file": (name, (SAMPLES / name).read_bytes() if content is None else content)})


def test_upload_poll_reupload_and_open_a_page(client, ctx):
    created = post(client, POLICY)
    assert created.status_code == 202
    doc_id = created.json()["id"]
    assert created.headers["location"] == f"/documents/{doc_id}"
    assert {k: created.json()[k] for k in ("filename", "kind", "status", "pages", "progress", "error")} == {
        "filename": POLICY, "kind": "pdf", "status": "queued", "pages": None, "progress": None, "error": None}

    documents.run_index(ctx, DocumentId(UUID(doc_id)))

    ready = client.get(f"/documents/{doc_id}").json()
    assert {k: ready[k] for k in ("status", "pages", "page_basis", "empty_pages", "insights_status", "document_type",
                                  "error")} == {
        "status": "ready", "pages": 7, "page_basis": "physical", "empty_pages": [], "insights_status": "pending",
        "document_type": None, "error": None}
    assert ready["links"]["pages"] == f"/documents/{doc_id}/pages/{{n}}"

    again = post(client, POLICY)
    assert (again.status_code, again.json()["id"], again.json()["status"]) == (200, doc_id, "ready")

    page = client.get(f"/documents/{doc_id}/pages/2").json()
    assert (page["number"], page["label"]) == (2, "p. 2")
    assert "Each Occurrence Limit\n$1,000,000" in page["text"]

    missing = client.get(f"/documents/{doc_id}/pages/8")
    assert missing.status_code == 404
    assert missing.json()["error"] == {"code": "page_not_found", "retryable": False, "details": {},
                                       "message": "This document has no page 8. It has 7."}


def test_docx_sections_are_labelled_sections(client, ctx):
    doc_id = post(client, ENDORSEMENT).json()["id"]
    documents.run_index(ctx, DocumentId(UUID(doc_id)))
    assert client.get(f"/documents/{doc_id}").json()["page_basis"] == "synthetic"
    page = client.get(f"/documents/{doc_id}/pages/2").json()
    assert page["label"] == "section 2"
    assert "at least 20 days before the effective date of cancellation" in page["text"]


def test_rejected_uploads_store_nothing(client, ctx):
    outcomes = {
        "empty": post(client, "empty.pdf", b""),
        "image": post(client, "scan.pdf", b"\x89PNG\r\n\x1a\n" + bytes(64)),
        "large": post(client, "big.txt", b"word " * 300_000),
    }
    assert {k: (r.status_code, r.json()["error"]["code"]) for k, r in outcomes.items()} == {
        "empty": (422, "empty_file"), "image": (415, "unsupported_type"), "large": (413, "file_too_large")}
    assert client.get("/documents").json() == {"items": [], "next_cursor": None}
    assert [p for p in ctx.blobs.root.rglob("*") if p.is_file()] == []


def test_list_filters_by_status_and_pages_with_a_cursor(client, ctx):
    ids = [post(client, name).json()["id"] for name in (POLICY, QUOTE, ENDORSEMENT)]
    documents.run_index(ctx, DocumentId(UUID(ids[1])))

    assert [d["id"] for d in client.get("/documents", params={"status": "ready"}).json()["items"]] == [ids[1]]

    first = client.get("/documents", params={"limit": 2}).json()
    second = client.get("/documents", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [d["id"] for d in first["items"]] == [ids[2], ids[1]]
    assert ([d["id"] for d in second["items"]], second["next_cursor"]) == ([ids[0]], None)

    bad = client.get("/documents", params={"cursor": "not-a-cursor"})
    assert (bad.status_code, bad.json()["error"]["code"]) == (400, "invalid_cursor")


def test_retry_and_delete_over_http(client, ctx):
    doc_id = post(client, QUOTE).json()["id"]

    nothing = client.post(f"/documents/{doc_id}/retry")
    assert (nothing.status_code, nothing.json()["error"]["code"]) == (409, "nothing_to_retry")

    assert client.delete(f"/documents/{doc_id}").status_code == 204
    for response in (client.get(f"/documents/{doc_id}"), client.delete(f"/documents/{doc_id}"),
                     client.post(f"/documents/{uuid4()}/retry")):
        assert (response.status_code, response.json()["error"]["code"]) == (404, "document_not_found")


def test_a_failed_document_names_the_step_that_failed_and_a_retried_one_keeps_its_pages(client, ctx, tmp_path):
    scanned = post(client, "scan.pdf", scanned_pdf(tmp_path).read_bytes()).json()["id"]
    quote = post(client, QUOTE).json()["id"]
    documents.run_index(ctx, DocumentId(UUID(scanned)))
    down = with_embedder(ctx, FailingEmbedder(0, ProviderError("timeout", retryable=True)))
    documents.run_index(down, DocumentId(UUID(quote)), attempt=documents.MAX_RETRIES + 1)

    def state(doc: dict) -> tuple:
        return doc["status"], doc["failed_stage"], doc["pages"], doc["page_basis"], doc["error"] and doc["error"]["code"]

    assert state(client.get(f"/documents/{scanned}").json()) == ("failed", "parse", None, None, "no_extractable_text")
    assert state(client.get(f"/documents/{quote}").json()) == ("failed", "embed", 3, "physical", "ai_unavailable")
    assert state(client.post(f"/documents/{quote}/retry").json()) == ("queued", None, 3, "physical", None)
    assert state(client.post(f"/documents/{scanned}/retry").json()) == ("queued", None, None, None, None)


def test_every_error_response_in_the_openapi_schema_is_the_error_envelope(client):
    schema = client.get("/openapi.json").json()
    declared = {(path, method, status): response["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[1]
                for path, methods in schema["paths"].items() for method, operation in methods.items()
                for status, response in operation["responses"].items() if int(status) >= 400}
    assert {key: model for key, model in declared.items() if model != "ErrorOut"} == {
        ("/health", "get", "503"): "HealthOut"}
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert sorted(status for path, method, status in declared if (path, method) == ("/documents", "post")) == [
        "404", "409", "413", "415", "422", "429"]
    assert schema["paths"]["/documents"]["post"]["responses"]["422"]["description"] == "empty_file, invalid_request"


def test_malformed_input_gets_the_error_envelope_naming_the_field(client):
    response = client.get("/documents", params={"limit": 0})
    assert (response.status_code, response.json()) == (422, {"error": {
        "code": "invalid_request", "retryable": False,
        "message": "The request is not valid. query.limit: Input should be greater than or equal to 1",
        "details": {"errors": [{"field": "query.limit", "message": "Input should be greater than or equal to 1"}]}}})


def test_an_upload_whose_content_length_is_over_the_limit_is_refused_before_its_body_is_read(client, ctx):
    reads: list[dict] = []
    sent: list[dict] = []

    async def receive() -> dict:
        reads.append({})
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    length = ctx.settings.max_upload_bytes + 64 * 1024 + 1
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/documents", "raw_path": b"/documents", "root_path": "", "query_string": b"",
             "headers": [(b"content-length", str(length).encode()),
                         (b"content-type", b"multipart/form-data; boundary=x")],
             "client": ("test", 1), "server": ("test", 80), "app": app}
    asyncio.run(app(scope, receive, send))

    start, body = sent[0], json.loads(b"".join(m.get("body", b"") for m in sent[1:]))
    assert (start["status"], body["error"]["code"], body["error"]["message"], reads) == (
        413, "file_too_large", "The file is larger than the 1 MB limit.", [])


def test_a_streamed_upload_without_content_length_is_stopped_at_the_limit_while_it_is_copied(client, ctx):
    head = b'--b\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\nContent-Type: text/plain\r\n\r\n'
    chunks = [head, *[b"word " * 100_000] * 3, b"\r\n--b--\r\n"]   # 1.5 MB in three chunks, no Content-Length

    response = client.post("/documents", content=iter(chunks), headers={"content-type": "multipart/form-data; boundary=b"})

    assert (response.status_code, response.json()["error"]["code"]) == (413, "file_too_large")
    assert [p for p in ctx.blobs.root.rglob("*") if p.is_file()] == []


def test_health_reports_the_upload_limit_and_503_when_the_broker_is_down(client, ctx):
    app.state.ctx = dataclasses.replace(ctx, settings=ctx.settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"}))
    response = client.get("/health")
    assert (response.status_code, response.json()) == (503, {
        "db": "ok", "redis": "unreachable", "ai_mode": "fake", "embed_model": "fake-hash-1536",
        "max_upload_bytes": 1024 * 1024, "ai_budget": None, "cache": "off"})


def test_a_nul_in_the_uploaded_filename_is_dropped(client):
    body = (b'--b\r\nContent-Disposition: form-data; name="file"; filename="cgl\x00policy.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\nEach Occurrence Limit $500.\r\n--b--\r\n")

    response = client.post("/documents", content=body, headers={"content-type": "multipart/form-data; boundary=b"})

    assert (response.status_code, response.json()["filename"]) == (202, "cglpolicy.txt")
