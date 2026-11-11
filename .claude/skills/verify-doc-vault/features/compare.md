# Compare documents

`POST /comparisons` lines up 2 to 5 ready documents field by field in one model call. Every cell's quote is checked on the page it cites, and `differs` is computed in code from the values. The web UI shows the result as a table, one column per document, with each value's page tab opening the source pane.

## Sub-features

- `compare-run` (`POST /comparisons`) returns 201 with `documents` (the columns, with `deleted`, `pages_read`, `page_count`), `rows` (`field`, one cell per document, `differs`), `dropped_cells`, `model`, `prompt_version` (`compare-v1`) and `usage`.
- `compare-focus`: a `focus` of at most 200 characters keeps only the fields about it.
- `compare-refuse`: 422 `invalid_request` for fewer than 2 or more than 5 ids, a repeated id, or a focus over 200 characters. 404 `document_not_found` for an unknown id. 409 `documents_not_ready` with `details.not_ready` (`id`, `filename`, `status`) for a document still processing. 429 `rate_limited` from the ask budget.
- `compare-read`: `GET /comparisons/{id}` returns the stored comparison, and `GET /comparisons` lists them newest first. A deleted document keeps its column, cells and page labels, with `deleted: true`.
- `ui-compare`: the Compare section in the top bar, the new-chat screen's Compare button, and the document page's "Compare with..." all start one. The comparison screen marks differing rows with a revision bar and a "Differs" tag, filters to "Only differences", and opens a cell's page with its quote marked.

## How to get to it (user POV)

- `$S/call.sh POST /comparisons -H 'content-type: application/json' -d '{"document_ids": ["<policy>", "<quote>"]}'`.
- Web UI: the top bar's **Compare** link (`/?view=compare`). The new-chat screen (`/`), with two or more ready documents ticked, shows **Compare** beside **Ask**; the question box's text becomes the focus. A ready document's page shows **Compare with...**, which opens `/?view=compare&with=<id>` with that document ticked. A comparison lives at `/?compare=<id>`, a cell's page at `&cite=cmp.<row>.<col>` (0-based), and the filter at `&only=differences`.

## Driving it

Preconditions: a stack from `launch.sh`, with `sample-cgl-policy.pdf` and `sample-renewal-quote.pdf` uploaded and both `ready` (`$S/wait-doc.sh <id>`). Insights need not be ready: the prompt sends the page text either way.

- **API.** Compare the two. Expected values come from the `changes` and `unchanged` lists in `samples/facts.yaml`. On the fake provider the body has 12 rows and `dropped_cells` 0. "Property Damage Deductible", "General Aggregate Limit" and "Medical Expense Limit" have `differs: true`, with both cells on page 2 and quotes holding the planted text. "Each Occurrence Limit" has `differs: false`. Premium gets no row on the fake, because the two documents label it differently (`docs/evals.md`). `$S/sql.sh "SELECT purpose, comparison_id IS NOT NULL AS owned FROM llm_calls WHERE purpose = 'compare'"` shows one row billed to the comparison.
- **Focus.** The same with `"focus": "deductible"` returns the one deductible row.
- **Refusals.** Send one id, six ids, the same id twice, and a 201-character focus: each is 422. Upload a text file of about 5 MB and compare it with the policy while it is still `parsing` or `embedding`: 409 with that file in `details.not_ready`.
- **Deleted document.** Upload a text file with the lines `Named Insured:` and `Example Contractors LLC` on two lines, compare it with the policy, delete it, then `GET /comparisons/<id>`. Its column has `deleted: true`, and its cells keep their values and page labels.
- **UI, compare.** Open `/?view=compare`, tick the two samples (checkboxes named by filename), and click **Compare**. The status line reads "Comparing ..." until the URL becomes `/?compare=<id>`. The Property Damage Deductible row header has `data-differs` and a "Differs" tag. Click its first page tab, named "Property Damage Deductible in sample-cgl-policy.pdf: show the passage on p. 2". The URL gains `cite=cmp.<row>.0`, the tab has `aria-pressed="true"`, and the pane reads "Page 2 of 7" with the `mark` on "Property Damage Deductible $2,500 per occurrence".
- **UI, other entry points.** On `/`, tick both: the footer says Compare uses the question as its focus. Type `deductible` in the question box: **Compare** gives one row. On `/?doc=<policy>`, **Compare with...** lands on the compare screen with the policy ticked.
- **UI, filter.** Tick **Only differences (n)**. Only rows with the tag remain, and the URL has `only=differences`.
- **UI, deleted.** Open the deleted-document comparison. Its header marks the file "(deleted)", and its page tabs read "p. n, deleted" as text, not buttons.
- **UI, running and failed.** Hold the `POST **/api/comparisons` request with a Playwright route to see the running state, and abort it to see the failure with **Try again** and **Change the documents**. Label these runs MOCKED.
- Run `scripts/visible.js` at 1440x900, 1024x768 and 375x812 on the compare screen (with and without recent comparisons), a comparison with the pane open, "Only differences", and the running and failed states. Every run returns `[]`. At 375 px the field column and the first document's column are readable, and the table scrolls sideways inside its own box (`data-scroll-x`), never the page.
- Keyboard: from the top of the comparison screen, Tab reaches the filter and every page tab, each with the carbon focus ring.

## Gotchas

- The API cannot delete a comparison, so every proof run leaves its comparisons in the stack's database. Use a fresh `launch.sh` stack for runs that should start empty.
- Below 1100 px the source pane covers the centre column, so with the pane open the table's tabs sit under it. `visible.js` does not report overlap; close the pane to reach them.
- A fake comparison needs labels that two or more documents share. A throwaway file whose labels match nothing in the other documents gives zero rows, and the screen says no fields were found.
