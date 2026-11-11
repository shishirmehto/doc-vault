# Web UI

The Next.js app on the `web` service shows the vault in a browser. A user uploads files and watches them process, reads a document's insights, chats with one or more documents, and opens any key fact or citation to its page with the quote highlighted. The URL holds what is open, so Back closes the page and a pasted link reopens it.

## Sub-features

- `ui-upload` uploads picked or dropped files, at most three at a time. A duplicate shows "Already in the vault" and an unsupported file shows the API's message.
- `ui-status` shows each document with a three-part stage bar, polled until it settles. A failed document shows its reason and "Retry anyway".
- `ui-insights` shows the summary, tags and key facts, each with its page. A long document shows its coverage ("Based on the first N of M sections").
- `ui-chat` starts a chat from ticked ready documents, or from "Ask about this document", and shows cited answers with numbered sources, "Read as" for follow-ups, and a not-found state.
- `ui-citation-open` opens a key fact or a source on its page with the quote marked and focused. Back closes it, and a pasted link reopens it.
- `ui-metrics` shows the two metrics endpoints.

## How to get to it (user POV)

- Open `http://localhost:13000` (verify stack) in a browser at 1,440 px wide or more. Narrow layouts are out of scope.
- Rail: the drop zone, the "Choose files" button, and one link per document.
- The document view: the "Read the pages" and "Ask about this document" buttons, and one button per key fact, named "<label>: show the passage on p. N".
- The chat: the document checkboxes, a "Question" textbox and an "Ask" button. Each source is a button named "Source N: <file>, p. N".
- The page pane: "Previous", "Next" and "Close".

## Driving it with Playwright

Preconditions:

- A baseline stack launched with `web` (the default for `launch.sh`), with the three samples uploaded and ready (see `upload-and-indexing.md`).
- The browser is at 1,440 x 900.

- **Upload.** Choose "Choose files" and pick `samples/sample-renewal-quote.pdf`. If it is new, its row moves from "Waiting to start" through "Reading pages" to "Ready". If it is a duplicate, it reads "Already in the vault".
- **Open insights.** Click the `sample-cgl-policy.pdf` link. The URL gains `?doc=<id>`, and the view lists key facts with page chips.
- **Open a fact.** Click the button "Each Occurrence Limit: show the passage on p. 2". The URL gains `&cite=fact.<n>`, and the pane shows "Page 2 of 7". There is one `mark` whose text, whitespace collapsed, is `Each Occurrence Limit $1,000,000`, and it has focus.
- **Back.** Go back. The URL returns to `?doc=<id>` and no `mark` remains.
- **Chat.** Tick the checkboxes for `sample-cgl-policy.pdf` and `sample-renewal-quote.pdf`, type `What is the property damage deductible?` in "Question" and press Enter. The URL becomes `?chat=<id>`. The answer lists source 1 `sample-cgl-policy.pdf, p. 2` and source 2 `sample-renewal-quote.pdf, p. 2`.
- **Open a source.** Click "Source 2: sample-renewal-quote.pdf, p. 2". The URL gains `&cite=turn.<turn id>.2`, the pane shows "Page 2 of 3", and the `mark` text is `Property Damage Deductible $5,000 per occurrence`.
- **Proof.** Take a screenshot after each step into the run's evidence folder. Read the URL and the `mark` text with `browser_evaluate` and save them beside the screenshots.

## Gotchas

- The Playwright MCP browser is headed and visible on the desktop. A stray click there changes the URL. Log input events with an init script (`pointerdown`, `click`, `keydown`, `popstate`) and redo any step with input you did not make.
- A browser that once visited another app on the same port may hold a cached redirect (for example to `/sign-in`). Add a query string (`/?fresh=1`) to get past it.
- The page pane slides in. Wait for the `mark` before you take the screenshot.
- The document checkboxes may have no accessible name until the UI labels them. If `getByRole('checkbox', { name })` finds nothing, find the row by its filename text.
- The API cannot read a chat's documents back (no `GET /chats/{id}` yet), so the chat header comes from this browser's `localStorage`. In a fresh browser, a chat link shows "Chat" as its title.
