# Evals

[`docvault/evals.py`](../docvault/evals.py) asks a fixed set of broker questions about the three sample documents and scores retrieval, answers, refusals and insights separately, so a miss shows the step where it happened. It also compares the policy with its renewal quote and scores the comparison, and summarises each sample at each length and scores the summaries' sources. The case set is [`evals/cases.yaml`](../evals/cases.yaml), a held-out set of extra cases is [`evals/cases-extra.yaml`](../evals/cases-extra.yaml), and the expected values come from [`samples/facts.yaml`](../samples/facts.yaml).

## Results

One run of the full case set on 2026-11-18 at `f2243ec`, with the answer prompt `answer-v4`, on the configured OpenAI models, cost $1.24 on the ledger. The answer step received 16 sources, and the opening passages in chats of up to three documents. No model call failed.

| Number | Result |
|---|---|
| Retrieval hit rate | 34/34 (100%) |
| Citation accuracy | 28/34 (82%) |
| Not-found accuracy | 15/16 (94%) |
| Key-fact grounding | 36/36 (100%) |
| Key-fact recall | 19/28 (68%) |
| Suggestions answered | 89/89 (100%) |
| Cell grounding | 46/46 (100%) |
| Difference recall | 4/4 (100%) |
| Equal fields kept equal | 1/1 (100%) |
| Summary source grounding | 54/54 (100%), 18 at each length |
| Gate at 0.20: answerable turns passed | 28/28 (100%) |
| Gate at 0.20: out-of-scope turns refused | 0/16 (0%), by design: the gate sits below the lowest answerable turn, and the answer step declined 15 of the 16 |
| p95 turn latency | 9.3 s |

The six citation misses are `deductible-differ-explicit` and the two turns in `aggregate-then-medical`. The answers are right ("from $2,500 per occurrence ... to $5,000 per occurrence", "from $2,000,000 to $3,000,000", "from $5,000 to $10,000"), but each cites the one line of the quote's change list on page 3 that states both values, as the Known limits section predicts. The one not-found miss is the flood question in `insured-then-fired-employee`. The answer says the documents do not establish flood cover, and it counts as answered because it cites the coverage grant.

The gate passed every turn, so the answer step declined the 15 out-of-scope turns itself.

`answer-v4` lets the answer use a Markdown list and bold, which the web UI renders, and its markers name evidence items (`[E1]`), so each sentence cites the quote written for it. The plain-text prompt, `answer-v2`, scored 30/34 and 15/16 on 2026-11-17 at `f9f69eb` ($1.08). Four runs on 2026-11-18 moved two turns. `deductible-differ-explicit` cited the change list in three of the four, and `rented-equipment` was answered with related limits in the three runs of `answer-v3`.

### Fewer passages

The samples are small. At the default settings they make 11 chunks (6 for the policy, 2 for the quote, 3 for the endorsement), and the answer step receives up to 16 passages, so every chunk in a chat reaches the answer step and the retrieval hit rate cannot miss. To make retrieval choose, the same case set was run on 2026-11-18 at `91c96fb` with 4 passages and 600-character chunks, which gives 36 chunks. It cost $0.79.

| Number | 16 passages (above) | 4 passages, 600-character chunks |
|---|---|---|
| Retrieval hit rate | 34/34 (100%) | 31/34 (91%) |
| Citation accuracy | 28/34 (82%) | 24/34 (71%) |
| Turns cited in full | 25/28 (89%), counted from that run's turn table | 22/28 (79%) |
| Not-found accuracy | 15/16 (94%) | 15/16 (94%) |

Retrieval missed three facts: the policy's general aggregate limit in `most-paid-in-total`, the quote's in the first turn of `aggregate-then-medical`, and the employment exclusion in `insured-then-fired-employee`. No answer guessed. The aggregate question was still answered right, from the quote's change list. The employment question was declined. The total-limit answer said the documents do not state the limit's amount, which is wrong only because the line holding $2,000,000 was not among the 4 passages. That is the cost of a small number of passages, and the reason the default is 16. The other citation misses cite the quote's change list on page 3, as at the default settings.

