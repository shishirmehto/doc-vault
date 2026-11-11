# Web UI

The Next.js app on the `web` service shows the vault in a browser. A user uploads files and watches them process, reads a document's insights, chats with one or more documents, and opens any key fact or citation to its page with the quote highlighted. The URL holds what is open, so Back closes the page and a pasted link reopens it.

## Sub-features

- `ui-upload` uploads picked or dropped files, at most three at a time. A duplicate shows "Already in the vault" and an unsupported file shows the API's message.
- `ui-status` shows each document with a three-part stage bar, polled until it settles. A failed document shows its reason and "Retry anyway".
- `ui-insights` shows the summary, tags and key facts, each with its page. A long document shows its coverage ("Based on the first N of M sections").
- `ui-chat` starts a chat from ticked ready documents, or from "Ask about this document", and shows cited answers with numbered sources, "Read as" for follow-ups, and a not-found state.
- `ui-chat-stream` shows the answer as it is written, under a status line ("Searching 2 documents", "Writing the answer", "Checking citations"), then swaps in the cited answer.
- `ui-suggestions` offers the latest turn's follow-up questions as buttons. A click asks one at once.
- `ui-citation-open` opens a key fact or a source on its page with the quote marked and focused. Back closes it, and a pasted link reopens it.
- `ui-metrics` shows the two metrics endpoints. Figures never wrap, and a table wider than its column scrolls inside its own box.
- `ui-delete` deletes a document from its page, after a dialog that names the file, then opens the home screen with a note.
- `ui-find` filters the rail and the new-chat picker over six documents. "Select all ready" ticks the ready documents shown.
- `ui-missing` shows a plain sentence and "Ask your documents" for an unknown path, and for a `?doc=` or `?chat=` the vault does not have.
- `ui-layout` keeps the question box on screen as a footer row at every height down to 500 px, folds the rail into "Documents (n)" below 760 px, and shows numbered first steps for an empty vault.

## How to get to it (user POV)

- Open `http://localhost:13000` (verify stack) in a browser. Prove a UI change at 1440x785, 1024x768 and 375x812, and run `scripts/visible.js` on every screen at each size (see `README.md`).
- Top bar: the "Doc Vault" link, the "Ask" and "Metrics" links (the current one has `aria-current`), and the service note. Under the fake provider, the note is a "Fake AI provider" button that opens its explanation.
- Rail: the drop zone, the "Choose files" button, one link per document, and over six documents a "Find a document" searchbox. Below 760 px the rail sits behind a "Documents (n)" button with `aria-expanded`.
- The empty home screen: three numbered steps, with a second "Choose files" button in step 1.
- The document view: the "Read the pages" and "Ask about this document" buttons, a "Delete" button, and one button per key fact, named "<label>: show the passage on p. N".
- The new-chat screen: the document checkboxes, "Find a document" and "Select all ready" over six documents, a "Question" textbox and an "Ask" button in the footer row.
- The chat: a "Question" textbox and an "Ask" button in the footer row. Each source is a button named "Source N: <file>, p. N". Suggestions are buttons named "Ask: <question>" in a group named "Suggested questions".
- The page pane: "Previous", "Next" and "Close".

## Driving it with Playwright

Preconditions:

- A baseline stack launched with `web` (the default for `launch.sh`), with the three samples uploaded and ready (see `upload-and-indexing.md`).
- The browser is at 1440x785 for the steps below, then at 1024x768 and 375x812 for the layout checks.

