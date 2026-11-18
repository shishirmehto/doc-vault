# Evals

[`docvault/evals.py`](../docvault/evals.py) asks a fixed set of broker questions about the three sample documents and scores retrieval, answers, refusals and insights separately, so a miss shows the step where it happened. It also compares the policy with its renewal quote and scores the comparison, and summarises each sample at each length and scores the summaries' sources. The case set is [`evals/cases.yaml`](../evals/cases.yaml), a held-out set of extra cases is [`evals/cases-extra.yaml`](../evals/cases-extra.yaml), a page set is [`evals/cases-pages.yaml`](../evals/cases-pages.yaml), and the expected values come from [`samples/facts.yaml`](../samples/facts.yaml). A separate benchmark, [`docvault/retrieval_evals.py`](../docvault/retrieval_evals.py), scores retrieval alone on four long public documents (see Retrieval benchmark on long public documents).

## Results

One run of the full case set on 2026-11-18 at `703cca0`, with the answer prompt `answer-v6`, cost $0.898 on the ledger. The answers came from `gpt-6.1-sol`, the follow-up rewrites from `gpt-6-luna` and the embeddings from `text-embedding-3-small`. The answer step received 16 sources, and the opening passages in chats of up to three documents. No model call failed. The full output is at the end of this page.

| Number | Result |
|---|---|
| Retrieval hit rate | 34/34 (100%) |
| Citation accuracy | 27/34 (79%) |
| Turns cited in full | 24/28 (86%) |
| Not-found accuracy | 14/16 (88%) |
| Key-fact grounding | 36/36 (100%) |
| Key-fact recall | 20/28 (71%) |
| Suggestions answered | 77/77 (100%) |
| Cell grounding | 46/46 (100%) |
| Difference recall | 4/4 (100%) |
| Equal fields kept equal | 1/1 (100%) |
| Summary source grounding | 54/54 (100%), 18 at each length |
| Gate at 0.20: answerable turns passed | 28/28 (100%) |
| Gate at 0.20: out-of-scope turns refused | 0/16 (0%), by design: the gate sits below the lowest answerable turn, and the answer step declined 14 of the 16 |
| p95 turn latency | 7,542 ms |

The seven citation misses are the two facts of `deductible-differ-explicit`, the four of the two turns in `aggregate-then-medical`, and the one of the follow-up "Deductible?" in `renewal-term-then-deductible`. The first six also missed in the `answer-v4` run, where each answer cited the line of the quote's change list on page 3 that states both values, as the Known limits section predicts. "Deductible?" is the new miss.

The two not-found misses are the flood question in `insured-then-fired-employee` and `rented-equipment` ("What limit applies to damage to equipment they rent?"). Both answers say the documents do not establish it, and neither gives a figure from outside the documents. Strict scoring counts them as answered.

The gate passed every turn, so the answer step declined 14 of the 16 out-of-scope turns itself.

The answer step made 121 calls with 410,616 input tokens, 3,393 per call, against 3,242 per call over 133 calls in the `answer-v4` run. With the rules for page numbers, the answer step's system prompt is 2,888 characters long, against 2,360 at `189b3f6`.

`answer-v4` lets the answer use a Markdown list and bold, which the web UI renders, and its markers name evidence items (`[E1]`), so each sentence cites the quote written for it. On 2026-11-18 at `f2243ec` it scored 34/34, 28/34 and 15/16 ($1.24). The plain-text prompt, `answer-v2`, scored 30/34 and 15/16 on 2026-11-17 at `f9f69eb` ($1.08). Four runs on 2026-11-18 moved two turns. `deductible-differ-explicit` cited the change list in three of the four, and `rented-equipment` was answered with related limits in the three runs of `answer-v3`.

### Fewer passages

The samples are small. At the default settings they make 11 chunks (6 for the policy, 2 for the quote, 3 for the endorsement), and the answer step receives up to 16 passages, so every chunk in a chat reaches the answer step and the retrieval hit rate cannot miss. To make retrieval choose, the same case set was run on 2026-11-18 at `703cca0` with 4 passages and 600-character chunks. It cost $0.498.

