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

## How to get to it (user POV)

- `POST /chats` with `{"document_ids": [...]}`, then `POST /chats/{id}/messages` with `{"question": "..."}`.
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

## Gotchas

- Under `ai_mode: fake`, answers are the source sentence that shares the most words with the question. Grounding and citations are real, but wording and judgement are not. Two known fake limits are tracked in `docs/evals.md`:
  - a comparison phrased with a word no chunk contains ("differ") is gated as `no_evidence`;
  - an elliptical follow-up ("And the premium?") is rewritten, but the fake often cites the wrong passage for it.
- The fake never produces `unverified`, because it quotes its sources word for word.
- Chats have no delete route. Runs on the isolated stack are removed by `cleanup.sh`.
