# Evals

`docvault/evals.py` asks a fixed set of broker questions about the three sample documents and scores retrieval, answers, refusals and insights separately, so a miss shows the step where it happened. It also compares the policy with its renewal quote and scores the comparison, and summarises each sample at each length and scores the summaries' sources. The case set is `evals/cases.yaml`, and the expected values come from `samples/facts.yaml`.

## Results

One run of the full case set on 2026-11-12, on the configured OpenAI models, cost $1.18 on the ledger. The answer step then received eight sources. It has received 16 since 2026-11-17, and the set has not been run live again since.

| Number | Result |
|---|---|
| Retrieval hit rate | 34/34 (100%) |
| Citation accuracy | 30/34 (88%) |
| Not-found accuracy | 15/16 (94%) |
| Key-fact grounding | 36/36 (100%) |
| Key-fact recall | 19/28 (68%) |
| Suggestions answered | 90/90 (100%) |
| Cell grounding | 45/45 (100%) |
| Difference recall | 4/4 (100%) |
| Equal fields kept equal | 1/1 (100%) |
| Summary source grounding | 54/54 (100%), 18 at each length |
| Gate at 0.20: answerable turns passed | 28/28 (100%) |
| Gate at 0.20: out-of-scope turns refused | 0/16 (0%) |
| p95 turn latency | 7.3 s |

The four citation misses are the two turns in `aggregate-then-medical`. The answers are right ("from $2,000,000 to $3,000,000", "from $5,000 to $10,000"), but they cite the quote's change list on page 3, as the Known limits section predicts. The one not-found miss is the flood question in `insured-then-fired-employee`. The answer says the documents do not establish flood cover, and it counts as answered because it cites the coverage grant.

The gate passed every turn, so the answer step declined the 15 out-of-scope turns itself.

## Run the evals

Set `OPENAI_API_KEY` in `.env` and start the compose Postgres (`docker compose up -d db`). Then run the eval on the host. It prints the results.

```bash
DATABASE_URL=postgresql://vault:vault@localhost:54329/vault uv run python -m docvault.evals
```

- The run works in its own database: the `DATABASE_URL` database name plus `_evals` (here `vault_evals`). It drops and recreates that database at the start of every run and never opens `vault` or `vault_test`. Blobs go to a temporary directory.
- `--calibrate` prints, instead of the results, every turn's standalone query, best vector similarity and whether one chunk matched every query term, then the gate threshold that fits the set best.
- The eval needs the dev dependencies (`pyyaml`), so it runs on the host with `uv run`. The Docker image installs `--no-dev` and holds neither `evals/` nor `samples/`.
- Without a key the eval runs on the fake provider. That run says nothing about answer quality. `tests/test_evals.py` runs it in `<test database>_evals` and fails when a code change moves its numbers.

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
| Not-found accuracy | An out-of-scope turn comes back `not_found`, for any reason (`no_evidence`, `model_declined` or `unverified`). |
| Key-fact grounding | Insights key facts kept, over kept plus dropped, summed over the three documents. `insights.verify` keeps a fact only when its quote is on the page it cites, so every kept fact is grounded by construction. This number says how often the model cited the wrong page or misquoted, not whether the kept facts are the right ones. |
| Key-fact recall | Planted facts whose text appears in a kept key fact's quote on the same document and page. The denominator is `samples/facts.yaml` without the two running footers (28 facts), because a footer repeated on every page is not a key fact. |
| Suggestions answered | After a case's own turns, every suggestion under its last turn is asked in that chat, the way a click on one asks it, and should come back `answered`. These turns are kept out of every other number: they have no expected facts, so they would count as out of scope and move the gate fit. |
| Cell grounding | Comparison cells kept, over kept plus dropped. `compare.ground` keeps a filled cell only when its quote is on the page it cites, so every kept cell is grounded by construction. Like key-fact grounding, this says how often the model cited the wrong page or misquoted. |
| Difference recall | The comparison case is built in code from `samples/facts.yaml`, not from `cases.yaml`, whose hash is frozen. It compares the policy with the renewal quote, with no focus. Each entry under `changes` (general aggregate limit, medical expense limit, property damage deductible, premium) counts when one row has `differs` true, its policy cell is on the planted page with the planted text inside its quote and its value inside the planted text, and its quote cell likewise. The value check stops a row whose long quote runs on into a neighbouring fact from matching that fact. A change found only as two rows, or with a cell on the wrong page, is a miss. |
| Equal fields kept equal | The same match for each entry under `unchanged` (each occurrence limit), which must come back with `differs` false. It catches a comparison that marks every row as different. |
| Summary source grounding | Each of the three samples is summarised once at each length (brief, standard, detailed), with no focus and the plain tone. For each length, sources kept over kept plus dropped, summed over the samples. `summaries.ground` keeps a source only when its quote is on the page it cites, so this says how often the model cited the wrong page or misquoted. |
| Gate rows | A turn passes the gate when one chunk holds every query term or its best vector similarity reaches `min_similarity`. "Refused" here means nothing passed the gate. An out-of-scope turn that passes can still come back `not_found` from the answer step. |
| Cost and latency | `metrics.processing_stats`, the reader behind `GET /metrics/processing`: `llm_calls` grouped by purpose and model (calls, failed calls, tokens, cost, p95 `latency_ms`), and p95 turn latency over `chat_turns.finished_at - started_at`. Every row in the eval database belongs to the run. |

## The case set

`evals/cases.yaml` holds 36 cases with 44 turns: 28 answerable turns with 34 expected facts, and 16 out-of-scope turns. `evals.load_cases` checks it against `samples/facts.yaml` before a run. Every fact id must exist and sit in the chat's scope. Every fact must be asked or listed under `not_asked` with a reason. Every `absent_topics` entry must appear in an out-of-scope question.

- Questions use a broker's words, not the fact's. A question that repeats a fact's words passes the gate's all-terms arm every time and measures nothing.
- Out-of-scope questions cover each absent topic, then questions that share the documents' vocabulary without being answered by them (an umbrella limit, a producer's commission, a deductible in the endorsement).
- Multi-turn cases include follow-ups ("And the premium?", "Is it shorter if...", "What about...") and standalone topic switches, so a condense rule that rewrites every question cannot score well.
- The case set was written and frozen (sha256 `f9d83a5e793d2650c669aba8211426ec3d3d6e9c96a6caadd17ab82316758d36`) before any calibration. No question was edited afterwards.

## Known limits

- The citation check counts only the planted text. "The General Aggregate Limit is the most we will pay during the policy period" answers its question correctly and still scores as a miss. A model comparing the policy and the quote may cite the quote's change list on page 3 ("increases from $2,500 to $5,000"), which also scores as a miss.
- "CG 21 47 Employment-Related Practices Exclusion" is on page 3 (the schedule) and, as a heading, on page 7. The case asks about the schedule, and a page 7 citation scores as a miss.
- Some out-of-scope questions can be answered by inference ("no flood limit is listed"). Review not-found misses by hand after a run.
- Difference recall counts only the planted changes. There is no precision score for differences beyond the one equal field.
- The gate threshold was set from a calibration run on the same case set, so the gate rows are in-sample.