### Held-out extra cases

[`evals/cases-extra.yaml`](../evals/cases-extra.yaml) asks 10 more turns, of kinds the main set does not ask (see The case set). Both runs were on 2026-11-18 at `91c96fb`, and cost $0.36 and $0.28.

| Number | 16 passages | 4 passages, 600-character chunks |
|---|---|---|
| Retrieval hit rate | 16/16 (100%) | 12/16 (75%) |
| Citation accuracy | 12/16 (75%) | 11/16 (69%) |
| Turns cited in full | 5/8 (62%) | 4/8 (50%) |
| Answer text | 7/7 (100%) | 7/7 (100%) |
| Not-found accuracy | 2/2 (100%) | 2/2 (100%) |
| Gate at 0.20: answerable turns passed | 8/8 (100%) | 8/8 (100%) |

- Start and end of one document. At 16 passages, all three start-and-end turns answered both halves right. Two cited both facts. `start-end-policy-number-refused-hire` cited the right clause on page 7 with a quote that stops before "refusal to employ", so the citation check counts a miss. At 4 passages, the page 7 exclusion did not reach the answer step for `start-end-policy-period-termination`, and the answer gave the dates and said the documents do not establish whether termination claims are covered.
- Four facts from pages 1, 2, 5 and 6 of the policy were cited, with the three expected values in the answer, in both runs.
- The premium rise was right in both runs, $3,450 worked out from $21,900 and $18,450. It cites the quote's change list on page 3, which states both premiums, so both planted facts score as misses.
- The date case was right in both runs: no cover for an accident on 1 February 2026, because the endorsement applies from 03/15/2026.
- "Just the renewal one, please." was rewritten into a standalone question and answered $5,000 per occurrence in both runs.
- Neither out-of-scope question leaked an answer from a document outside its chat. Both were declined.
- The gate threshold was fitted on the main set, and it passed all 8 answerable held-out turns.

## Run the evals

Set `OPENAI_API_KEY` in `.env` and start the compose Postgres (`docker compose up -d db`). Then run the eval on the host. It prints the results.

```bash
DATABASE_URL=postgresql://vault:vault@localhost:54329/vault uv run python -m docvault.evals
```

- The run works in its own database: the `DATABASE_URL` database name plus `_evals` (here `vault_evals`). It drops and recreates that database at the start of every run and never opens `vault` or `vault_test`. Blobs go to a temporary directory.
- `--cases evals/cases-extra.yaml` runs the held-out extra cases instead of the main set.
- `RETRIEVAL_K=4 CHUNK_MAX_CHARS=600 CHUNK_OVERLAP_CHARS=100` in front of the command runs with 4 passages and 600-character chunks, as in Fewer passages above.
- `--calibrate` prints, instead of the results, every turn's standalone query, best vector similarity and whether one chunk matched every query term, then the gate threshold that fits the set best.
- The eval needs the dev dependencies (`pyyaml`), so it runs on the host with `uv run`. The Docker image installs `--no-dev` and holds neither [`evals/`](../evals/) nor [`samples/`](../samples/).
- Without a key the eval runs on the fake provider. That run says nothing about answer quality. [`tests/test_evals.py`](../tests/test_evals.py) runs it in `<test database>_evals` and fails when a code change moves its numbers.

## Calibrate the OpenAI gate

`OpenAIEmbedder.min_similarity` is 0.20, set from the calibration run of 2026-11-12. To calibrate it again, run the eval with a key and read the `Best threshold` line:

```bash
OPENAI_API_KEY=sk-... DATABASE_URL=postgresql://vault:vault@localhost:54329/vault uv run python -m docvault.evals --calibrate
```

The gate reads only the embedder, so `CHAT_PROVIDER=fake` in front of the command limits the spend to embeddings. The follow-up turns then use the fake condense, which rewrites them differently from the live model.