| Number | 16 passages (above) | 4 passages, 600-character chunks |
|---|---|---|
| Retrieval hit rate | 34/34 (100%) | 30/34 (88%) |
| Citation accuracy | 27/34 (79%) | 25/34 (74%) |
| Turns cited in full | 24/28 (86%) | 23/28 (82%) |
| Not-found accuracy | 14/16 (88%) | 16/16 (100%) |

Retrieval missed four facts, each with the reason `text`: the policy's general aggregate limit in `most-paid-in-total`, the quote's in the first turn of `aggregate-then-medical`, and both medical expense limits in its second turn. That is the cost of a small number of passages, and the reason the default is 16. At `91c96fb`, on `answer-v4`, the same settings found 31/34 and cited 24/34.

### Held-out extra cases

[`evals/cases-extra.yaml`](../evals/cases-extra.yaml) asks 10 more turns, of kinds the main set does not ask (see The case set). Both runs were on 2026-11-18 at `703cca0`, and cost $0.271 and $0.212.

| Number | 16 passages | 4 passages, 600-character chunks |
|---|---|---|
| Retrieval hit rate | 16/16 (100%) | 13/16 (81%) |
| Citation accuracy | 12/16 (75%) | 12/16 (75%) |
| Turns cited in full | 5/8 (62%) | 5/8 (62%) |
| Answer text | 7/7 (100%) | 6/7 (86%) |
| Not-found accuracy | 2/2 (100%) | 1/2 (50%) |
| Gate at 0.20: answerable turns passed | 8/8 (100%) | 8/8 (100%) |

- Start and end of one document. In both runs, `start-end-policy-period-termination` and `start-end-quote-term-exclusion` cited both facts, and `start-end-policy-number-refused-hire` missed the employment exclusion on page 7.
- Four facts from pages 1, 2, 5 and 6 of the policy. At 16 passages all four were cited, with the three expected values in the answer. At 4 passages the per-occurrence limit did not reach the answer step, and the answer lacks "1,000,000".
- The premium rise. Both answers hold $3,450, and both cite neither planted premium. At 4 passages neither premium reached the answer step.
- The date case cited the endorsement's effective date in both runs. The case lists no values, so the answer text is not checked.
- The first turn of `deductible-then-renewal-only` missed the quote's deductible at 16 passages and cited both deductibles at 4. Its follow-up, "Just the renewal one, please.", was rewritten into a standalone question, cited the quote's deductible and held "5,000" in both runs.
- Out of scope. At 16 passages both questions were declined. At 4 passages `renewal-premium-outside-scope` came back answered, and its answer holds none of its forbidden strings.
- The gate threshold was fitted on the main set, and it passed all 8 answerable held-out turns in both runs.

At `91c96fb`, on `answer-v4`, the two runs found 16/16 and 12/16 and cited 12/16 and 11/16.

### Page set

[`evals/cases-pages.yaml`](../evals/cases-pages.yaml) asks 6 questions that name a place in the samples (see The case set). This is its first published run, on 2026-11-18 at `703cca0`.

| Number | 16 passages | 4 passages, 600-character chunks |
|---|---|---|
| Retrieval hit rate | 5/5 (100%) | 3/5 (60%) |
| Citation accuracy | 3/5 (60%) | 3/5 (60%) |
| Answer text | 4/4 (100%) | 4/4 (100%) |
| Not-found accuracy | 1/1 (100%) | 1/1 (100%) |
| Cost | $0.227 | $0.177 |

- "What does page 12 of the policy say?" is declined. The policy has 7 pages.
- At 16 passages the two citation misses are "What is on the last page of the policy?" and "How does the policy end?". The employment exclusion is on page 7, but both answers cite other text on that page.
- At 4 passages and 600-character chunks, page 7 holds more chunks than the 2 sent for a named page, so the exclusion's chunk is not sent, and those two turns miss at retrieval.

## Run the evals

Set `OPENAI_API_KEY` in `.env` and start the compose Postgres (`docker compose up -d db`). Then run the eval on the host. It prints the results.

```bash
DATABASE_URL=postgresql://vault:vault@localhost:54329/vault uv run python -m docvault.evals
```

