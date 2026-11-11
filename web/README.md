# Doc Vault web UI

A small Next.js app for the doc-vault API. You can upload documents and watch them process. Each document shows its summary, tags and key facts. You can also start a chat over one or more ready documents and ask questions. The answer appears as it is written, then the cited answer replaces it, with up to three follow-up questions to ask in one click. Click a key fact's page tab, or an answer's numbered citation, to open that page with the quoted passage marked and a flag on the page edge. In a chat, the flag carries the citation's number. The Metrics link in the header shows how documents, steps, AI calls and chats are doing.

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
| `src/lib/` | Pure functions: `locateQuote`, `isSettled`, `stageProgress`, the upload scheduler and the chat thread's state machine |
| `src/hooks/` | `usePolled`, `useFetched`, `useSelection`, `useUploads`, `useHealth` (one shared `/health` poll) and `useThread`, which sends one question at a time per chat |
| `src/components/` | The rail, the new-chat screen, the chat, the document view, the source pane and the metrics screen |
| `src/copy.ts` | Every sentence the UI writes about state |

The URL holds the selection:

- No parameters shows the new-chat screen. Only ready documents can be chosen. Below it are the five newest chats.
- `?view=metrics` shows the metrics, for the last 24 hours. `&window=7d` shows the last 7 days.
- `?doc=<id>` opens a document. `&cite=fact.<n>` or `&p=<n>` opens the source pane.
- `?chat=<id>` opens a chat. `&cite=turn.<turn id>.<n>` opens citation `[n]` of that turn, and `&p=<n>` pages away from it.

Starting a chat, opening a fact and opening a citation each add a history entry, so Back undoes them. A pasted link reopens the same chat or highlight in any browser, because the chat's header comes from `GET /chats/{id}`. If a chat's document was deleted after it started, the header marks it and the composer closes, since the API would refuse the question.

A file larger than `/health`'s `max_upload_bytes` is refused in the browser and never sent. Until `/health` answers, the API's 413 decides.

The metrics screen has two halves, documents and processing. Each half loads and fails on its own, so one failing route leaves the other on screen with its numbers. Under the fake provider, the cost reads "fake provider, no cost".

Questions go to `POST /chats/{id}/messages/stream`. While the answer streams, the turn shows its provisional text, without citations, under a status line: "Searching N documents", "Writing the answer", and "Checking citations" once no text has arrived for 300 ms. A `reset` event clears the text. The `turn` event replaces it with the grounded answer. The latest turn's suggestions show as buttons only while no question is in flight, and a click asks one at once.

When an ask fails, the chat reads its history again before it offers Try again. If the question was recorded after all, the turn is shown and nothing is asked twice. An `error` event and a stream that ends without its `turn` take this path too. The second case is a connection dropped mid-answer, and the backend still stores the turn.

## Tests and fixtures

`src/api/fixtures/` holds responses captured from the running API on 2026-11-11, with their HTTP status. They include a real failed document, made by uploading a textless PDF. Each document state from queued to ready is captured from one text upload. The insights states captured are pending, ready, ready with partial coverage, and unavailable. The chat fixtures come from the fake provider: a cited answer, a follow-up with its `interpreted_as`, a two-document answer, `no_evidence`, `model_declined`, a chat's history, the 409 `documents_not_ready` with filenames, a 404 and a 422. Phase 3 added a chat read back with a deleted document and its `documents_deleted` 409, the recent-chats page, a 429 `rate_limited` with `Retry-After`, `/health` with `max_upload_bytes`, and both metrics routes, including an empty window whose percentiles are null. `fixtures/constructed/` holds the responses no request could reach: failed insights, a 503 with `Retry-After`, an `unverified` turn and an answered turn with no citations. Its README says so.

At bonus phase A, `TurnOut` gained `suggestions`, so the `turn-*` and history fixtures were recaptured with the same questions over the same documents, and `ask-stream-deductible.txt` holds the raw body of a streamed ask. The stream tests read it split at every byte.

The `locateQuote` cases come from the backend's `tests/test_pages.py`. They were checked against the Python `locate_quote` on the same inputs.