The 2026-11-12 run did not use the best-fit value. Answerable turns scored 0.239 to 0.606 and out-of-scope turns 0.257 to 0.514, so the classes overlap and no threshold separates them. The best fit, 0.518, put 32 of 44 turns on the right side, but it refused 12 of the 28 answerable turns. A refused answerable question costs a broker more than one answer call, so the threshold sits below the lowest answerable turn, at 0.20. On the full live run, the gate at 0.20 passed all 44 turns, including the 16 out-of-scope turns. The answer step then declined 15 of those 16.

## What each number means

Each answerable turn names one or more expected facts. A comparison names one fact per document. Retrieval and citations are scored per expected fact, so a turn can hit one document and miss the other.

| Number | Definition |
|---|---|
| Retrieval hit rate | The expected fact's text is in a source of the expected document that the answer step received, at a position on the expected page. `pages.locate_quote` finds the text and `PageMap.page_at` gives its page. The eval reads the sources with a `ChatBackend` wrapper around `ctx.ai.chat`, so `chat.py` is unchanged. A miss names its reason: `gate` (nothing passed the relevance gate, so the answer step saw only the opening passages), `text` (no source of that document holds the text) or `page` (a source holds it on another page). |
| Citation accuracy | A citation of the expected document has the expected page, and its quote contains the fact's text after collapsing whitespace. An answerable turn that comes back `not_found` misses every fact it expected. |
| Turns cited in full | An answerable turn cites every one of its expected facts, as citation accuracy counts them. A two-fact turn that cites one fact scores 1/2 on citation accuracy and 0/1 here. |
| Answer text | A turn that lists `values` holds each of them in its answer text, and a turn that lists `forbid` holds none of them. Matching ignores case, "$", "," and "*", and counts whole words and numbers only, so "5,000" is not found in "$15,000". Only turns that list `values` or `forbid` count. |
| Not-found accuracy | An out-of-scope turn comes back `not_found`, for any reason (`no_evidence`, `model_declined` or `unverified`). |
| Key-fact grounding | Insights key facts kept, over kept plus dropped, summed over the three documents. `insights.verify` keeps a fact only when its quote is on the page it cites, so every kept fact is grounded by construction. This number says how often the model cited the wrong page or misquoted, not whether the kept facts are the right ones. |
| Key-fact recall | Planted facts whose text appears in a kept key fact's quote on the same document and page. The denominator is [`samples/facts.yaml`](../samples/facts.yaml) without the two running footers (28 facts), because a footer repeated on every page is not a key fact. |
| Suggestions answered | After a case's own turns, every suggestion under its last turn is asked in that chat, the way a click on one asks it, and should come back `answered`. These turns are kept out of every other number: they have no expected facts, so they would count as out of scope and move the gate fit. |
| Cell grounding | Comparison cells kept, over kept plus dropped. `compare.ground` keeps a filled cell only when its quote is on the page it cites, so every kept cell is grounded by construction. Like key-fact grounding, this says how often the model cited the wrong page or misquoted. |
| Difference recall | The comparison case is built in code from `samples/facts.yaml`, not from `cases.yaml`, whose hash is frozen. It compares the policy with the renewal quote, with no focus. Each entry under `changes` (general aggregate limit, medical expense limit, property damage deductible, premium) counts when one row has `differs` true, its policy cell is on the planted page with the planted text inside its quote and its value inside the planted text, and its quote cell likewise. The value check stops a row whose long quote runs on into a neighbouring fact from matching that fact. A change found only as two rows, or with a cell on the wrong page, is a miss. |
| Equal fields kept equal | The same match for each entry under `unchanged` (each occurrence limit), which must come back with `differs` false. It catches a comparison that marks every row as different. |
| Summary source grounding | Each of the three samples is summarised once at each length (brief, standard, detailed), with no focus and the plain tone. For each length, sources kept over kept plus dropped, summed over the samples. `summaries.ground` keeps a source only when its quote is on the page it cites, so this says how often the model cited the wrong page or misquoted. |
| Gate rows | A turn passes the gate when one chunk holds every query term or its best vector similarity reaches `min_similarity`. "Refused" here means nothing passed the gate. An out-of-scope turn that passes can still come back `not_found` from the answer step. |
| Cost and latency | `metrics.processing_stats`, the reader behind `GET /metrics/processing`: `llm_calls` grouped by purpose and model (calls, failed calls, tokens, cost, p95 `latency_ms`), and p95 turn latency over `chat_turns.finished_at - started_at`. Every row in the eval database belongs to the run. |

