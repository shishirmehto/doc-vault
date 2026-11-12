# Doc Vault web UI

A small Next.js app for the doc-vault API. You can upload documents and watch them process. Each document shows its summary, tags and key facts, and can be deleted from its page. A ready document's summary can be written again at another length, about one topic, or for the reader's client, and each quote in the new summary opens its page. You can also start a chat over one or more ready documents and ask questions. The answer appears as it is written, then the cited answer replaces it, with up to three follow-up questions to ask in one click. Click a key fact's page tab, or an answer's numbered citation, to open that page with the quoted passage marked and a flag on the page edge. In a chat, the flag carries the citation's number. You can also compare 2 to 5 ready documents side by side. Doc Vault lines up the fields a broker compares, such as limits, deductibles and premium, marks each field whose values differ, and gives every value a page tab that opens its passage. A file uploaded again under the same name with new content becomes the document's next version. The rail shows the newest version with a "v2" mark, and the document page links the other versions and compares this one with the next or previous one in one click. The top bar has three sections. Ask is the new-chat screen and its chats. Compare is the compare screen and its comparisons. Metrics shows how documents, steps, AI calls and chats are doing, with comparisons among the AI calls, the daily AI spending cap and the caches.

## Layout

The page itself never scrolls. `html`, `body` and the `.app` wrapper are flex and grid boxes down to the three columns, so each column scrolls on its own. Anything a browser extension adds to the page, or a margin it sets, takes its own share of the height instead of pushing content off the window. On the chat, new-chat and compare screens the content scrolls above a footer row that holds the question box or the focus field, and nothing is `position: sticky`. The question box stays on screen at every window height down to 500 px.

Below 1100 px the source pane covers the centre column. Below 760 px the rail folds into a "Documents (n)" button above the centre column. It is open while the vault is empty or uploads are listed, closes when a document is opened from it, and takes at most half the height, with its own scroll. Folded, it still lists up to three documents that are processing under the button, so a document uploaded from another tab shows its progress. Past three, an "N more processing" button opens the rail.

An empty vault shows the steps from upload to first question, with a large drop target in step 1. Over six documents, the rail shows a "Find a document" field, the new-chat picker a "Find a document to ask about" field and the compare picker a "Find a document to compare" field. Each matches the filename and the document type, except the type "other", which rows do not show. The picker's "Select all ready" ticks the ready documents the search shows, up to the 50 a chat allows. Each row's meta leads with the document type once insights know it ("Policy. PDF, 7 pages, 11 KB"). The rail and both pickers list one row per document, its latest version, and a version after the first carries a "v2" mark in the row meta, before the type. A screen reader hears "Version 2." for it.

A document page has one action bar under its header. It holds "Ask about this document", "Compare with...", "Rewrite the summary", "Read the pages" (or sections) and, when the family has more than one version, "Versions (n)". Then, set apart by space and in quieter graphite, come "Upload a new version" and "Delete". The bar may run the centre column's full width while the text keeps its 68ch measure, so it holds one row at 1440 px with the pane closed. It wraps at narrower widths, the file actions last. A document that is not ready offers only what works on it: "Read the pages" once its pages are read, and the file actions. "Rewrite the summary" opens the disclosure under the summary, moves focus to it and brings it up the column. Pressing it again leaves the disclosure open. A refused "Ask about this document" shows its reason under the bar.

A document with other versions has a version line under its header: "Version 2 of 2." with a link to each other version, then "Compare with version N". If the earlier ones were all deleted, it says so. "Compare with version N" compares an earlier version with the latest, or the latest with the version before it, and lands on the comparison. While either version is not ready, the button is `aria-disabled` and a line under the version line says why. An earlier version opens with a note, "A newer version of this document, version 2, was uploaded ...", and an "Open version 2" link. "Versions (n)" opens a list of every version with its file, upload time and status, the current one marked "this page". "Upload a new version" picks a file of any name and sends it with `replaces=<this id>`. Its progress shows in the rail, like any upload. The family is read once per page and kept current by the live feed, so a version uploaded or deleted elsewhere shows up on the page without a reload.

