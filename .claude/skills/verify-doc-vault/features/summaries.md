# Summary options

`POST /documents/{id}/summaries` writes a ready document's summary again at a length (`brief`, `standard`, `detailed`: about 60, 150 or 300 words), about a focus, and in a tone (`plain`, `formal`, `client`). Up to 6 sources are each checked on the page they cite. A summary is stored by document, normalised options, model and prompt version, so the same request again returns it with no model call. The web UI offers it as "Rewrite the summary" under each ready document's summary.

## Sub-features

- `summary-run` returns 201 with `id`, `document_id`, `length`, `focus` (normalised: trimmed, lowercased, spaces collapsed), `tone`, `text`, `sources` (`page`, `page_label`, `quote`), `dropped_sources`, `model`, `prompt_version` (`summary-v1`), `cached: false` and `created_at`.
- `summary-options`: each of length, focus and tone changes the text.
- `summary-cached`: the same request again, with the focus written differently but normalising the same, returns 200 with the same `id` and text, `cached: true`, and no new `llm_calls` row.
- `summary-refuse`: 422 `invalid_request` for a length or tone outside its values or a focus over 200 characters. 404 `document_not_found` for an unknown id. 409 `documents_not_ready` with `details.not_ready` for a document still processing. 429 `rate_limited` from the ask budget.
- `summary-list`: `GET /documents/{id}/summaries` lists them newest first, each `cached: true`. Deleting the document deletes its summaries.
- `ui-summary`: the "Rewrite the summary" disclosure, its result with a page tab per source, "Saved earlier, no new model call", the earlier rewrites, and a source cite that survives a reload.

## How to get to it (user POV)

- `$S/call.sh POST /documents/<id>/summaries -H 'content-type: application/json' -d '{"length": "brief", "focus": "deductibles", "tone": "formal"}'`.
- Web UI: open a ready document (`/?doc=<id>`). Under the summary paragraph, the **Rewrite the summary** disclosure opens the options. The action bar's **Rewrite the summary** button also opens it and moves focus to it. The options are the **Length** radio group (Brief, Standard, Detailed), the **Focus (optional)** field, the **Written for** select (Plain, Formal, For the client) and **Rewrite**. A source's page lives at `&cite=sum.<summary id>.<n>` (0-based) and `&p=<n>` pages away from it. While insights are pending or failed on a ready document, the disclosure sits under that note.

## Driving it

Preconditions: a stack from `launch.sh`, with `sample-cgl-policy.pdf` uploaded and `ready` (`$S/wait-doc.sh <id>`). Insights need not be ready.

- **API, run.** Post brief, focus `deductibles`, tone `formal` for the policy. On the fake provider: 201, `text` is `This note summarises sample-cgl-policy.pdf. DEDUCTIBLE Property Damage Deductible $2,500 per occurrence`, two sources, both page 2 with `page_label` `p. 2`, quoting `DEDUCTIBLE` and `Property Damage Deductible $2,500 per occurrence` (the planted deductible in `samples/facts.yaml`), and `dropped_sources` 0.
- **Cached.** Read `$S/sql.sh "SELECT count(*) FROM llm_calls WHERE purpose = 'summary'"`. Post the same with focus `"  Deductibles "` and tone `formal`: 200, the same `id` and text, `cached: true`, and the count unchanged. Count by purpose: the total moves with anything else the stack does.
- **Options.** On the fake, the tone sets the opening sentence (`sample-cgl-policy.pdf in brief.` for plain, `This note summarises sample-cgl-policy.pdf.` for formal, `Here is what sample-cgl-policy.pdf means for you.` for client), the length the number of passages after it (2, 4 or 8), and the focus their order (passages sharing its words first). Each new combination is 201 with one new `summary` row.
- **Refusals.** `"length": "short"`, `"tone": "casual"` and a 201-character focus are each 422. An unknown id is 404 on POST and GET. Upload a text file of about 5 MB and post for it while it is `parsing` or `embedding`: 409 with that file in `details.not_ready`.
- **List and delete.** `GET /documents/<id>/summaries` lists the policy's summaries newest first, each `cached: true`. Upload a text file with the lines `Named Insured:` and `Example Contractors LLC`, summarise it, delete it, then GET its summaries: 404.
- **UI, rewrite.** These steps use options the API steps do not (the client tone), so the first rewrite in the browser is fresh on the same stack. On `/?doc=<policy>`, open **Rewrite the summary**, pick **Brief**, type `deductibles`, choose **For the client** and press **Rewrite**. The live region reads "Rewritten.", the `summary` count goes up by one, and the sheet's heading reads "Brief, for your client, about “deductibles”" over `Here is what sample-cgl-policy.pdf means for you. DEDUCTIBLE Property Damage Deductible $2,500 per occurrence`. The earlier rewrites list it first with `aria-current="true"`.
- **UI, source.** Click "Source 1 of 2: show the passage on p. 2". The URL gains `cite=sum.<id>.0`, the tab has `aria-pressed="true"`, and the pane reads "Page 2 of 7" with the `mark` on "DEDUCTIBLE". Reload: the disclosure is open, the same tab is pressed and the pane is back.
- **UI, cached.** Close the pane, press **Rewrite** again with the same options: the live region and the sheet read "Saved earlier, no new model call.", and the `summary` count is unchanged.
- **UI, running and failed.** Hold the `POST **/api/documents/*/summaries` request with a Playwright route to see the running sentence ("Writing a detailed summary in a formal tone about “named insured”.", then after 5 s "A long document can take up to a minute and a half."), then answer it with a 503 `ai_unavailable` to see the failure with **Try again**. Label these runs MOCKED, and abort a held route before leaving the page, or the browser sends it on.
- Run `scripts/visible.js` at 1440x900, 1024x768 and 375x812 with the disclosure open, after a fresh result, after a cached result, with a source open, and in the running and failed states. Every run returns `[]`.
- Keyboard: Tab from the disclosure reaches the length group, the focus field, the tone select, **Rewrite**, each source tab and each earlier rewrite, each with the carbon focus ring.

## Gotchas

- Every summary POST, cache hits included, spends one of the client's 20 asks a minute, shared with asks and comparisons. A walk at three sizes needs pacing, or mocked POSTs for the running and failed states.
- The API cannot delete a summary on its own, so a proof run leaves its summaries until the document is deleted. A first request that must be fresh needs options the stack has not seen, or a fresh `launch.sh` stack.
- The fake's text is passages of the page, far under the word targets. It proves the options change the output, not how a model writes.
