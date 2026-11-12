# Live status

Document changes reach every client as they commit, through `GET /events/documents`, a server-sent event stream. Postgres triggers send a NOTIFY for each changed document, one listener per API process reads that document once, and every open stream gets the result. The web UI keeps its document list current from the stream and polls only while the stream is down.

## Sub-features

- `live-stream` (`GET /events/documents`) sends `document` (the `GET /documents/{id}` body), `deleted` (`{"id": ...}`) and `resync` (`{}`). It opens with the comment `: connected` and sends `: keepalive` after 15 s of silence.
- `live-cap` answers 503 `too_many_streams` with `Retry-After: 5` past 100 open streams on one API process.
- `live-resync` sends `resync` to every stream after the API's database listener reconnects.
- `live-ui` updates the rail with no reload and no list polling: a document uploaded elsewhere appears and moves to Ready, and a deleted one goes. When the rail holds the newest 100 and a delete leaves 99, the list is read once to bring in the next one. A document older than the newest 100, opened by link, is read again when an event names it. A tab showing a deleted document shows the missing-document page. On a phone with the rail folded, up to three documents in flight show under "Documents (n)", then "N more processing".
- `live-paused` shows "Live updates paused, retrying" in the top bar after 3 s without the stream, and polls the list as before until the stream is back.

## How to get to it (user POV)

- `curl -N $API/events/documents`.
- The web UI's rail and document page, on any screen that lists documents. The top bar's service note shows the paused state.

## Driving it

Preconditions: a stack from `launch.sh`. The web UI at `http://localhost:13000` is open and settled.

- **Stream.** Run `curl -sN --max-time 20 $API/events/documents > "$RUN_DIR/stream.txt" &`. Then upload a new file with `$S/call.sh POST /documents -F file=@<file>` and wait for it with `$S/wait-doc.sh <id>`. `stream.txt` holds `: connected`, then `document` events for the id whose `status` never goes backwards and ends at `ready` with `processing_ms` set. Run `$S/wait-doc.sh <id> insights`. Once insights are ready, a later `document` event carries the same `document_type` as `GET /documents/<id>`. Delete it with `$S/call.sh DELETE /documents/<id>`. The stream gets `event: deleted` with that id.
- **Stages.** To watch more than one or two events, upload a text file of about 5 MB: it parses in about a second and embeds in 27 batches of 128 passages. Over 2,000 synthetic sections (about 6 MB of text) it fails with `too_many_pages`.
- **Keepalive.** Leave a stream open for 16 s with nothing happening. It gets `: keepalive`.
- **Resync.** With a stream open, run `compose exec -T db psql -U vault -d vault -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'docvault-events'"`. The stream gets `event: resync` within about a second.
- **UI, no polling.** In the browser, record every request the page makes (Playwright `page.on("request")`) and the rail row's `.row-status` text with a `MutationObserver`. Upload with `curl` to the API port, not through the page. The row appears and reads Ready with zero `GET /api/documents` requests in between. The only stream request is the one the page opened at load.
- **UI, delete in another tab.** Open `/?doc=<id>` in a second tab, then delete the document with curl. That tab shows "This document isn't in the vault". Both pages must be visible: headless Playwright pages are, but a background browser tab or a hidden browser pane closes its stream (see Gotchas) and catches up only when shown.
- **UI, phone.** At 375x812 on the home screen, the rail is folded. Upload with curl. The in-flight row shows under "Documents (n)" with its stage bar. Upload five at once and the list shows three and "2 more processing", which opens the rail.
- **UI, paused.** Answer `**/api/events/documents` with 503 through a Playwright route. After 3 s the top bar reads "Live updates paused, retrying" (`role="status"`). For a real outage, stop the API container with a tab open and start it again. The note shows while it is down and goes once the stream reopens. The page then reads the list once.
- Run `scripts/visible.js` on the home, metrics, document and missing-document screens, with the note showing and hidden, as `README.md` describes.

## Gotchas

- A hidden page (`document.hidden`: a background tab, a minimised window, a hidden browser pane) closes its stream, because over HTTP/1.1 a browser allows six connections per host across all its tabs. It shows no live updates until it is visible again, and then opens a new stream and reads the list once. Drive live updates in a visible page. To test the rule, override `document.hidden` and dispatch `visibilitychange`.
- `page.wait_for_load_state("networkidle")` never returns on a screen that lists documents, because the event stream stays open. Wait for a selector instead.
- The first frame carries `retry: 1000`, so a browser reconnects 1 s after a dropped stream, before the 3 s that shows the paused note.

- `TestClient` streams this route fine on the pinned Starlette. The tests still use a real uvicorn server, so a closed socket frees its slot through the real middleware.
- The fake provider indexes a sample in about 200 ms, so a 100 ms coalescing window often shows only one or two `document` events for it. That is the coalescing working, not a missed stage.
- Stopping an API with an open stream takes 5 s, the compose graceful-shutdown timeout. The log then shows "Cancel 1 running task(s), timeout graceful shutdown exceeded" and a `CancelledError` traceback for each stream. That is expected.
- The web proxy returns 500, not 502, while the API is down. `EventSource` treats that as final, and the page opens a new stream after 2 s, then 4 s, and so on up to 30 s.