A chat keeps the document ids it started on, so it can ask an earlier version than the rail now lists. When a document's family has more than one version, the chat's header names the version, as in "Asking terms.txt (version 1)". A chat on an earlier version adds a note under the header, "This chat asks version 1 of terms.txt. A newer version of this document, version 2, was uploaded ...", with an "Open version 2" link, the same note an earlier version's page shows. A chat document the list lacks is read on its own, and that read follows the live events that name it.

`.claude/skills/verify-doc-vault/scripts/visible.js` checks a screen for clipped text, controls nothing can scroll into view, and sideways page scroll. The verify skill's `features/README.md` says how to run it.

## Screens

The top bar's sections lead to three screens, and every other screen is reached from one of them. The URL holds the screen, so Back, Forward and a pasted link all work. The parameters are listed under Where things live.

| Screen | URL | Ways in |
|---|---|---|
| Ask: the new-chat screen, or the empty-vault steps | `/` | Top bar "Ask", the wordmark, or "New chat" in a chat |
| A chat | `?chat=<id>` | Ask on the new-chat screen, Recent chats, or "Ask about this document" |
| Compare | `?view=compare` | Top bar "Compare", "Compare with..." on a document page, or Compare on the new-chat screen |
| A comparison | `?compare=<id>` | Compare, Recent comparisons, or "Compare with version N" |
| A document | `?doc=<id>` | A rail row, a version link, or "Open version N" in a chat or on an earlier version |
| Metrics | `?view=metrics` | Top bar "Metrics" |
| Not found | An unknown or malformed id, or another path | A stale link. The page says what is missing and links back |

## Run it

With the whole stack:

```bash
docker compose up --build        # the UI is on http://localhost:3000
```

Against an API that already runs on port 8000:

```bash
cd web
npm ci
npm run dev                      # http://localhost:3000
```

The browser calls only `/api/*` on the Next.js server. Next.js rewrites those calls to `API_ORIGIN`, which defaults to `http://localhost:8000`. The rewrite target is fixed at build time, so `API_ORIGIN` is a build argument. Compose passes `http://api:8000`, and setting it on a running container has no effect.

Two proxy limits are raised in `next.config.ts`:

- `proxyClientMaxBodySize` is 60 MB. The default 10 MB would truncate the API's 50 MiB uploads.
- `proxyTimeout` is 120 s, so a slow model answer can finish.

The rewrite also compresses responses, and a compressed `text/event-stream` arrives in one burst when the answer ends. The API marks the stream `Cache-Control: no-cache, no-transform`, and the proxy's compression skips such a response, so the streamed answer needs no route handler of its own. Measured in a browser on port 3000, the deltas of a fake answer arrived at 61, 87, 117 and 144 ms and the final turn at 546 ms. The document event stream takes the same rewrite with the same headers.

## Scripts

| Script | What it does |
|---|---|
| `npm run dev` | Development server on port 3000 |
| `npm run build` | Production build (`output: "standalone"`) |
| `npm run lint` | ESLint with `eslint-config-next` |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Unit tests on Node's built-in runner. Node strips the TypeScript types |
| `npm run gen:api` | Regenerates `src/api/schema.d.ts` from the running API's `/openapi.json` |
| `npm run check:api` | Runs `gen:api`, then fails if `schema.d.ts` changed |

## Where things live

