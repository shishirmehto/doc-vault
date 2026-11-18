# Doc Vault web UI

A small Next.js app for the doc-vault API. You can upload documents and watch them process. Each document shows its summary, tags and key facts, and can be deleted from its page. You can also start a chat over one or more ready documents and ask questions. The answer appears as it is written, then the cited answer replaces it, with up to three follow-up questions to ask in one click. In a chat's question box, Up and Down go through the chat's earlier questions. An answer shows in every tab of the browser that has the chat open. Click a key fact's page tab, or an answer's numbered citation, to open that page with the quoted passage marked.

You can also compare 2 to 5 ready documents side by side. A ready document's summary can be written again at another length, about one topic, or for the reader's client. A file uploaded again under the same name with new content becomes the document's next version. Metrics shows how documents, steps, AI calls and chats are doing, with the daily AI spending cap and the caches.

## Screens

The top bar's sections lead to three screens, and every other screen is reached from one of them. The URL holds the screen, so Back, Forward and a pasted link all work.

| Screen | URL | Ways in |
|---|---|---|
| Ask: the new-chat screen, or the empty-vault steps | `/` | Top bar "Ask", the wordmark, or "New chat" in a chat |
| A chat | `?chat=<id>` | Ask on the new-chat screen, Recent chats, or "Ask about this document" |
| Compare | `?view=compare` | Top bar "Compare", "Compare with..." on a document page, or Compare on the new-chat screen |
| A comparison | `?compare=<id>` | Compare, Recent comparisons, or "Compare with version N" |
| A document | `?doc=<id>` | A rail row, a version link, or "Open version N" in a chat or on an earlier version |
| Metrics | `?view=metrics` | Top bar "Metrics" |
| Not found | An unknown or malformed id, or another path | A stale link. The page says what is missing and links back |

The page itself never scrolls. Each of the three columns scrolls on its own. Below 1100 px the source pane covers the centre column. Below 760 px the rail folds into a "Documents (n)" button above the centre column.

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

The rewrite also compresses responses, and a compressed `text/event-stream` arrives in one burst when the answer ends. The API marks the stream `Cache-Control: no-cache, no-transform`, and the proxy's compression skips such a response, so the streamed answer needs no route handler of its own.

## Scripts

| Script | What it does |
|---|---|
| `npm run dev` | Development server on port 3000 |
| `npm run build` | Production build (`output: "standalone"`) |
| `npm run lint` | ESLint with `eslint-config-next` |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Unit tests on Node's built-in runner. Node strips the TypeScript types |
| `npm run gen:api` | Regenerates `src/api/schema.d.ts` from the running API's `/openapi.json` |
| `npm run check:api` | Runs `gen:api` against the running API, then fails if `schema.d.ts` changed |

## Where things live

| Path | Contents |
|---|---|
| `src/api/schema.d.ts` | Wire types generated from the API. Committed and never edited by hand |
| `src/api/parse.ts` | The only place where wire types become domain types. It also parses the URL selection |
| `src/api/client.ts` | One function per endpoint. Each returns `Result<T>` and never throws |
| `src/api/sse.ts` | Frames a `text/event-stream` body into messages, from text split anywhere |
| `src/api/domain.ts` | Types the components read: branded ids, the lifecycle, insights and turn unions, `SourceRef` |
| `src/lib/` | Logic with no React, tested on Node |
| `src/hooks/` | The React hooks, such as `useDocuments` (the list, kept current by the live feed, with polling as the fallback) and `useThread`, which sends one question at a time per chat and shows a question that another tab asked |
| `src/components/` | The top bar, the rail, the new-chat screen, the chat, the compare screen, the comparison, the document view, the source pane, the metrics screen, and the missing-page message |
| `src/copy.ts` | Every sentence the UI writes about state |

The URL holds the selection:

- No parameters shows the new-chat screen. Only ready documents can be chosen. Below the picker are the five newest chats.
- `?view=metrics` shows the metrics, for the last 24 hours. `&window=7d` shows the last 7 days.
- `?doc=<id>` opens a document. `&cite=fact.<n>` or `&p=<n>` opens the source pane.
- `?chat=<id>` opens a chat. `&cite=turn.<turn id>.<n>` opens citation `[n]` of that turn.
- `?view=compare` shows the compare screen. Only ready documents can be ticked, at most 5.
- `?compare=<id>` opens a comparison. `&cite=cmp.<row>.<col>` opens the page of one cell, and `&only=differences` shows only the fields that differ.

The document list follows `GET /events/documents` through one `EventSource` for the page. A document uploaded, retried or deleted from another tab or client shows up in the rail and moves through its stages with no reload. While the stream is down, polling takes over.

Questions go to `POST /chats/{id}/messages/stream`. While the answer streams, the turn shows its provisional text, without citations, under a status line. The `turn` event replaces it with the grounded answer.

The other tabs of the same browser that show the chat follow the answer. The asking tab posts its question's state on a `BroadcastChannel` after each step. The other tabs draw that state and cannot ask until the question is over. The asking tab also holds a Web Lock while it sends the question and checks that the turn was stored. If the browser frees that lock while another tab still shows the question, the asking tab was closed or has stopped checking. The other tab then takes the question over and reads the chat's history until the stored turn appears. None of this goes through the API.

Up in an empty question box brings back the chat's newest question. Up and Down then move through the earlier ones, and Down past the newest empties the box. A typed draft is never replaced.

## Tests and fixtures

`src/api/fixtures/` holds responses captured from the running API, with their HTTP status. The chat fixtures come from the fake provider. `fixtures/constructed/` holds responses built by hand, not captured: failed insights, a 503 with `Retry-After`, a 500 `internal_error`, a stream that ends in `quota_exceeded`, an `unverified` turn, an answered turn with no citations, health with the cache off or unreachable, and processing metrics with the cache off. Its README says how each was made.

The stream tests read a captured stream body split at every byte. The `locateQuote` cases come from the backend's `tests/test_pages.py`. They were checked against the Python `locate_quote` on the same inputs.