- The run works in its own database: the `DATABASE_URL` database name plus `_evals` (here `vault_evals`). It drops and recreates that database at the start of every run and never opens `vault` or `vault_test`. Blobs go to a temporary directory.
- `--cases evals/cases-extra.yaml` runs the held-out extra cases instead of the main set, and `--cases evals/cases-pages.yaml` runs the page set.
- `--out docs/evals.md` also writes the results between the results markers at the end of this page. The prose around them is hand-edited.
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

The 2026-11-12 run did not use the best-fit value. Answerable turns scored 0.239 to 0.606 and out-of-scope turns 0.257 to 0.514, so the classes overlap and no threshold separates them. The best fit, 0.518, put 32 of 44 turns on the right side, but it refused 12 of the 28 answerable turns. A refused answerable question costs a broker more than one answer call, so the threshold sits below the lowest answerable turn, at 0.20. On the full live run at `703cca0`, the gate at 0.20 passed all 44 turns, including the 16 out-of-scope turns. The answer step then declined 14 of those 16. That run's best fit was 0.521, with 32 of 44 turns on the right side.

## What each number means

Each answerable turn names one or more expected facts. A comparison names one fact per document. Retrieval and citations are scored per expected fact, so a turn can hit one document and miss the other.

| Number | Definition |
|---|---|
| Retrieval hit rate | The expected fact's text is in a source of the expected document that the answer step received, at a position on the expected page. `pages.locate_quote` finds the text and `PageMap.page_at` gives its page. The eval reads the sources from the turn's retrieval log (`chat.turn_retrieval`), and `chat.sent_sources` rebuilds their text from the chunks the log names. A miss names its reason: `gate` (nothing passed the relevance gate, so the answer step saw only the opening passages), `text` (no source of that document holds the text) or `page` (a source holds it on another page). |
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
| Gate rows | A turn passes the gate when one chunk holds every query term, its best vector similarity reaches `min_similarity`, or it names a page or a section that its documents have. "Refused" here means nothing passed the gate. An out-of-scope turn that passes can still come back `not_found` from the answer step. |
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

[`evals/cases-pages.yaml`](../evals/cases-pages.yaml) holds 6 cases with 6 turns: 5 answerable turns with 5 expected facts, and 1 out-of-scope turn. It was written on 2026-11-18 for questions that name a place in a document: page 3 and page 5 of the policy, the last page of the policy and of the quote, how the policy ends, and page 12 of the 7-page policy. It uses only facts already in `facts.yaml`, sets `partial:`, and was frozen (sha256 `6d0378979f876a22bf6d35f9f851972d897d3c2a10cb4ccefb4bbabf8a2418c6`) before its first run. A turn that names a page or a section its documents have passes the gate, as in `chat.ask`.

## Retrieval benchmark on long public documents

The 44 sample questions sit on 11 chunks, so at 16 passages retrieval cannot miss on them. [`docvault/retrieval_evals.py`](../docvault/retrieval_evals.py) scores retrieval alone on four public-domain PDFs, 2,633 pages in all:

| Document | Pages |
|---|---|
| Crop insurance handbook, 2025 (FCIC-18010-1) | 922 |
| Affordable Care Act (Public Law 111-148) | 906 |
| Loss adjustment standards handbook, 2025 edition (FCIC-25010) | 400 |
| Loss adjustment standards handbook, 2026 edition (FCIC-25010) | 405 |

The PDFs are not in the repository. [`evals/retrieval/documents.yaml`](../evals/retrieval/documents.yaml) lists each one's URL, licence and sha256, and the runner refuses a file whose sha256 differs.

### The questions

The benchmark asks 53 questions, in five files under [`evals/retrieval/`](../evals/retrieval/). Each question names its expected document and pages. The manifest freezes each file by its sha256.

- [`handbook-retrieval.yaml`](../evals/retrieval/handbook-retrieval.yaml) holds 24 questions on the crop insurance handbook, written for this benchmark before any run.
- `crop.yaml`, `aca.yaml`, `lash.yaml` and `versions.yaml` hold 29 questions converted from the scale test's verified question sets, with their text, ids and expected pages unchanged.

Expected pages are PDF page numbers, counted from 1, not the numbers printed in the footers.

### What it measures