| Path | Contents |
|---|---|
| `src/api/schema.d.ts` | Wire types generated from the API. Committed and never edited by hand |
| `src/api/parse.ts` | The only place where wire types become domain types. It also parses the URL selection |
| `src/api/client.ts` | One function per endpoint. Each returns `Result<T>` and never throws. Uploads use XHR for progress. `askStream` reads the streamed ask with `fetch`, because `EventSource` cannot POST. Every `fetch` and the upload XHR send a fresh `X-Request-ID` (`crypto.randomUUID()`), and a failure records the id the API echoed, or the one it sent when nothing was echoed. A streamed ask's `error` event takes the id of its 200. `EventSource` cannot send headers, so the document event stream sends none |
| `src/api/sse.ts` | Frames a `text/event-stream` body into messages, from text split anywhere |
| `src/api/domain.ts` | Types the components read: branded ids, the lifecycle, insights and turn unions, `SourceRef` |
| `src/lib/` | Logic with no React, tested on Node: `locateQuote`, `isSettled`, `stageProgress`, the upload scheduler, the chat thread's state machine, `follow.ts`, which says when the chat stops following a streamed answer, `findDocs` and `chooseAll` for long lists, the rail list's rules (`docList`), `ListSync`, which keeps a list read, or a document family's, and the events around it consistent, `LiveFeed`, which reopens the event stream and says when it is paused, `compare.ts`: the comparison run's state machine, the column order, `withDoc` (a linked document older than the newest 100 joins the picker), the "Only differences" rows and the cell a URL names, `comparisonPane.ts`, which turns a comparison cell into what the source pane shows, `citedPage.ts`, which opens a cited page with its passage marked until the reader pages away, `summary.ts`: a rewritten summary's lengths and tones, `normaliseFocus` (the backend's rule), the rewrite's state machine, `mergeSummary`, `shownSummary` and `paneList` (which summaries the source pane resolves a cite against while the earlier rewrites load or fail), and `summaryPane.ts`, which tells a key fact's cite from a summary source's and turns a summary source into what the source pane shows, and `versions.ts`: the "v2" mark, which version "Compare with version N" uses, the newer version an earlier one links, and a family kept current by events |
| `src/hooks/` | `useDocuments` (the list, kept current by the live feed, with polling as the fallback), `useLiveFeed` (the page's one event stream and its status), `useFetched`, `useListedDoc`, which gives a screen the listed document or reads one the list does not hold, `useSelection`, `useUploads`, `useHealth` (one shared `/health` poll), `useFileDrop`, `useThread`, which sends one question at a time per chat, `useCompareRun`, which holds the one comparison in flight, `useRewrites`, which holds a document's rewritten summaries, the rewrite in flight and the form's state, `useVersions`, which reads a document's family and follows its events, `useLongRun`, which says when a comparison or a rewrite has run long enough to say the document is long, and `useWorthRetrying`, which says whether a failure offers Try again and renders again when a used-up spending cap resets |
| `src/components/` | The top bar, the rail, the new-chat screen, the document picker it shares with the compare screen, the chat, the compare screen, the comparison, the document view, its delete dialog and its "Rewrite the summary" disclosure (`SummaryRewrite`), its version line, newer-version note, versions list and "Upload a new version" control (`Versions`), the "v2" mark (`VersionMark`), the source pane, the metrics screen, and the missing-page message |
| `src/copy.ts` | Every sentence the UI writes about state |

The URL holds the selection:

- No parameters shows the new-chat screen. Only ready documents can be chosen. Below the picker are the five newest chats.
- `?view=metrics` shows the metrics, for the last 24 hours. `&window=7d` shows the last 7 days. A contents link adds `#metrics-<section>`, which only scrolls the column.
- `?doc=<id>` opens a document. `&cite=fact.<n>` or `&p=<n>` opens the source pane. `&cite=sum.<summary id>.<n>` opens source `n` of a rewritten summary, counting from 0, and `&p=<n>` pages away from it.
- `?chat=<id>` opens a chat. `&cite=turn.<turn id>.<n>` opens citation `[n]` of that turn, and `&p=<n>` pages away from it.
- `?view=compare` shows the compare screen. Only ready documents can be ticked, at most 5. Below the picker are the ten newest comparisons. `&with=<doc id>` ticks that document, which is how a document page's "Compare with..." opens it.
- `?compare=<id>` opens a comparison. `&cite=cmp.<row>.<col>` opens the page of one cell, counting rows and columns from 0 among all the rows. `&p=<n>` pages away from it, and `&only=differences` shows only the fields that differ.

A `?doc=` or `?chat=` the vault does not have, or one that is not an id at all, shows a sentence saying so and an "Ask your documents" button. A `?compare=` that is not an id, or that the vault does not have, shows the same kind of sentence with a "Compare documents" button. Any other path shows the app's own not-found page.

Deleting a document asks first, in a dialog that names the file and says that chats which used it keep their answers and citations but take no more questions. For a document with other versions, it adds that they stay in the vault with their numbers. Afterwards the home screen opens with a note naming the file, and the note takes focus.

Starting a chat, opening a fact, opening a citation, opening a comparison's cell and opening a rewritten summary's source each add a history entry, so Back undoes them. A pasted link reopens the same chat or highlight in any browser, because the chat's header comes from `GET /chats/{id}`. If a chat's document was deleted after it started, the header marks it and the composer closes, since the API would refuse the question.

A file larger than `/health`'s `max_upload_bytes` is refused in the browser and never sent. Until `/health` answers, the API's 413 decides.

The document list follows `GET /events/documents` through one `EventSource` for the page, open while the list is on screen. A document uploaded, retried or deleted from another tab or client shows up in the rail and moves through its stages with no reload, and the list is not polled while the stream is open. A `deleted` event removes the row, and a tab showing that document shows the missing-document page. The list is read again each time the stream opens and on a `resync` event. Every event applied after a list read starts is applied again to the read's result, so the result loses neither an event it already holds nor one it missed. While the stream is down, polling takes over: every second while a document can still change, and 5 s after a failed read. A failed read is also retried after 5 s while the stream is open, since nothing else would bring the list. When the browser gives up on the stream, after a non-200 answer such as the 503 past 100 streams or a proxy error while the API restarts, the page opens it again after 2 s, doubling to 30 s. After 3 s without the stream, the top bar reads "Live updates paused, retrying". The server's first frame sets `retry: 1000`, so the browser retries a dropped stream after 1 s, before the note would show.

A hidden page (a background tab or a minimised window) closes its stream and opens a new one when shown, which reads the list again. Over HTTP/1.1 a browser allows six connections to one host across all its tabs, so streams held by background tabs would leave the visible one none for its requests. When the rail holds the newest 100 and a delete leaves 99, the list is read once to bring in the next document. A document older than the newest 100, opened by its link, is read again on each event that names it, and on every reconnect. So is an earlier version, which the rail never lists. A chat whose cited document the rail does not list reads that document by itself, so its citations still open their pages.

The rail asks for `GET /documents?latest=true`, so the newest 100 are the newest 100 documents, each counted once whatever its number of versions. Live events keep one row per family. A `document` event for a version that is not its family's latest removes that version's row and adds nothing. A `document` event for a version that names itself latest replaces the family's row, even a higher version's. When version 2 is deleted, version 1's event carries `latest_version` 1, and the rail lists version 1 again with no list read, whether that event comes before or after version 2's `deleted`. Version 1 read just before version 2 was uploaded also names itself latest, and takes the row for a moment. Version 2's own event always comes after it, because the API reads and sends events in order, and it takes the row back.

The metrics screen opens with a contents line, "On this page: Documents, Processing, AI calls and cost, Chats, Spending cap, Caches.". Each link scrolls the column to its section's heading and moves focus there. Documents reads `/metrics/documents`. Processing, AI calls and cost, and Chats read `/metrics/processing` for the window chosen beside Processing, and each of their leads starts with when the window starts. Each section loads and fails on its own with Try again, so one failing route leaves the others on screen with their numbers. Refresh reads all of them again. Under the fake provider, the cost reads "fake provider, no cost".

The last two sections are these.

- **Spending cap** reads `/health`. With `DAILY_AI_BUDGET_USD` set, one table gives the cap per day (UTC), what was spent today, what is left (never below $0) and when it resets, at the next 00:00 UTC in the reader's time. Amounts keep the decimals they need, so a cap reads `$0.000001` or `$5.00`. At or over the cap, a red line above the table says that asking, comparing, rewriting and new insights stop until the reset, and that uploads are still indexed. With no cap, it says so and names `DAILY_AI_BUDGET_USD`. While `/health` reports the database unreachable, it says the cap cannot be read, because `/health` then has no cap to give.
- **Caches** reads `processing.cache`: hits, misses and hit rate for embeddings and for chats' first answers, with a dash for a cache with no lookups, which a screen reader hears as "no lookups". The lead line says the counts run from `countedSince`, in the reader's time, and do not follow the window chosen above. The cache keeps nothing on disk and may drop any key to make room, so the counts start again from zero after a restart or an eviction, and both the lead line and "nothing counted yet" say so. When the figures are null, `/health`'s `cache` says why: `off` names `CACHE_URL`, and `unreachable` says every request goes to the model and still works.

Questions go to `POST /chats/{id}/messages/stream`. While the answer streams, the turn shows its provisional text, without citations, under a status line. It reads "Sending the question" until the server names its first step, then follows the steps in the order the server sends them: "Reading the follow-up against the conversation" (follow-ups only), "Searching N documents", "Writing the answer", and "Checking citations" once no text has arrived for 300 ms. A `reset` event clears the text. The `turn` event replaces it with the grounded answer. The latest turn's suggestions show as buttons only while no question is in flight, and a click asks one at once.

A chat's first question that was answered before on the same documents comes back from the answer cache. The turn's usage line then reads "Saved earlier, no new model call." in place of its calls, tokens and cost, on the streamed answer and in history alike.

When today's AI spending cap is used up, Ask, Compare and Rewrite the summary say so in one sentence: "Today's AI spending cap of $5.00 is used up. Asking, comparing and rewriting start again at 05:30 on 13 Nov, when it resets." The cap keeps its own decimals, and the reset is the API's `resets_at` in the reader's time. None of the three offers Try again before the reset, and a timer brings Try again back at the reset without a reload. A raised cap needs a reload to show. The chat's streamed ask meets the cap in two ways: a 429 before its stream starts, or an `error` event inside a stream that started, when the cap is reached between the check and the answer. Both reach the same sentence. The failed ask keeps "Edit the question".

A failure from the server itself ends with "Reference <request id>.", so a reader can quote it and an operator can find the request's log line, its worker lines and its `llm_calls` rows. That covers a `broken` response, a refusal with status 500 or more, such as `internal_error`, and an `error` event inside a streamed ask other than the cap's, which carries the stream's 200 and its id. A long id wraps rather than clips. The notes under a list that reads itself again ("... Retrying." in the rail, the new-chat picker and the compare picker) leave the reference out, because each read sends a new id and the note would change, and be announced again, on every retry.

Insights that hit the cap need nothing new. The document page shows them as a failed summary with the backend's sentence, "Insights were not written because today's AI spending cap of $0.000001 is used up. Retry after midnight UTC.", and a quiet "Retry anyway", because the backend marks them final.

When an ask fails, the chat reads its history again before it offers Try again. If the question was recorded after all, the turn is shown and nothing is asked twice. An `error` event takes this path too: it is the backend's verdict, so Try again follows when history lacks the turn. A stream that the backend accepted with a 200 and that ends without a `turn` or an `error` event is different: a connection dropped mid-answer, or an event the web build cannot read. The backend still answers and stores the turn, possibly minutes later, so no read of history can show it never will. The chat reads history again after 0, 1, 2, 4, 8 and 15 seconds, then says the answer did not arrive and that it is still checking, offers Check again, and reads history every 30 s for 9 more minutes, past the backend's longest answer at its default timeouts. It adopts the turn as soon as it appears and never offers Try again for that question; a reload starts the chat afresh. In a browser check, a stream cut after its first delta was adopted from the 1-second read, with no Try again.

One comparison runs at a time, in a store outside React, so the run outlives the screen that started it. The compare screen starts one, and so does the new-chat screen's Compare button, which uses the question as the focus and opens the compare screen. While it runs, the compare screen shows "Comparing a.pdf and b.pdf" with the ask's progress line, and after 5 s says that a long document can take up to a minute and a half. When it lands, the compare screen's URL is replaced by the comparison's, so Back skips the compare screen and returns to where Compare was clicked. A reader who has moved to another screen stays there, and the comparison heads Recent comparisons. When a run fails, the screen says why, names any document that is not ready, and offers Try again, which sends the same request, and "Change the documents", which returns to the picker with the ticks and focus kept. Try again is left out for a refusal the backend marks final, other than documents that are not ready yet.

A comparison's table scrolls sideways in its own box. Each field that differs carries a bar on the left edge of its field cell and a "Differs" tag. A page tab opens its cell's page in the source pane with the passage marked, and the cell turns canary. A deleted document keeps its column and values, but its page tabs become plain text, such as "p. 2, deleted". The "Only differences" checkbox replaces the URL instead of adding a history entry. A comparison that found no field in two or more of its documents says what to try under its header: a focus or documents that state the same fields, a broader focus or none when it had one, or a new comparison when one of its documents was deleted.

A ready document has "Rewrite the summary" under its summary, or under the note that the summary is loading, is still being written, failed, or could not be read. It opens on a form with the length (Brief, Standard or Detailed, about 60, 150 or 300 words), an optional focus, and who the summary is written for (Plain, Formal or For the client). One rewrite runs at a time per document. While it runs, the form says what it is writing, and after 5 s it says that a long document can take up to a minute and a half. A failure gives the backend's reason and Try again, which sends the same options and moves focus to Rewrite. Try again is left out for a refusal the backend marks final, such as a deleted document or invalid options. Changing an option clears the failure. The result shows on white paper with a page tab per source, and a tab opens the source's passage in the source pane, as a key fact's does. A summary with no sources says whether the model gave none or none of its quotes were on their pages, and to treat it as an overview. One whose backend left quotes out says how many. Asking again with the same options returns the stored summary without a model call, marked "Saved earlier, no new model call". Focus stays where it was, and a screen reader hears "Rewritten." or the saved note. Below the result, "Earlier rewrites" lists the document's newest 100 and this visit's results, most recently shown first, and a click shows one. The open source always belongs to the summary on screen, so showing another summary, or a new result landing, closes the pane. If the earlier rewrites fail to load, the list says why and offers Try again, which moves focus to the "Earlier rewrites" heading. A link with a rewritten summary's cite opens the form on that summary with its tab pressed. A cite of a summary made in this visit opens its passage even while the earlier rewrites load or after they fail. The form's state lives in the document screen. The insights panel keeps the disclosure as one node while the insights load, fail or land, so moving it under the summary keeps both its state and the focus in it.

While an answer streams, the column follows its text until the reader scrolls up. When the answer lands, the column scrolls to the end, so the suggestions under it sit fully above the question box.

## Tests and fixtures

`src/api/fixtures/` holds responses captured from the running API on 2026-11-11, with their HTTP status. They include a real failed document, made by uploading a textless PDF. Each document state from queued to ready is captured from one text upload. The insights states captured are pending, ready, ready with partial coverage, and unavailable. The chat fixtures come from the fake provider: a cited answer, a follow-up with its `interpreted_as`, a two-document answer, `no_evidence`, `model_declined`, a chat's history, the 409 `documents_not_ready` with filenames, a 404 and a 422. Phase 3 added a chat read back with a deleted document and its `documents_deleted` 409, the recent-chats page, a 429 `rate_limited` with `Retry-After`, `/health` with `max_upload_bytes`, and both metrics routes, including an empty window whose percentiles are null. `fixtures/constructed/` holds the responses no request could reach: failed insights, a 503 with `Retry-After`, an `unverified` turn and an answered turn with no citations. Its README says so.

At bonus phase A, `TurnOut` gained `suggestions`, so the `turn-*` and history fixtures were recaptured with the same questions over the same documents, and `ask-stream-deductible.txt` holds the raw body of a streamed ask. The stream tests read it split at every byte.

At phase G1, `DocumentOut` gained `document_type`. `documents-page.json` was captured again. The other document fixtures are snapshots of states that have passed, so the field was added as the API writes it: null while insights are not ready, and, for the two with ready insights, the value `GET /documents/{id}` returned. `delete-404-not-found.json` is a real `DELETE` of an id the vault does not have.

At bonus phase B, `events-documents.txt` holds the raw body of `GET /events/documents` read through the proxy on port 3000 while a 2.5 MB text file processed. It has the `: connected` comment, six `document` events from parsing through 128, 512, 896 and 1280 of 1604 passages to ready, a keepalive, a `resync` and the `deleted` event. The event tests read it split at every byte. The feed's reconnect delays and pause timing are tested against a fake `EventSource` with Node's mock timers.

At bonus phase C, the comparison fixtures were captured from the fake provider with a script that uploads two throwaway text files and deletes them again:
- `compare-201-policy-quote.json`: the policy and the renewal quote, 12 fields, 5 of which differ;
- `compare-201-focus-deductible.json`: the same with the focus `deductible`, which leaves one field;
- `compare-201-three-documents.json`: the policy, the quote and the endorsement, where the endorsement's empty cells are null;
- `comparisons-page.json`: `GET /comparisons?limit=10`, which includes a comparison with no rows;
- `comparison-get-deleted-document.json`: a comparison with a throwaway text file, read back after the file was deleted;
- `compare-409-not-ready.json`: a comparison with a large text file posted while it was still queued;
- `compare-422-one-document.json` and `comparison-404-not-found.json`.

The tests build the malformed bodies by editing a captured one: a row with a missing cell, cells in the wrong columns, and a cell with a value but no page or quote.

At bonus phase D, the rewritten-summary fixtures were captured from the fake provider with a script that uploads two throwaway text files and deletes them again. No summary was made of a sample.
- `summary-201-brief-focus-client.json`: a brief summary for the client, sent with the focus `"  Property   DAMAGE\tDeductibles "` and stored as `property damage deductibles`, with its two sources on sections 2 and 3;
- `summary-200-cached.json`: the same request again, which answers 200 with the same summary and `cached: true`;
- `summaries-page.json`: `GET /documents/{id}/summaries?limit=100` after three summaries, newest first;
- `summary-409-not-ready.json`: a summary of a large text file posted while it was still queued;
- `summary-422-length.json` (`length: "short"`) and `summaries-404-not-found.json` (a list for an id the vault does not have).

The fake grounds every source, so the tests build a summary with no sources, and the malformed bodies, by editing a captured one: an unknown length or tone, a source on page 0, and a `cached` that is not a boolean.

At bonus phase E, `DocumentOut` gained `family_id`, `version` and `latest_version`, and a compared document gained `version`. The stored fixtures gained the fields as the migrated API returns them, and five fixtures were captured with a script that uploads two throwaway text files under one name and deletes them again: `upload-202-version-2.json`, `document-version-1-of-2.json`, `versions-two.json`, `versions-404-not-found.json` and `upload-404-replaces-unknown.json`. `fixtures/constructed/README.md` says which files were edited and how. The parse tests refuse a version above its `latest_version`, a version below 1, and a `family_id` that is not a UUID.

At phase F, `TurnOut` gained `cached`, `HealthOut` gained `ai_budget` and `cache`, and `ProcessingMetricsOut` gained `cache`. The cached turn, its history, its stream (`ask-stream-cached.txt`), `health.json` and `metrics-processing-24h.json` were captured from the dev API at `b5c649c` with the cache on. A stack with `DAILY_AI_BUDGET_USD=0.000001` and one priced ledger row gave the `*-429-quota-exceeded.json` bodies with their `retry-after` and `x-request-id` headers, the health under and over the cap, the capped processing metrics, and a document whose insights failed with `quota_exceeded`. The stored turns gained `"cached": false` and the one-hour metrics `"cache": null`. `fixtures/constructed/README.md` lists those edits and the bodies no request on a healthy stack reaches: the `error` event inside a stream, a 500 `internal_error`, `/health` with the cache off and unreachable, and processing metrics with `cache: null`. The tests read request ids back from the captured headers. `copy.zone.test.ts` sets `TZ` to India before it loads `copy.ts`, so the reset reads 05:30 there on any machine. The reset timer is tested with Node's mock timers.

The `locateQuote` cases come from the backend's `tests/test_pages.py`. They were checked against the Python `locate_quote` on the same inputs.
