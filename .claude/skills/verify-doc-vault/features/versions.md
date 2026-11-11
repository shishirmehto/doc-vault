# Document versions

A file uploaded again under a stored filename with new bytes becomes the next version of that document. `replaces=<id>` does the same for a file with any name. Each version is a document of its own, with its own id, indexing, insights and summaries. The web UI lists the latest version of each document, says which version a page shows, links the others, and compares two versions in one click.

## Sub-features

- `version-new`: `POST /documents` with new bytes under a stored name returns 202 with `version` 2, the first upload's `family_id` and `latest_version` 2. The first upload now reads `latest_version` 2.
- `version-dedupe`: the same bytes again return 200 with the existing document, whatever the name or `replaces`.
- `version-replaces`: `POST /documents?replaces=<id>` makes a file with another name the next version. An unknown id is 404 `document_not_found`, and a value that is not a UUID is 422.
- `version-list`: `GET /documents?latest=true` lists each family once, at its highest version. `GET /documents/{id}/versions` lists the family oldest first. An unknown id is 404.
- `version-delete`: deleting a version leaves the others with their numbers.
- `version-live`: uploading or deleting a version sends a `document` event for every other version of its family, carrying the new `latest_version`.
- `ui-versions`: the rail's "v2" mark, the document page's version line, the newer-version note, "Compare with version N", "Versions (n)" and "Upload a new version".

## How to get to it (user POV)

- `$S/call.sh POST /documents -F file=@<file>` twice with one filename and different bytes. `$S/call.sh POST "/documents?replaces=<id>" -F file=@<other name>`.
- `$S/call.sh GET "/documents?latest=true&limit=100"` and `$S/call.sh GET /documents/<id>/versions`.
- Web UI: upload the changed file through the rail (**Choose files**, or drop it on the rail), or click **Upload a new version** on a document's page. The rail row shows "v2" in its meta line. The page of a document with other versions reads "Version 2 of 2." with a link per other version. An earlier version opens at `/?doc=<v1 id>` from those links. The action row holds **Compare with version N**, **Versions (n)** and **Upload a new version**.

## Driving it

Preconditions: a stack from `launch.sh`. Make two throwaway text files with one name in separate folders, so the fake comparison has shared labels to align. Each label sits on its own line with its value on the next, and passages are separated by blank lines:

```text
Named Insured:
Example Contractors LLC

Policy Number:
TERMS-0001

Property Damage Deductible:
$2,500 per occurrence
```

The second file is the same with `$5,000 per occurrence`. Put a run tag in the text (for example `Policy Number:` `TERMS-<tag>`), so a rerun is not a duplicate of an earlier run's bytes.

- **API, new version.** Upload the first file: 202, `version` 1, `latest_version` 1, `family_id` equal to `id`. Upload the second under the same name: 202, `version` 2, the first's `family_id`, `latest_version` 2. `GET /documents/<v1>` reads `latest_version` 2. Upload the second again: 200, the same id.
- **API, replaces.** Upload a third file under another name with `?replaces=<v1>`: 202, `version` 3, the same family. `?replaces=` with a random UUID: 404 `document_not_found`, and `GET /documents?latest=true` shows no new row. `?replaces=family`: 422.
- **API, lists.** `GET /documents?latest=true` holds version 3 and neither earlier version. `GET /documents/<v1>/versions` returns three items with `version` 1, 2, 3.
- **API, delete.** Delete version 1. `GET /documents/<v2>` still reads `version` 2. The next upload into the family is `version` 4, because 3 is the highest left.
- **Live.** Hold `curl -N $API/events/documents` open, then upload a new version. The stream carries one `document` event for the new version and one for each other version of the family, each with the new `latest_version`. Deleting the newest sends `deleted` for it and `document` events for the rest, with `latest_version` back down.
- **UI, rail.** Use a fresh pair of files with another tag. Open `/` and upload the first file, then the second, through the rail's **Choose files** input (`.rail input[type=file]`). The rail keeps one row for the name, and its meta line starts with "v2". `scripts/visible.js` returns `[]`.
- **UI, latest.** Open that row. Under the header: "Version 2 of 2." and a link "version 1". The action row has **Compare with version 1**, **Versions (2)** and **Upload a new version**. **Versions (2)** opens a list of both, with version 2 marked "this page".
- **UI, earlier.** Click "version 1". The URL becomes `/?doc=<v1>`. A note above the summary reads "A newer version of this document, version 2, was uploaded ..." with an **Open version 2** link. The note is not under any bar.
- **UI, compare.** Click **Compare with version 2** (or **Compare with version 1** on the latest). The URL becomes `/?compare=<id>`. The heading names "(version 1)" and "(version 2)", the column headers carry "v1" and "v2", and the Property Damage Deductible row has a "Differs" tag with $2,500 and $5,000. While either version is still processing, the button is `aria-disabled` and the line under the action row says which one is not ready.
- **UI, live.** Keep the rail open in one tab and version 1's page in another. Upload a version 3 with curl: the rail row's mark turns "v3" and version 1's note names version 3, with no list read in the network log. Delete version 3: the rail shows version 2 again.
- **UI, upload a new version.** On a page, **Upload a new version** opens a file picker. A file of another name lands as the family's next version, and its row replaces the family's row in the rail.
- Run `scripts/visible.js` at 1440x900, 1024x768 and 375x812 on the rail with a "v2" row, the latest version with **Versions** open, an earlier version with its note, and the comparison reached from **Compare with version N**. Every run returns `[]`.
- Keyboard: from the document header, Tab reaches each version link, **Compare with version N**, **Versions (n)**, **Upload a new version**, and on an earlier version **Open version N**, each with the carbon focus ring.

## Gotchas

- A version is linked by filename, ignoring case. A throwaway file named like one from an earlier run joins that run's family. Use a run tag in the filename too.
- The API cannot delete a comparison, so the compare step leaves one in the stack's database.
- At 375 px the rail is folded under "Documents (n)". Open it to see the "v2" row.
- `.rail .rows > li` also matches the rail's loading skeleton. Wait for `.rail a.row-name` before uploading through the page, or the upload may start before the page has hydrated.
- The "v2" mark's screen-reader text is visually hidden and positioned against the mark. If `visible.js` reports a long page or `clipped body` at 375 px only with a "v2" row on screen, check that `.version-mark` still has `position: relative`.
