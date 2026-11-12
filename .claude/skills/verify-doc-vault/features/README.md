# doc-vault verification map

This folder is the maintained source for verifying what a doc-vault user sees and does. Read this index first, then use the matching feature file as the recipe.

## Baseline preconditions

- A stack started by `scripts/launch.sh` (project `docvault-verify`, API `http://localhost:18000`, web `http://localhost:13000`), with empty volumes.
- `scripts/doctor.sh` exits 0.
- `00-health.json` in the run folder shows `"ai_mode": "fake"`, unless the run set `VERIFY_LIVE=1` on purpose.
- Run every command from the repo root. `S=.claude/skills/verify-doc-vault/scripts`.
- Never drive a stack this run did not start.

## Driving conventions

- Use only public routes (`/documents`, `/chats`, `/comparisons`, `/metrics`, `/health`) and the UI. Never call Python, insert rows, or edit the database to reach a state. The one exception is the priced `llm_calls` row in `quota-and-logs.md`, on the isolated `docvault-verify` stack only.
- Take expected values from `samples/facts.yaml` (document, page, exact text). Never take them from an earlier response.
- In the browser, find elements by role and accessible name, not position.
- A fresh upload is a fresh hash. To watch processing again, upload a new file. Do not delete a sample just to re-upload it.

## Proof and skip reporting

- Capture the action and the state after it: the request, the response, and a second read-only view (a GET, `sql.sh`, or the UI).
- Name the feature ID and the entry point next to every artifact.
- If an entry point cannot be reached, report the command you tried and the precondition that was not met. Never report it as verified through another entry point.

## Visibility check: `scripts/visible.js`

Every UI proof also runs `scripts/visible.js` on each screen it shows, at 1440x785, 1024x768 and 375x812. The script is one JavaScript expression that runs in the page. It returns an array with one finding per problem, and `[]` when the screen is clean:

- `clipped`: an element with text whose content is cut off by `overflow: hidden` or `clip`, `text-overflow: ellipsis` or `line-clamp` (`scrollWidth > clientWidth` or `scrollHeight > clientHeight`). `body` and the centre column count too, so content pushed off the bottom of the window shows up as `clipped body` beside the controls it hid.
- `unreachable`: a button, link, field or `summary` outside the viewport that no scroll container can bring into view. The check walks out from the control. An `overflow: hidden` ancestor that it falls outside hides it for good. A scroll container can bring it into view if the container is at least the control's size. From there the question is whether a control-sized box inside the container can be seen, so a sideways-scrolling table taller than its column is still reachable row by row.
- `page-scroll`: the page is wider than the viewport.
- `side-scroll`: a column or other scroll container scrolls sideways. The page never scrolls in this app, so this is where content too wide for its column ends up. A box meant to scroll sideways, such as a metrics table's, carries `data-scroll-x` and is not reported.

Run it with Playwright:

```python
findings = page.evaluate(Path(".claude/skills/verify-doc-vault/scripts/visible.js").read_text())
assert findings == [], findings
```

With a browser tool that evaluates an expression, paste the file's contents. With the Playwright MCP `browser_evaluate`, pass `() => <contents>`.

What it skips, and why:

- Scroll containers (`overflow: auto` or `scroll`), in the clipped check: the reader can scroll them.
- Elements that render no box (`display: none`, the `hidden` file input, a folded rail) and `visibility: hidden`: nothing is on screen to clip.
- Elements with no text, such as the stage bars and the indeterminate progress line, which are `overflow: hidden` around a moving bar.
- `input`, `textarea` and `select` in the clipped check: they scroll their own text.
- Controls inside `[inert]`.
- Overlap. A control covered by the source pane or by a dialog's backdrop is on screen, so the script does not report it.

Known blind spots: it tests a control against its DOM ancestors, not its containing block. So an absolutely positioned control under a static scroller, or a fixed control under a transformed ancestor, can be misjudged. The app has neither.

A clean result does not prove that text does not wrap where it should not, or that something sits above the question box. Check those with their own assertion, as `web-ui.md` describes.

## Features

| File | Feature IDs |
|---|---|
| [upload-and-indexing.md](upload-and-indexing.md) | `upload-new`, `upload-dedupe`, `upload-reject`, `index-ready`, `index-fail-retry`, `page-read` |
| [insights.md](insights.md) | `insights-ready`, `insights-grounded`, `insights-coverage`, `insights-fail-isolated` |
| [chat-with-citations.md](chat-with-citations.md) | `chat-start`, `chat-not-ready`, `chat-cited-answer`, `chat-follow-up`, `chat-multi-doc`, `chat-not-found`, `chat-history` |
| [metrics.md](metrics.md) | `metrics-documents`, `metrics-processing` |
| [compare.md](compare.md) | `compare-run`, `compare-focus`, `compare-refuse`, `compare-read`, `ui-compare` |
| [summaries.md](summaries.md) | `summary-run`, `summary-options`, `summary-cached`, `summary-refuse`, `summary-list`, `ui-summary` |
| [live-status.md](live-status.md) | `live-stream`, `live-cap`, `live-resync`, `live-ui`, `live-paused` |
| [versions.md](versions.md) | `version-new`, `version-dedupe`, `version-replaces`, `version-list`, `version-delete`, `version-live`, `ui-versions` |
| [caching.md](caching.md) | `cache-health`, `cache-answer`, `cache-answer-stream`, `cache-answer-skip`, `cache-embedding`, `cache-metrics`, `cache-unreachable` |
| [quota-and-logs.md](quota-and-logs.md) | `log-request-id`, `log-request-line`, `log-trace`, `log-web-proxy`, `quota-health`, `quota-refuse`, `quota-cached`, `quota-insights` |
| [web-ui.md](web-ui.md) | `ui-upload`, `ui-status`, `ui-insights`, `ui-chat`, `ui-chat-stream`, `ui-suggestions`, `ui-citation-open`, `ui-metrics`, `ui-delete`, `ui-find`, `ui-missing`, `ui-layout` |