A run indexes the documents in its own database, `<DATABASE_URL name>_retrieval`, which it drops and recreates each time. It embeds every question and runs `chat.search` over the question's documents, as a chat's first question does. For each question it records:

- hit@4, hit@8 and hit@16: `chat.select_sources` at that k holds a passage of the expected document on an expected page;
- the fused rank of the first such passage among the retrieved candidates, up to 50, and its rank by vector similarity alone;
- whether the gate passes.

MRR@50 averages 1/rank over the questions, and a miss counts 0. The benchmark calls embeddings only, no chat model, so a run costs about $0.04.

### Run the benchmark

Start the compose Postgres (`docker compose up -d db`) and run on the host:

```bash
scripts/fetch-public-docs.sh    # downloads the PDFs into evals/retrieval/docs and checks each sha256
DATABASE_URL=postgresql://vault:vault@localhost:54329/vault uv run python -m docvault.retrieval_evals --docs evals/retrieval/docs
```

- With `OPENAI_API_KEY` set in `.env`, it embeds with OpenAI. Without a key it runs on the fake embedder, and the report says that those numbers say nothing about retrieval quality.
- `--cases FILE ...` runs only the listed case files.
- `--out FILE` writes the Markdown report and, beside it, the per-question JSON that two runs are compared by.
- `--docs evals/retrieval/docs --verify` checks, with no database and no key, that each document has the manifest's page count, and that each converted question's quote and values are on its expected pages. The 24 handbook questions carry no quote, so it skips them.
- `CHUNK_MAX_CHARS` and `CHUNK_OVERLAP_CHARS` in front of the command set the chunking, so a sweep is several runs.

### Results

| Commit | hit@4 | hit@8 | hit@16 | MRR@50, fused |
|---|---|---|---|---|
| `e84c72e`, full-text rank at full weight | 38/53 | 44/53 | 49/53 | 0.616 |
| `703cca0`, full-text rank at half weight | 42/53 | 48/53 | 52/53 | 0.640 |

Both runs were on 2026-11-18, with `text-embedding-3-small` and 1800-character chunks with 250 characters of overlap. The `703cca0` run took 84 s and cost $0.0368. Its report is [`evals/retrieval/results/2026-11-18.md`](../evals/retrieval/results/2026-11-18.md).

Ranking by vector similarity alone scores a higher MRR@50, 0.658. What the answer step sees depends on hit@16, and there the fused ranking is ahead. In the comparison below, vector-only ranking finds 50 and the half weight 51. `lash-01` is the one question still missed at 16 passages. The first passage on its page is at fused rank 35, and outside the top 50 by vector similarity.

### What the benchmark decided

These experiments ran on 2026-11-18 on the same 53 questions. A scratch script ranked plain top k on the same candidates, so its counts differ from the benchmark's own by at most 1.

| Variant | hit@4 | hit@8 | hit@16 | Verdict |
|---|---|---|---|---|
| Fusion as it was, full-text rank at full weight | 37 | 43 | 49 | replaced |
| Full-text rank at half weight | 41 | 47 | 51 | chosen |
| Full-text rank at quarter weight | 42 | 47 | 51 | not chosen |
| All-terms rank only | 42 | 44 | 50 | rejected |
| Vector only | 42 | 44 | 50 | rejected |

The full-text rank ORs every question word, so a long broker question matches loosely related passages, and at full weight those passages outrank the right one. The quarter weight ties the half weight at 8 and 16 passages. The code keeps the half weight, which the comment on `LEXICAL_WEIGHT` in [`docvault/chat.py`](../docvault/chat.py) says still lifts an exact identifier such as "CG 21 47". On 42 facts from the samples, every variant kept all 42 within 16 passages with 1800-character chunks, and 41 with 600-character chunks. At 4 passages, the half weight cost 3 facts with 1800-character chunks and 1 with 600-character chunks.

Three more changes were tested at half weight, and one was not built. The first two give the scratch script's counts.

