# Doc Vault web UI

A small Next.js app for the doc-vault API. You can upload documents and watch them process. Each document shows its summary, tags and key facts, and can be deleted from its page. You can also start a chat over one or more ready documents and ask questions. The answer appears as it is written, then the cited answer replaces it, with up to three follow-up questions to ask in one click. Click a key fact's page tab, or an answer's numbered citation, to open that page with the quoted passage marked and a flag on the page edge. In a chat, the flag carries the citation's number. The top bar has two sections: Ask, the new-chat screen and its chats, and Metrics, which shows how documents, steps, AI calls and chats are doing.

## Layout

The page itself never scrolls. `html`, `body` and the `.app` wrapper are flex and grid boxes down to the three columns, so each column scrolls on its own. Anything a browser extension adds to the page, or a margin it sets, takes its own share of the height instead of pushing content off the window. On the chat and new-chat screens the content scrolls above a footer row that holds the question box, and nothing is `position: sticky`. The question box stays on screen at every window height down to 500 px.

Below 1100 px the source pane covers the centre column. Below 760 px the rail folds into a "Documents (n)" button above the centre column. It is open while the vault is empty or uploads are listed, closes when a document is opened from it, and takes at most half the height, with its own scroll.

An empty vault shows the steps from upload to first question, with a large drop target in step 1. Over six documents, the rail shows a "Find a document" field and the new-chat picker a "Find a document to ask about" field. Each matches the filename and the document type, except the type "other", which rows do not show. The picker's "Select all ready" ticks the ready documents the search shows, up to the 50 a chat allows. Each row's meta leads with the document type once insights know it ("Policy. PDF, 7 pages, 11 KB").

`.claude/skills/verify-doc-vault/scripts/visible.js` checks a screen for clipped text, controls nothing can scroll into view, and sideways page scroll. The verify skill's `features/README.md` says how to run it.

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

The rewrite also compresses responses, and a compressed `text/event-stream` arrives in one burst when the answer ends. The API marks the stream `Cache-Control: no-cache, no-transform`, and the proxy's compression skips such a response, so the streamed answer needs no route handler of its own. Measured in a browser on port 3000, the deltas of a fake answer arrived at 61, 87, 117 and 144 ms and the final turn at 546 ms.

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
| `src/api/client.ts` | One function per endpoint. Each returns `Result<T>` and never throws. Uploads use XHR for progress. `askStream` reads the streamed ask with `fetch`, because `EventSource` cannot POST |
| `src/api/sse.ts` | Frames a `text/event-stream` body into messages, from text split anywhere |
| `src/api/domain.ts` | Types the components read: branded ids, the lifecycle, insights and turn unions, `SourceRef` |
| `src/lib/` | Pure functions: `locateQuote`, `isSettled`, `stageProgress`, the upload scheduler, the chat thread's state machine, and `findDocs` and `chooseAll` for long lists |
| `src/hooks/` | `usePolled`, `useFetched`, `useSelection`, `useUploads`, `useHealth` (one shared `/health` poll), `useFileDrop`, and `useThread`, which sends one question at a time per chat |
| `src/components/` | The top bar, the rail, the new-chat screen, the chat, the document view and its delete dialog, the source pane, the metrics screen, and the missing-page message |
| `src/copy.ts` | Every sentence the UI writes about state |

The URL holds the selection:

- No parameters shows the new-chat screen. Only ready documents can be chosen. Below the picker are the five newest chats.
- `?view=metrics` shows the metrics, for the last 24 hours. `&window=7d` shows the last 7 days.
- `?doc=<id>` opens a document. `&cite=fact.<n>` or `&p=<n>` opens the source pane.
- `?chat=<id>` opens a chat. `&cite=turn.<turn id>.<n>` opens citation `[n]` of that turn, and `&p=<n>` pages away from it.

A `?doc=` or `?chat=` the vault does not have, or one that is not an id at all, shows a sentence saying so and an "Ask your documents" button. Any other path shows the app's own not-found page.

Deleting a document asks first, in a dialog that names the file and says that chats which used it keep their answers and citations but take no more questions. Afterwards the home screen opens with a note naming the file, and the note takes focus.

Starting a chat, opening a fact and opening a citation each add a history entry, so Back undoes them. A pasted link reopens the same chat or highlight in any browser, because the chat's header comes from `GET /chats/{id}`. If a chat's document was deleted after it started, the header marks it and the composer closes, since the API would refuse the question.

A file larger than `/health`'s `max_upload_bytes` is refused in the browser and never sent. Until `/health` answers, the API's 413 decides.

The metrics screen has two halves, documents and processing. Each half loads and fails on its own, so one failing route leaves the other on screen with its numbers. Under the fake provider, the cost reads "fake provider, no cost".

Questions go to `POST /chats/{id}/messages/stream`. While the answer streams, the turn shows its provisional text, without citations, under a status line. It reads "Sending the question" until the server names its first step, then follows the steps in the order the server sends them: "Reading the follow-up against the conversation" (follow-ups only), "Searching N documents", "Writing the answer", and "Checking citations" once no text has arrived for 300 ms. A `reset` event clears the text. The `turn` event replaces it with the grounded answer. The latest turn's suggestions show as buttons only while no question is in flight, and a click asks one at once.

When an ask fails, the chat reads its history again before it offers Try again. If the question was recorded after all, the turn is shown and nothing is asked twice. An `error` event and a stream that ends without its `turn` take this path too. The second case is a connection dropped mid-answer, and the backend still stores the turn, possibly seconds later. So after a stream that had started breaks, the chat reads history again after 0, 1, 2, 4, 8 and 15 seconds and adopts the turn as soon as it appears. It offers Try again only when the turn is still missing after that. In a browser check, a stream cut after its first delta was adopted from the 1-second read, with no Try again.

While an answer streams, the column follows its text until the reader scrolls up. When the answer lands, the column scrolls to the end, so the suggestions under it sit fully above the question box.

## Tests and fixtures

`src/api/fixtures/` holds responses captured from the running API on 2026-11-11, with their HTTP status. They include a real failed document, made by uploading a textless PDF. Each document state from queued to ready is captured from one text upload. The insights states captured are pending, ready, ready with partial coverage, and unavailable. The chat fixtures come from the fake provider: a cited answer, a follow-up with its `interpreted_as`, a two-document answer, `no_evidence`, `model_declined`, a chat's history, the 409 `documents_not_ready` with filenames, a 404 and a 422. Phase 3 added a chat read back with a deleted document and its `documents_deleted` 409, the recent-chats page, a 429 `rate_limited` with `Retry-After`, `/health` with `max_upload_bytes`, and both metrics routes, including an empty window whose percentiles are null. `fixtures/constructed/` holds the responses no request could reach: failed insights, a 503 with `Retry-After`, an `unverified` turn and an answered turn with no citations. Its README says so.

At bonus phase A, `TurnOut` gained `suggestions`, so the `turn-*` and history fixtures were recaptured with the same questions over the same documents, and `ask-stream-deductible.txt` holds the raw body of a streamed ask. The stream tests read it split at every byte.

At phase G1, `DocumentOut` gained `document_type`. `documents-page.json` was captured again. The other document fixtures are snapshots of states that have passed, so the field was added as the API writes it: null while insights are not ready, and, for the two with ready insights, the value `GET /documents/{id}` returned. `delete-404-not-found.json` is a real `DELETE` of an id the vault does not have.

The `locateQuote` cases come from the backend's `tests/test_pages.py`. They were checked against the Python `locate_quote` on the same inputs.
