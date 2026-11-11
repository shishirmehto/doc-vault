# Upload and indexing

A user uploads a PDF, DOCX or TXT file and gets an id at once. The document then moves from queued through parsing and embedding to ready in the background. Uploading the same bytes again returns the existing document without new work. A file that cannot be read ends in failed, with a reason and a retry.

## Sub-features

- `upload-new` returns 202 and a queued document for bytes the vault has not seen.
- `upload-dedupe` returns 200 and the same id for bytes it has seen, with no new processing.
- `upload-reject` refuses an unsupported type with 415 (or 400) and an error body, and creates no document.
- `index-ready` brings a document to `ready` with every chunk embedded.
- `index-fail-retry` ends a textless PDF in `failed` with `error.code == "no_extractable_text"`, and `POST /retry` queues it again.
- `page-read` returns the text of one page, which is the text a citation points at.

## How to get to it (user POV)

- `POST /documents` with a multipart `file` field (curl, Swagger at `/docs`, or the UI drop zone).
- `GET /documents` and `GET /documents/{id}` to watch status.
- `POST /documents/{id}/retry` on a failed document.
- `GET /documents/{id}/pages/{n}`.

## Driving it with call.sh

Preconditions:

- A baseline stack with no documents (`$S/call.sh GET /documents` returns `"items": []`).

- **Upload new.** Run `$S/call.sh POST /documents -F file=@samples/sample-cgl-policy.pdf`. Status `202`, body `"status": "queued"`, `"filename": "sample-cgl-policy.pdf"`.
- **Reach ready.** Run `$S/wait-doc.sh <id>`. It prints `status=ready`. The saved JSON has `"pages": 7` and `"page_basis": "physical"` (from `facts.yaml`). `progress` reports only while a document is embedding, so it is `null` here.
- **Side effect.** Run `$S/sql.sh "SELECT count(*) AS chunks, count(embedding) AS embedded FROM chunks WHERE document_id = '<id>'"`. Both counts are equal and above zero.
- **Dedupe.** Upload the same file again. Status `200`, the same `id`. Then run `$S/sql.sh "SELECT count(*) FROM documents"`; the count is still 1.
- **Read a page.** Run `$S/call.sh GET /documents/<id>/pages/2`. The text contains `Property Damage Deductible $2,500 per occurrence` (fact `policy-deductible`).
- **Reject.** Make a small PNG or ZIP outside the repo and upload it. You get a 4xx with `error.code` set to `unsupported_type`, and the document count does not change.
- **Fail and retry.** Upload a PDF with blank pages only (make it outside the repo). `wait-doc.sh` exits 1, and the saved JSON has `error.code == "no_extractable_text"` and `failed_stage == "parse"`. `POST /documents/<id>/retry` returns 202 with `"status": "queued"`, and the document fails again with the same code.
- **All formats.** Upload `samples/sample-renewal-quote.pdf` (3 pages) and `samples/sample-endorsement-additional-insured.docx` (2 synthetic pages, `page_basis: synthetic`), and wait for both to reach ready.

## Gotchas

- Dedupe is by content hash, not filename. Renaming a file does not make it new; changing one byte does.
- On a warm stack the samples may already be ready. A "new upload" proof needs an empty stack or a file the vault has not seen.
- `wait-doc.sh` polls every 2 s. Processing with the fake provider takes seconds; a timeout points at the worker (check `doctor.sh` and `docker compose -p docvault-verify logs worker`).
- The DOCX has no physical pages. Its page numbers are synthetic sections, and the API says so in `page_basis`.