- **Repeated page headers and footers left out of chunks.** Rejected. It lowered hit@16 from 51 to 49 and raised hit@4 from 41 to 42. It removes about 1.5% of the text on the long documents, and 14 to 20% on the samples.
- **The section path in each chunk's embedded text.** Rejected. On the 53 questions, hit@16 was 51 with and without it, and MRR@50 was 0.640 without it against 0.630 with it. On the earlier 24-question handbook set, hit@16 was 22/24 both ways.
- **Chunk size and overlap.** Kept at 1800/250. These are the benchmark's own counts:

  | Size / overlap (characters) | hit@4 | hit@8 | hit@16 |
  |---|---|---|---|
  | 1800/250 (kept) | 42 | 48 | 52 |
  | 1200/200 | 39 | 46 | 51 |
  | 2400/300 | 39 | 45 | 49 |
  | 1800/0 | 40 | 45 | 49 |
  | 1800/500 | 42 | 48 | 50 |

  1800/250 is best or tied at each k. At 1800/250, sending 8 passages instead of 16 would lose 4 of the 53, so k stays 16.
- **A reranker.** Not built. After the half weight, the benchmark misses 1 of the 53 at 16 passages (`lash-01`, at rank 35), so a reranker could gain at most 1. The scratch script, which skips the per-document share, misses 2. OpenAI has no rerank endpoint, so a reranker would mean a model call per question or a local model in the image.

## Known limits

- The citation check counts only the planted text. "The General Aggregate Limit is the most we will pay during the policy period" answers its question correctly and still scores as a miss. A model comparing the policy and the quote may cite the quote's change list on page 3 ("increases from $2,500 to $5,000"), which also scores as a miss.
- "CG 21 47 Employment-Related Practices Exclusion" is on page 3 (the schedule) and, as a heading, on page 7. The case asks about the schedule, and a page 7 citation scores as a miss.
- Some out-of-scope questions can be answered by inference ("no flood limit is listed"). Review not-found misses by hand after a run.
- Difference recall counts only the planted changes. There is no precision score for differences beyond the one equal field.
- The gate threshold was set from a calibration run on the same case set, so the gate rows are in-sample. The extra cases are out of sample.
- At the default settings, retrieval cannot miss on the samples (see Fewer passages). The run with 4 passages and the retrieval benchmark measure ranking.
- The full-text weight was chosen, and the chunk size kept, on the benchmark's 53 questions, so its `703cca0` numbers are in-sample.
- The retrieval benchmark checks that a passage on an expected page reaches the answer step, not that an answer uses it.
- The answer-text check is a string check. It confirms figures and catches a forbidden name, but it cannot judge a yes or no, or tell an answer that corrects a false premise from one that accepts it.
- The answer cases are 60 questions on three short samples: 44 in the main set, 10 held out and 6 in the page set. On long documents, the current prompt was checked on 10 live handbook questions, and the 91 real-document asks ran on `answer-v2`.
- Every number comes from one run. Four runs on 2026-11-18 moved two turns, so a difference of one or two facts between two runs is within the noise.

## Full output of the latest run

This is the `703cca0` run, case by case. `--out docs/evals.md` rewrites the text between the results markers.

<!-- results:start -->
Run 2026-11-18 at commit 703cca0: `uv run python -m docvault.evals`

Provider: chat openai (main gpt-6.1-sol, small gpt-6-luna); embeddings openai text-embedding-3-small-1536; gate min_similarity 0.2.

36 cases, 44 turns: 28 answerable with 34 expected facts, 16 out of scope.

