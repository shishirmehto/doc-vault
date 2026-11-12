# Chat with citations

A user starts a chat on one or more ready documents and asks questions. Each answer cites the pages it used, and every quote is checked against the page text before it is shown. A follow-up is rewritten into a standalone question using the chat's history. A question the documents cannot answer gets a deliberate "not found", made without paying for an answer call.

## Sub-features

- `chat-start` creates a chat with a fixed document scope (`POST /chats`, 201).
- `chat-not-ready` refuses a scope with a document that is not ready (409 `documents_not_ready`). An empty scope gets 422.
- `chat-cited-answer` answers with `status: answered`. Each citation has `filename`, `page` and a `quote` that is on that page.
- `chat-follow-up` rewrites a follow-up with a reference word ("it", "that") into a standalone question, returned as `interpreted_as`.
- `chat-multi-doc` cites every document in the scope that holds the answer.
- `chat-not-found` returns `status: not_found` with `reason` `no_evidence` (gated before the answer call), `model_declined` or `unverified`.
- `chat-history` returns every turn in order (`GET /chats/{id}/messages`).
- `chat-stream` streams the same answer as server-sent events (`POST /chats/{id}/messages/stream`): `stage`, then `delta` text without `[S#]` markers, then one `turn` (or `error`). A client that leaves mid-answer still gets its turn stored.
- `chat-suggestions` returns up to three `suggestions` per turn. Asking one comes back `answered`.

## How to get to it (user POV)

- `POST /chats` with `{"document_ids": [...]}`, then `POST /chats/{id}/messages` with `{"question": "..."}`.
- `POST /chats/{id}/messages/stream` with the same body, read with `curl -N`.
- `GET /chats/{id}/messages`.
- The web UI's chat (see `web-ui.md`).

## Driving it with call.sh

Preconditions:

- `sample-cgl-policy.pdf` (P) and `sample-renewal-quote.pdf` (Q) uploaded and `ready`.
- `H='content-type: application/json'`.

- **Start.** Run `$S/call.sh POST /chats -H "$H" -d '{"document_ids":["<P>"]}'`. Status `201`, and `documents` lists `sample-cgl-policy.pdf`.
- **Cited answer.** Ask `What is the property damage deductible?`. Status `200`, `"status": "answered"`. One citation has `"filename": "sample-cgl-policy.pdf"`, `"page": 2` and the quote `Property Damage Deductible $2,500 per occurrence` (fact `policy-deductible`).
- **Check the quote.** Run `$S/call.sh GET /documents/<P>/pages/2`. The text contains the quote (whitespace may differ).
- **Follow-up.** Ask `Does it apply per occurrence or per claim?`. `interpreted_as` differs from the question and contains `deductible`.
- **Not found.** Ask `Who won the 2022 football World Cup?`. `"status": "not_found"`, `"reason": "no_evidence"`, no citations. Then run `$S/sql.sh "SELECT purpose FROM llm_calls WHERE turn_id = '<turn id>'"`. It shows no `answer` row.
- **Two documents.** Start a chat on P and Q and ask `What is the property damage deductible?`. Citations come from both files: P p. 2 `$2,500` and Q p. 2 `$5,000` (facts `policy-deductible` and `quote-deductible`).
- **History.** Run `$S/call.sh GET /chats/<chat id>/messages`. `turns` holds every question in order, with its status.
- **Refusals.** Run `POST /chats` with `{"document_ids": []}` and get `422`. With a failed document's id you get `409`, with `error.code == "documents_not_ready"` naming it.
- **Suggestions.** Every turn above has `suggestions`, at most three, each ending in `?`. The flood turn's come from the policy's key facts (`What is the Policy Number?`). Ask one and get `"status": "answered"`.

## Driving the stream

`call.sh` saves whole bodies, so read the stream with `curl -N` and stamp each line with its arrival time. The answer cache serves a first question that this stack has answered before on the same documents, with one `delta` and no `stage` (`caching.md`). So stream in a new chat on P before **Cited answer** asks the same question:

```bash
curl -sN -D - http://localhost:18000/chats/<chat id>/messages/stream -H "$H" \
  -d '{"question": "What is the property damage deductible?"}' \
  | python3 -c 'import sys,time; t=time.time(); [print(f"{(time.time()-t)*1000:6.0f} ms {l.rstrip()}") for l in sys.stdin]' \
  | tee "$EVIDENCE_ROOT/$RUN_ID/stream-deductible.txt"
```

- Headers include `content-type: text/event-stream`, `cache-control: no-cache, no-transform` and `x-accel-buffering: no`.
- Events come in this order: `stage` `searching`, `stage` `answering`, several `delta` events about 25 ms apart (`FAKE_STREAM_DELAY_MS` in compose), then `turn`. The deltas joined read `Property Damage Deductible $2,500 per occurrence`, without `[S1]`. The `turn` data equals the last turn of `GET /chats/<chat id>/messages`.
- A follow-up starts with `stage` `condensing`. `Who won the 2022 football World Cup?` sends `stage` `searching`, then `turn` with `"reason": "no_evidence"`, and no `delta`.
- **Disconnect.** Start a new chat on Q alone, a scope no step above has asked, and run the same command with `--max-time 0.1` to cut the stream mid-answer (curl exits 28). After a second, `GET /chats/<that chat id>/messages` shows the turn as answered, and `$S/sql.sh "SELECT purpose, ok FROM llm_calls WHERE turn_id = '<turn id>'"` shows its `answer` row. It has no `embed_query` row, because the first stream embedded the same question text and the embedding cache serves it.
- **Through the web proxy.** In the chat from the first stream, where the question is now a follow-up that the answer cache does not serve, run the same command against `http://localhost:13000/api/chats/<chat id>/messages/stream` with `-H 'accept-encoding: gzip'`. The response has no `content-encoding`, and the deltas keep their spacing. If they arrive in one burst at the end, the proxy is compressing the stream.

## Gotchas

- Under `ai_mode: fake`, answers are the source sentence that shares the most words with the question. Grounding and citations are real, but wording and judgement are not. Two known fake limits are tracked in `docs/evals.md`:
  - a comparison phrased with a word no chunk contains ("differ") is gated as `no_evidence`;
  - an elliptical follow-up ("And the premium?") is rewritten, but the fake often cites the wrong passage for it.
- The fake never produces `unverified`, because it quotes its sources word for word.
- The fake's suggestions are "What is the <label>?" for label-and-value passages in its sources, so most policy questions suggest the policy number, the named insured and the mailing address.
- Chats have no delete route. Runs on the isolated stack are removed by `cleanup.sh`.
- The answer cache is on in the isolated stack. A new chat whose first question this stack already answered on the same documents comes back `cached: true` and writes no `llm_calls` row. **Cited answer** still passes on a cached answer, because the cache keeps its citations.