- **Upload.** Choose "Choose files" and pick `samples/sample-renewal-quote.pdf`. If it is new, its row moves from "Waiting to start" through "Reading pages" to "Ready". If it is a duplicate, it reads "Already in the vault".
- **Open insights.** Click the `sample-cgl-policy.pdf` link. The URL gains `?doc=<id>`, and the view lists key facts with page chips.
- **Open a fact.** Click the button "Each Occurrence Limit: show the passage on p. 2". The URL gains `&cite=fact.<n>`, and the pane shows "Page 2 of 7". There is one `mark` whose text, whitespace collapsed, is `Each Occurrence Limit $1,000,000`, and it has focus.
- **Back.** Go back. The URL returns to `?doc=<id>` and no `mark` remains.
- **Chat.** Tick the checkboxes for `sample-cgl-policy.pdf` and `sample-renewal-quote.pdf`, type `What is the property damage deductible?` in "Question" and press Enter. The URL becomes `?chat=<id>`. The answer lists source 1 `sample-cgl-policy.pdf, p. 2` and source 2 `sample-renewal-quote.pdf, p. 2`.
- **Watch it stream.** Before pressing Enter, start a `MutationObserver` in the page that records, with `performance.now()`, the text of `.answer.provisional` and of the busy turn's `[role="status"]`. The log shows the status "Writing the answer" while the text grows a word about every 25 ms, then "Checking citations", then the provisional text gone and the cited answer in its place. A screenshot alone is not proof: the fake writes the whole answer in under a second.
- **Ask a suggestion.** Click the first button in "Suggested questions". The question appears as a new turn at once, streams, and comes back answered with a source. New suggestions replace the old ones.
- **Open a source.** Click "Source 2: sample-renewal-quote.pdf, p. 2". The URL gains `&cite=turn.<turn id>.2`, the pane shows "Page 2 of 3", and the `mark` text is `Property Damage Deductible $5,000 per occurrence`.
- **Recent chats.** Open `/`. The new-chat screen lists the newest chats from `GET /chats`, each named by its documents. Click one and the URL becomes `?chat=<id>`.
- **Metrics.** Click the "Metrics" link in the header. The URL becomes `?view=metrics`. The document counts match `GET /metrics/documents`, the "Questions" table matches `chat.outcomes` from `GET /metrics/processing?window=24h`, and under the fake provider the cost reads "fake provider, no cost".
- **Delete.** Upload a throwaway file (never a sample: other runs use their ids) and start a chat on it with `call.sh`. Open the file's page, click "Delete", and check the dialog names the file and that focus is on "Cancel". Press Escape: the dialog closes and the URL is unchanged. Click "Delete", then the dialog's "Delete". The URL becomes `/`, a status note "Deleted <file>. ..." has focus, the rail no longer lists it, `GET /documents/<id>` answers 404, and the chat's header reads "Asking <file> (deleted)".
- **Find.** With more than six documents, type `policy` into "Find a document". The rail and the picker list only `sample-cgl-policy.pdf`. Type a word nothing matches and both say so, with a "Clear" button.
- **Question box and suggestions.** In a three-turn chat at 1440x785, 1440x600 and 1440x500, the "Question" textbox and "Ask" button are inside the viewport. Inject a 72 px fixed element on the right, then an in-flow 72 px element as the first child of `body`; both stay inside. After an answer lands at 1024x768, the "Suggested questions" group's bottom is at or above the footer row's top.
- **Status line.** Record the busy turn's `[role="status"]` with a `MutationObserver` while a suggested follow-up is asked. It starts with "Sending the question" and never goes back to an earlier step.
- **Missing.** Open `/no-such-page`, `/?doc=<random uuid>` and `/?chat=<random uuid>`. Each shows a sentence and an "Ask your documents" link to `/`, and the missing chat has no "Question" textbox.
- **Keyboard.** Tab through the top bar, rail, document page, chat and pane. Every stop shows the carbon focus ring and is on screen.
- **Upload limit.** Pick a file larger than `max_upload_bytes` from `/health`. The rail refuses it before sending, and no `POST /api/documents` request is made.
- **Proof.** Take a screenshot after each step into the run's evidence folder. Read the URL and the `mark` text with `browser_evaluate` and save them beside the screenshots.

## Gotchas

- The Playwright MCP browser is headed and visible on the desktop. A stray click there changes the URL. Log input events with an init script (`pointerdown`, `click`, `keydown`, `popstate`) and redo any step with input you did not make.
- A browser that once visited another app on the same port may hold a cached redirect (for example to `/sign-in`). Add a query string (`/?fresh=1`) to get past it.
- The page pane slides in. Wait for the `mark` before you take the screenshot.
- An empty vault exists only on a fresh stack. On a stack with documents, a Playwright route that answers `GET /api/documents?limit=100` with `{"items": [], "next_cursor": null}` shows the screen; label that screenshot MOCKED.
- The fake provider sends its stages within a millisecond of each other, so React often renders two of them at once. A status log can skip a step; it must not go backwards.
- Each document checkbox is named by its filename through `aria-labelledby`, so use `getByRole('checkbox', { name: 'sample-cgl-policy.pdf', exact: true })`. Some accessibility-tree tools do not resolve `aria-labelledby` and show the checkboxes as "on". Playwright's role queries do resolve it.
- The chat header comes from `GET /chats/{id}`, so a chat link names its documents in any browser.