| Metric | Result |
|---|---|
| Retrieval hit rate (expected facts in the answer step's sources) | 34/34 (100%) |
| Citation accuracy (expected facts cited on their page) | 27/34 (79%) |
| Turns cited in full (answerable turns with every expected fact cited) | 24/28 (86%) |
| Answer text (turns holding every expected value and no forbidden one) | n/a |
| Not-found accuracy (out-of-scope turns not answered) | 14/16 (88%) |
| Key-fact grounding (insights facts kept / kept + dropped) | 36/36 (100%) |
| Key-fact recall (planted facts in a kept key fact) | 20/28 (71%) |
| Suggestions answered (each one under a case's last turn, asked in that chat) | 77/77 (100%) |
| Cell grounding (comparison cells kept / kept + dropped) | 46/46 (100%) |
| Difference recall (planted changes found as a differing row, both cells quoting the fact) | 4/4 (100%) |
| Equal fields kept equal (planted unchanged fields found as a row that does not differ) | 1/1 (100%) |
| Summary source grounding (sources kept / kept + dropped, three samples, by length) | brief 18/18 (100%), standard 18/18 (100%), detailed 18/18 (100%) |
| Gate at 0.2: answerable passed, out-of-scope refused | 28/28 (100%) passed, 0/16 (0%) refused |
| Best gate threshold on this set | 0.521, 32/44 turns on the right side; nearest similarity passed 0.529 (+0.008), nearest refused 0.514 (-0.007) |
| p95 turn latency | 7542 ms |

| Purpose | Model | Calls | Errors | Input tokens | Output tokens | Cost (USD) | p95 latency |
|---|---|---|---|---|---|---|---|
| answer | gpt-6.1-sol | 121 | 0 | 410616 | 21428 | 0.798632 | 5971 ms |
| compare | gpt-6.1-sol | 1 | 0 | 3784 | 2428 | 0.031848 | 21380 ms |
| condense | gpt-6-luna | 85 | 0 | 25049 | 2032 | 0.003527 | 2011 ms |
| embed_chunks | text-embedding-3-small | 3 | 0 | 3774 | 0 | 0.000076 | 1926 ms |
| embed_query | text-embedding-3-small | 121 | 0 | 1303 | 0 | 0.000007 | 481 ms |
| insights | gpt-6.1-sol | 3 | 0 | 5164 | 2514 | 0.025673 | 14212 ms |
| summary | gpt-6.1-sol | 9 | 0 | 14088 | 3712 | 0.038581 | 9803 ms |

Comparison of sample-cgl-policy.pdf with sample-renewal-quote.pdf, no focus:

| Expected field | Matched row | Differs | Policy page | Quote page | Result |
|---|---|---|---|---|---|
| general aggregate limit | General Aggregate Limit | yes | 2 | 2 | found |
| medical expense limit | Medical Expense Limit | yes | 2 | 2 | found |
| property damage deductible | Property Damage Deductible | yes | 2 | 2 | found |
| premium | Premium (Advance / Annual) | yes | 1 | 2 | found |
| each occurrence limit | Each Occurrence Limit | no | 2 | 2 | found |

| Case | Question | Interpreted as | Outcome | Retrieval | Citation | Answer text |
|---|---|---|---|---|---|---|
| deductible-differ | How does the deductible differ between the two? | (unchanged) | answered | 2/2 | 2/2 | - |
| premium-follow-up | What is the per-occurrence limit on the policy and on the renewal quote? | (unchanged) | answered | 2/2 | 2/2 | - |
| premium-follow-up | And the premium? | What is the premium on the policy and on the renewal quote? | answered | 2/2 | 2/2 | - |
| deductible-differ-explicit | How does the property damage deductible differ between the policy and the renewal quote? | (unchanged) | answered | 2/2 | 0/2 (missed policy-deductible; missed quote-deductible) | - |
| policy-number | What policy number do I quote when I call the carrier? | (unchanged) | answered | 1/1 | 1/1 | - |
| insured-then-fired-employee | Who is insured under this policy? | (unchanged) | answered | 1/1 | 1/1 | - |
| insured-then-fired-employee | Does the policy cover a claim from someone they fired? | Does the policy cover a claim from someone Example Contractors LLC fired? | answered | 1/1 | 1/1 | - |
| insured-then-fired-employee | Are they covered if a flood damages a job site? | Does the policy cover Example Contractors LLC if a flood damages a job site? | answered | out of scope | out of scope | - |
| policy-period | When does this policy start and end? | (unchanged) | answered | 1/1 | 1/1 | - |
| medical-bills | How much is available for an injured person's medical bills? | (unchanged) | answered | 1/1 | 1/1 | - |
| most-paid-in-total | What is the most the policy pays in total during the period? | (unchanged) | answered | 1/1 | 1/1 | - |
| completed-operations-aggregate | What is the aggregate for completed operations? | (unchanged) | answered | 1/1 | 1/1 | - |
| attached-endorsements | Which endorsements are attached to the policy? | (unchanged) | answered | 1/1 | 1/1 | - |
| defence-of-uncovered-suits | Will the insurer defend a suit for damage the policy does not cover? | (unchanged) | answered | 1/1 | 1/1 | - |
| roofing-height | Is there a height limit on the roofing jobs they can do? | (unchanged) | answered | 1/1 | 1/1 | - |
| open-roof-rain | What happens if rain gets in through a roof left open overnight? | (unchanged) | answered | 1/1 | 1/1 | - |
| cancellation-then-nonpayment | How much notice must the insurer give before cancelling? | (unchanged) | answered | 1/1 | 1/1 | - |
| cancellation-then-nonpayment | Is it shorter if the premium goes unpaid? | How much notice must the insurer give before cancelling if the premium goes unpaid? | answered | 1/1 | 1/1 | - |
| subcontractor-limits | What limits do subcontractors need to carry? | (unchanged) | answered | 1/1 | 1/1 | - |
| renewal-term-then-deductible | When would the renewal term run? | (unchanged) | answered | 1/1 | 1/1 | - |
| renewal-term-then-deductible | Deductible? | What is the deductible? | answered | 1/1 | 0/1 (missed quote-deductible) | - |
| quote-reference | What is the quote reference number? | (unchanged) | answered | 1/1 | 1/1 | - |
| new-exclusions-then-out-of-scope | Is the carrier adding any new exclusions at renewal? | (unchanged) | answered | 1/1 | 1/1 | - |
| new-exclusions-then-out-of-scope | Does the renewal include terrorism coverage? | (unchanged) | model_declined | out of scope | out of scope | - |
| new-exclusions-then-out-of-scope | And cyber liability? | Does the renewal include cyber liability coverage? | model_declined | out of scope | out of scope | - |
| aggregate-then-medical | Has the general aggregate gone up at renewal? | (unchanged) | answered | 2/2 | 0/2 (missed policy-general-aggregate-limit; missed quote-general-aggregate-limit) | - |
| aggregate-then-medical | What about the medical expense cover? | What about the medical expense coverage in the renewal quote? | answered | 2/2 | 0/2 (missed policy-medical-expense-limit; missed quote-medical-expense-limit) | - |
| endorsement-start | From what date does this endorsement apply? | (unchanged) | answered | 1/1 | 1/1 | - |
| company-added | Which company is added to the policy by this endorsement? | (unchanged) | answered | 1/1 | 1/1 | - |
| extra-charge | Is there an extra charge for this endorsement? | (unchanged) | answered | 1/1 | 1/1 | - |
| additional-insured-warning | How much warning does the additional insured get if the policy is cancelled? | (unchanged) | answered | 1/1 | 1/1 | - |
| earthquake | Is there any earthquake cover? | (unchanged) | model_declined | out of scope | out of scope | - |
| cyber-breach | Does the policy include cyber liability cover for a data breach? | (unchanged) | model_declined | out of scope | out of scope | - |
| aircraft | Is the client covered for using aircraft on a job? | (unchanged) | model_declined | out of scope | out of scope | - |
| liquor | Does the policy cover liquor liability at the company party? | (unchanged) | model_declined | out of scope | out of scope | - |
| flood-renewal | What does the renewal pay for flood damage? | (unchanged) | model_declined | out of scope | out of scope | - |
| umbrella-limit | What is the umbrella or excess liability limit? | (unchanged) | model_declined | out of scope | out of scope | - |
| professional-liability-limit | What is the professional liability limit? | (unchanged) | model_declined | out of scope | out of scope | - |
| hired-auto | What is the hired and non-owned auto limit? | (unchanged) | model_declined | out of scope | out of scope | - |
| rented-equipment | What limit applies to damage to equipment they rent? | (unchanged) | answered | out of scope | out of scope | - |
| producer-commission | What commission does the producer earn on this policy? | (unchanged) | model_declined | out of scope | out of scope | - |
| claims-adjuster | Who is the claims adjuster assigned to this policy? | (unchanged) | model_declined | out of scope | out of scope | - |
| endorsement-deductible | What deductible applies under this endorsement? | (unchanged) | model_declined | out of scope | out of scope | - |
| business-interruption | What is the waiting period for business interruption claims? | (unchanged) | model_declined | out of scope | out of scope | - |

All 77 suggestions came back answered.
<!-- results:end -->