## The case set

[`evals/cases.yaml`](../evals/cases.yaml) holds 36 cases with 44 turns: 28 answerable turns with 34 expected facts, and 16 out-of-scope turns. `evals.load_cases` checks it against [`samples/facts.yaml`](../samples/facts.yaml) before a run. Every fact id must exist and sit in the chat's scope. Every fact must be asked or listed under `not_asked` with a reason. Every `absent_topics` entry must appear in an out-of-scope question.

- Questions use a broker's words, not the fact's. A question that repeats a fact's words passes the gate's all-terms arm every time and measures nothing.
- Out-of-scope questions cover each absent topic, then questions that share the documents' vocabulary without being answered by them (an umbrella limit, a producer's commission, a deductible in the endorsement).
- Multi-turn cases include follow-ups ("And the premium?", "Is it shorter if...", "What about...") and standalone topic switches, so a condense rule that rewrites every question cannot score well.
- The case set was written and frozen (sha256 `f9d83a5e793d2650c669aba8211426ec3d3d6e9c96a6caadd17ab82316758d36`) before any calibration. No question was edited afterwards.

[`evals/cases-extra.yaml`](../evals/cases-extra.yaml) holds 9 cases with 10 turns: 8 answerable turns with 16 expected facts, and 2 out-of-scope turns. It was written on 2026-11-18, after the gate was calibrated, and frozen (sha256 `7b58a1372ec75a1ebbe8fc7243c65fbd34d40e4a09025c36548a424fdfbf1321`) before its first run. It uses only facts already in `facts.yaml` and asks what the main set does not:

- two facts at the two ends of one document, on the policy (pages 1 and 7) and on the quote (pages 1 and 3);
- four facts spread through one document (pages 1, 2, 5 and 6);
- a figure worked out from two cited figures (the premium rise);
- a date that decides the answer (an accident before the endorsement's effective date);
- a follow-up that narrows the last question instead of changing its topic;
- two questions that only a document outside the chat answers, which must not name that answer.

Its turns list `values` that the answer text must hold and `forbid` strings it must not. The file sets `partial:`, so `load_cases` skips the two coverage checks that the main set must pass.

## Known limits

- The citation check counts only the planted text. "The General Aggregate Limit is the most we will pay during the policy period" answers its question correctly and still scores as a miss. A model comparing the policy and the quote may cite the quote's change list on page 3 ("increases from $2,500 to $5,000"), which also scores as a miss.
- "CG 21 47 Employment-Related Practices Exclusion" is on page 3 (the schedule) and, as a heading, on page 7. The case asks about the schedule, and a page 7 citation scores as a miss.
- Some out-of-scope questions can be answered by inference ("no flood limit is listed"). Review not-found misses by hand after a run.
- Difference recall counts only the planted changes. There is no precision score for differences beyond the one equal field.
- The gate threshold was set from a calibration run on the same case set, so the gate rows are in-sample. The extra cases are out of sample.
- At the default settings, retrieval cannot miss on the samples (see Fewer passages). The run with 4 passages is the measurement of ranking.
- The answer-text check is a string check. It confirms figures and catches a forbidden name, but it cannot judge a yes or no, or tell an answer that corrects a false premise from one that accepts it.
- Every number comes from one run. Four runs on 2026-11-18 moved two turns, so a difference of one or two facts between two runs is within the noise.
