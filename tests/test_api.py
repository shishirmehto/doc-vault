"""The /documents routes over HTTP. The pipeline is run in-process between requests."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from docvault import documents
from docvault.domain import DocumentId

from .conftest import ENDORSEMENT, POLICY, QUOTE, SAMPLES


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
    assert {k: ready[k] for k in ("status", "pages", "page_basis", "empty_pages", "insights_status", "error")} == {
        "status": "ready", "pages": 7, "page_basis": "physical", "empty_pages": [], "insights_status": "pending",
        "error": None}
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
