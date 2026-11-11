# Doc Vault web UI

A small Next.js app for the doc-vault API. You can upload documents and watch them process. Each document shows its summary, tags and key facts. Click a key fact's page tab to open that page with the quoted passage marked.

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
| `src/api/client.ts` | One function per endpoint. Each returns `Result<T>` and never throws. Uploads use XHR for progress |
| `src/api/domain.ts` | Types the components read: branded ids, the lifecycle and insights unions, `SourceRef` |
| `src/lib/` | Pure functions: `locateQuote`, `isSettled`, `stageProgress` and the upload scheduler |
| `src/hooks/` | `usePolled`, `useFetched`, `useSelection` and `useUploads` |
| `src/components/` | The rail, the document view and the source pane |
| `src/copy.ts` | Every sentence the UI writes about state |

The URL holds the selection: `?doc=<id>` opens a document, and `&cite=fact.<n>` or `&p=<n>` opens the source pane. Opening a fact adds a history entry, so Back closes the pane. A pasted link reopens the same highlight.

## Tests and fixtures

`src/api/fixtures/` holds responses captured from the running API on 2026-11-11, with their HTTP status. They include a real failed document, made by uploading a textless PDF. Each document state from queued to ready is captured from one text upload. The insights states captured are pending, ready, ready with partial coverage, and unavailable. `fixtures/constructed/` holds the two responses no request could reach, failed insights and a 503 with `Retry-After`. Its README says so.

The `locateQuote` cases come from the backend's `tests/test_pages.py`. They were checked against the Python `locate_quote` on the same inputs.
