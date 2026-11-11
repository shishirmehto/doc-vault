# Evals

`docvault/evals.py` asks a fixed set of broker questions about the three sample documents and scores retrieval, answers, refusals and insights separately, so a miss shows the step where it happened. It also compares the policy with its renewal quote and scores the comparison. The case set is `evals/cases.yaml`, and the expected values come from `samples/facts.yaml`.

## Run the evals

Start the compose Postgres (`docker compose up -d db`), then run the eval on the host:

```bash
DATABASE_URL=postgresql://vault:vault@localhost:54329/vault uv run python -m docvault.evals --out docs/evals.md
```

- The run works in its own database: the `DATABASE_URL` database name plus `_evals` (here `vault_evals`). It drops and recreates that database at the start of every run and never opens `vault` or `vault_test`. Blobs go to a temporary directory.
- With no `OPENAI_API_KEY` the run uses the fake provider. With a key it calls OpenAI, and the spend table shows what the run cost.
- `--out docs/evals.md` rewrites only the text between the results markers below. The prose around them is hand-edited.
- `--calibrate` prints, instead of the results, every turn's standalone query, best vector similarity and whether one chunk matched every query term, then the gate threshold that fits the set best.
- The eval needs the dev dependencies (`pyyaml`), so it runs on the host with `uv run`. The Docker image installs `--no-dev` and holds neither `evals/` nor `samples/`.
- `tests/test_evals.py` runs the same eval on the fake in `<test database>_evals` and pins its numbers.

## Calibrate the OpenAI gate

`OpenAIEmbedder.min_similarity` (0.25) is uncalibrated. To calibrate it, run the eval with a key and read the `Best threshold` line:

```bash
OPENAI_API_KEY=sk-... DATABASE_URL=postgresql://vault:vault@localhost:54329/vault uv run python -m docvault.evals --calibrate
```

Set `OpenAIEmbedder.min_similarity` to that value, then rerun with `--out docs/evals.md`. The gate reads only the embedder, so `CHAT_PROVIDER=fake` in front of the command limits the spend to embeddings. The follow-up turns then use the fake condense, which rewrites them differently from the live model.

## What each number means

Each answerable turn names one or more expected facts. A comparison names one fact per document. Retrieval and citations are scored per expected fact, so a turn can hit one document and miss the other.

| Number | Definition |
|---|---|
| Retrieval hit rate | The expected fact's text is in a source of the expected document that the answer step received, at a position on the expected page. `pages.locate_quote` finds the text and `PageMap.page_at` gives its page. The eval reads the sources with a `ChatBackend` wrapper around `ctx.ai.chat`, so `chat.py` is unchanged. A miss names its reason: `gate` (the relevance gate refused, so there was no answer step), `text` (no source of that document holds the text) or `page` (a source holds it on another page). |
| Citation accuracy | A citation of the expected document has the expected page, and its quote contains the fact's text after collapsing whitespace. An answerable turn that comes back `not_found` misses every fact it expected. |
| Not-found accuracy | An out-of-scope turn comes back `not_found`, for any reason (`no_evidence`, `model_declined` or `unverified`). |
| Key-fact grounding | Insights key facts kept, over kept plus dropped, summed over the three documents. `insights.verify` keeps a fact only when its quote is on the page it cites, so every kept fact is grounded by construction. This number says how often the model cited the wrong page or misquoted, not whether the kept facts are the right ones. |
| Key-fact recall | Planted facts whose text appears in a kept key fact's quote on the same document and page. The denominator is `samples/facts.yaml` without the two running footers (28 facts), because a footer repeated on every page is not a key fact. |
| Suggestions answered | After a case's own turns, every suggestion under its last turn is asked in that chat, the way a click on one asks it, and should come back `answered`. These turns are kept out of every other number: they have no expected facts, so they would count as out of scope and move the gate fit. |
| Cell grounding | Comparison cells kept, over kept plus dropped. `compare.ground` keeps a filled cell only when its quote is on the page it cites, so every kept cell is grounded by construction. Like key-fact grounding, this says how often the model cited the wrong page or misquoted. |
| Difference recall | The comparison case is built in code from `samples/facts.yaml`, not from `cases.yaml`, whose hash is frozen. It compares the policy with the renewal quote, with no focus. Each entry under `changes` (general aggregate limit, medical expense limit, property damage deductible, premium) counts when one row has `differs` true, its policy cell is on the planted page with the planted text inside its quote, and its quote cell likewise. A change found only as two rows, or with a cell on the wrong page, is a miss. |
| Equal fields kept equal | The same match for each entry under `unchanged` (each occurrence limit), which must come back with `differs` false. It catches a comparison that marks every row as different. |
| Gate rows | A turn passes the gate when one chunk holds every query term or its best vector similarity reaches `min_similarity`. "Refused" here means refused by the gate. An out-of-scope turn that passes can still come back `not_found` from the answer step. |
| Cost and latency | `metrics.processing_stats`, the reader behind `GET /metrics/processing`: `llm_calls` grouped by purpose and model (calls, failed calls, tokens, cost, p95 `latency_ms`), and p95 turn latency over `chat_turns.finished_at - started_at`. Every row in the eval database belongs to the run. Under the fake, latency measures Postgres and Python, not a model. |

## The case set

`evals/cases.yaml` holds 36 cases with 44 turns: 28 answerable turns with 34 expected facts, and 16 out-of-scope turns. `evals.load_cases` checks it against `samples/facts.yaml` before a run. Every fact id must exist and sit in the chat's scope. Every fact must be asked or listed under `not_asked` with a reason. Every `absent_topics` entry must appear in an out-of-scope question.

- Questions use a broker's words, not the fact's. A question that repeats a fact's words passes the gate's all-terms arm every time and measures nothing.
- Out-of-scope questions cover each absent topic, then questions that share the documents' vocabulary without being answered by them (an umbrella limit, a producer's commission, a deductible in the endorsement).
- Multi-turn cases include follow-ups ("And the premium?", "Is it shorter if...", "What about...") and standalone topic switches, so a condense rule that rewrites every question cannot score well.
- The case set was written and frozen (sha256 `f9d83a5e793d2650c669aba8211426ec3d3d6e9c96a6caadd17ab82316758d36`) before any calibration. No question was edited afterwards.

## How the fake was calibrated

The fake's gate threshold and its follow-up rule were set from the whole case set, in that order: condense first, because the condense rule changes the standalone queries and so the similarities.

**Condense.** Four rules were scored on every turn, each at its own best threshold:

| Rule for "this question is a follow-up" | Turns right at the gate | Retrieval | Not found |
|---|---|---|---|
| A reference word (`it`, `that`, `they`, ...), the rule before calibration | 33/44 | 21/34 | 15/16 |
| A reference word, or it opens with `and`, `also`, `but`, `or`, `what about` or `how about` | 33/44 | 21/34 | 15/16 |
| The above, or fewer than three content words | 32/44 | 20/34 | 15/16 |
| An opening connective or fewer than three content words, no reference words | 32/44 | 19/34 | 16/16 |

The second rule is now `_fake_condense`. It changes the outcome of one turn. "And the premium?" is now condensed, and its outcome moves from `model_declined` to `answered`. Both rules pass that turn at the gate (the old one by an all-terms match) and retrieve both premiums, and neither rule gets them cited. No scored number moves with the rule. The gains in the results come from the threshold. The content-word count hurt. "Deductible?" stands alone and matches every term in one chunk, and condensing it adds the previous turn's words and loses that match. Dropping reference words fixes one out-of-scope turn but loses two retrievals.

**Gate threshold.** A turn with an all-terms match passes at any threshold, so only the other turns place the cut. The threshold is the middle of the gap that puts the most turns on the right side, with ties going to the wider gap. That gap is between 0.181 (an out-of-scope question about rented equipment) and 0.187 (an answerable cancellation question), so `FakeEmbedder.min_similarity` is 0.184, with 0.003 of margin each side. The old 0.20 put 31 turns on the right side; 0.184 puts 33.

Read that margin as a warning, not a result. The fake embedder hashes words, so it cannot tell "medical bills" from "Medical Expense Limit". Answerable turns score 0.066 to 0.364 and out-of-scope turns 0.051 to 0.235, and the objective moves between 31 and 33 turns for any threshold from 0.166 to 0.203. One reworded question can move the best value. `tests/test_evals.py` fails when it does, so the threshold cannot go stale silently. The threshold was fit on the same turns the results below score, so the gate and not-found numbers are in-sample, not held-out accuracy.

## Known limits

- Citation accuracy under the fake measures `_fake_answer`. It quotes the passage that shares the most words with the query, which is often a heading ("b. Roofing Height Limitation.") or a neighbouring sentence on the right page. Of the 21 facts retrieved, 7 are cited. For 12 the answer quotes another passage, and for 2 it declines.
- The citation check counts only the planted text. "The General Aggregate Limit is the most we will pay during the policy period" answers its question correctly and still scores as a miss. A live model comparing the policy and the quote may cite the quote's change list on page 3 ("increases from $2,500 to $5,000"), which also scores as a miss.
- Condensing appends the whole previous query, so an earlier topic can outvote the new one. "And the premium?" retrieves both premiums, but the fake answer quotes the renewal quote's title and the policy's per-occurrence limit, which the first turn asked about.
- A reference word condenses a question even when "they" means the insured. The flood question in `insured-then-fired-employee` takes the earlier turns' words, passes the gate at 0.235 and is answered: the one out-of-scope miss.
- "How does the deductible differ between the two?" stays `no_evidence`. Its other words ("differ", "two") are in no chunk, so nothing matches every term, and its best similarity is 0.066.
- "CG 21 47 Employment-Related Practices Exclusion" is on page 3 (the schedule) and, as a heading, on page 7. The case asks about the schedule, and a page 7 citation scores as a miss.
- Some out-of-scope questions can be answered by inference ("no flood limit is listed"). Review not-found misses by hand after a live run.
- The fake comparison aligns fields only when two or more documents use the same label, so the premium change ("Total Advance Premium: $18,450" on the policy's page 1, "Total Annual Premium $21,900" on the quote's page 2) gets no row: the one difference-recall miss. A live model is asked to line up fields by meaning and should catch it, but no live run has measured that.
- The fake also marks Estimated Annual Payroll and Rate per $1,000 of Payroll as different. Both are real differences, but `facts.yaml` does not plant them, so they count in no number. There is no precision score for differences beyond the one equal field.
- Under the fake, suggestions are "What is the <label>?" for label-and-value passages, so they repeat across cases ("What is the Policy Number?" follows most policy questions) and 108 of 108 answered says that a label's words find its passage, not that the questions are good ones. A live run measures the model's own follow-ups.

## Results

**Comparisons (2026-11-11, compare-v1).** The comparison adds one `compare` call to the spend and moves no other number. On the policy and the renewal quote the fake returns 12 rows, all 24 cells grounded. It finds 3 of the 4 planted changes, all on page 2, and keeps the each-occurrence limit equal. `tests/test_evals.py` pins these numbers.

**Suggestions (2026-11-11, answer-v2).** Adding follow-up suggestions moved no scored number: retrieval, citations, not-found, grounding, recall and the gate fit are the same as before. The spend counts rose because the eval now asks 108 more turns (three suggestions under each of the 36 cases' last turns), each with a condense, a query embedding and an answer call. `tests/test_evals.py` pins the new counts and the 108 of 108.

**Reading the retrieval number.** Every retrieval miss in the results below is a gate refusal: the case table marks each one `gate`. When a question passes the gate, the expected fact is among the answer step's sources every time (21 of 21 on this run). So under the fake provider the weak step is the gate, which hashes words and cannot match a broker's wording to the document's, not retrieval or ranking. Citation misses are a separate step: the fake answer quotes the sentence that shares the most words with the question, which is often not the planted fact.

<!-- results:start -->
Run 2026-11-11 at commit 033ba80: `uv run python -m docvault.evals --out docs/evals.md`

Provider: chat fake (main fake-chat, small fake-chat); embeddings fake fake-hash-1536; gate min_similarity 0.184.

36 cases, 44 turns: 28 answerable with 34 expected facts, 16 out of scope.

| Metric | Result |
|---|---|
| Retrieval hit rate (expected facts in the answer step's sources) | 21/34 (62%) |
| Citation accuracy (expected facts cited on their page) | 7/34 (21%) |
| Not-found accuracy (out-of-scope turns not answered) | 15/16 (94%) |
| Key-fact grounding (insights facts kept / kept + dropped) | 31/31 (100%) |
| Key-fact recall (planted facts in a kept key fact) | 12/28 (43%) |
| Suggestions answered (each one under a case's last turn, asked in that chat) | 108/108 (100%) |
| Cell grounding (comparison cells kept / kept + dropped) | 24/24 (100%) |
| Difference recall (planted changes found as a differing row, both cells quoting the fact) | 3/4 (75%) |
| Equal fields kept equal (planted unchanged fields found as a row that does not differ) | 1/1 (100%) |
| Gate at 0.184: answerable passed, out-of-scope refused | 18/28 (64%) passed, 15/16 (94%) refused |
| Best gate threshold on this set | 0.184, 33/44 turns on the right side; nearest similarity passed 0.187 (+0.003), nearest refused 0.181 (-0.003) |
| p95 turn latency | 7 ms |

| Purpose | Model | Calls | Errors | Input tokens | Output tokens | Cost (USD) | p95 latency |
|---|---|---|---|---|---|---|---|
| answer | fake-chat | 127 | 0 | 246532 | 3457 | 0.000000 | 3 ms |
| compare | fake-chat | 1 | 0 | 2292 | 169 | 0.000000 | 1 ms |
| condense | fake-chat | 116 | 0 | 15151 | 670 | 0.000000 | 0 ms |
| embed_chunks | fake-hash | 3 | 0 | 2731 | 0 | 0.000000 | 1 ms |
| embed_query | fake-hash | 152 | 0 | 993 | 0 | 0.000000 | 0 ms |
| insights | fake-chat | 3 | 0 | 3096 | 296 | 0.000000 | 1 ms |

Comparison of sample-cgl-policy.pdf with sample-renewal-quote.pdf, no focus:

| Expected field | Matched row | Differs | Policy page | Quote page | Result |
|---|---|---|---|---|---|
| general aggregate limit | General Aggregate Limit | yes | 2 | 2 | found |
| medical expense limit | Medical Expense Limit | yes | 2 | 2 | found |
| property damage deductible | Property Damage Deductible | yes | 2 | 2 | found |
| premium | no row | - | 1 | 2 | no row |
| each occurrence limit | Each Occurrence Limit | no | 2 | 2 | found |

| Case | Question | Interpreted as | Outcome | Retrieval | Citation |
|---|---|---|---|---|---|
| deductible-differ | How does the deductible differ between the two? | (unchanged) | no_evidence | 0/2 (policy-deductible: gate; quote-deductible: gate) | 0/2 (missed policy-deductible; missed quote-deductible) |
| premium-follow-up | What is the per-occurrence limit on the policy and on the renewal quote? | (unchanged) | answered | 2/2 | 1/2 (missed quote-each-occurrence-limit) |
| premium-follow-up | And the premium? | What is the per-occurrence limit on the policy and on the renewal quote? And the premium? | answered | 2/2 | 0/2 (missed policy-premium; missed quote-premium) |
| deductible-differ-explicit | How does the property damage deductible differ between the policy and the renewal quote? | (unchanged) | answered | 2/2 | 1/2 (missed policy-deductible) |
| policy-number | What policy number do I quote when I call the carrier? | (unchanged) | no_evidence | 0/1 (policy-number: gate) | 0/1 (missed policy-number) |
| insured-then-fired-employee | Who is insured under this policy? | (unchanged) | answered | 1/1 | 0/1 (missed policy-named-insured) |
| insured-then-fired-employee | Does the policy cover a claim from someone they fired? | Who is insured under this policy? Does the policy cover a claim from someone they fired? | answered | 1/1 | 0/1 (missed policy-employment-exclusion) |
| insured-then-fired-employee | Are they covered if a flood damages a job site? | Who is insured under this policy? Does the policy cover a claim from someone they fired? Are they covered if a flood damages a job site? | answered | out of scope | out of scope |
| policy-period | When does this policy start and end? | (unchanged) | model_declined | 1/1 | 0/1 (missed policy-period) |
| medical-bills | How much is available for an injured person's medical bills? | (unchanged) | no_evidence | 0/1 (policy-medical-expense-limit: gate) | 0/1 (missed policy-medical-expense-limit) |
| most-paid-in-total | What is the most the policy pays in total during the period? | (unchanged) | answered | 1/1 | 0/1 (missed policy-general-aggregate-limit) |
| completed-operations-aggregate | What is the aggregate for completed operations? | (unchanged) | answered | 1/1 | 1/1 |
| attached-endorsements | Which endorsements are attached to the policy? | (unchanged) | answered | 1/1 | 0/1 (missed policy-form-cg-21-47) |
| defence-of-uncovered-suits | Will the insurer defend a suit for damage the policy does not cover? | (unchanged) | answered | 1/1 | 1/1 |
| roofing-height | Is there a height limit on the roofing jobs they can do? | (unchanged) | answered | 1/1 | 0/1 (missed policy-roofing-height-exclusion) |
| open-roof-rain | What happens if rain gets in through a roof left open overnight? | (unchanged) | answered | 1/1 | 1/1 |
| cancellation-then-nonpayment | How much notice must the insurer give before cancelling? | (unchanged) | answered | 1/1 | 0/1 (missed policy-cancellation-notice) |
| cancellation-then-nonpayment | Is it shorter if the premium goes unpaid? | How much notice must the insurer give before cancelling? Is it shorter if the premium goes unpaid? | answered | 1/1 | 0/1 (missed policy-cancellation-notice) |
| subcontractor-limits | What limits do subcontractors need to carry? | (unchanged) | no_evidence | 0/1 (policy-subcontractor-certificates: gate) | 0/1 (missed policy-subcontractor-certificates) |
| renewal-term-then-deductible | When would the renewal term run? | (unchanged) | no_evidence | 0/1 (quote-period: gate) | 0/1 (missed quote-period) |
| renewal-term-then-deductible | Deductible? | (unchanged) | model_declined | 1/1 | 0/1 (missed quote-deductible) |
| quote-reference | What is the quote reference number? | (unchanged) | no_evidence | 0/1 (quote-number: gate) | 0/1 (missed quote-number) |
| new-exclusions-then-out-of-scope | Is the carrier adding any new exclusions at renewal? | (unchanged) | no_evidence | 0/1 (quote-new-exclusion: gate) | 0/1 (missed quote-new-exclusion) |
| new-exclusions-then-out-of-scope | Does the renewal include terrorism coverage? | (unchanged) | no_evidence | out of scope | out of scope |
| new-exclusions-then-out-of-scope | And cyber liability? | Does the renewal include terrorism coverage? And cyber liability? | no_evidence | out of scope | out of scope |
| aggregate-then-medical | Has the general aggregate gone up at renewal? | (unchanged) | no_evidence | 0/2 (policy-general-aggregate-limit: gate; quote-general-aggregate-limit: gate) | 0/2 (missed policy-general-aggregate-limit; missed quote-general-aggregate-limit) |
| aggregate-then-medical | What about the medical expense cover? | Has the general aggregate gone up at renewal? What about the medical expense cover? | no_evidence | 0/2 (policy-medical-expense-limit: gate; quote-medical-expense-limit: gate) | 0/2 (missed policy-medical-expense-limit; missed quote-medical-expense-limit) |
| endorsement-start | From what date does this endorsement apply? | (unchanged) | answered | 1/1 | 1/1 |
| company-added | Which company is added to the policy by this endorsement? | (unchanged) | answered | 1/1 | 0/1 (missed endorsement-additional-insured) |
| extra-charge | Is there an extra charge for this endorsement? | (unchanged) | no_evidence | 0/1 (endorsement-additional-premium: gate) | 0/1 (missed endorsement-additional-premium) |
| additional-insured-warning | How much warning does the additional insured get if the policy is cancelled? | (unchanged) | answered | 1/1 | 1/1 |
| earthquake | Is there any earthquake cover? | (unchanged) | no_evidence | out of scope | out of scope |
| cyber-breach | Does the policy include cyber liability cover for a data breach? | (unchanged) | no_evidence | out of scope | out of scope |
| aircraft | Is the client covered for using aircraft on a job? | (unchanged) | no_evidence | out of scope | out of scope |
| liquor | Does the policy cover liquor liability at the company party? | (unchanged) | no_evidence | out of scope | out of scope |
| flood-renewal | What does the renewal pay for flood damage? | (unchanged) | no_evidence | out of scope | out of scope |
| umbrella-limit | What is the umbrella or excess liability limit? | (unchanged) | no_evidence | out of scope | out of scope |
| professional-liability-limit | What is the professional liability limit? | (unchanged) | no_evidence | out of scope | out of scope |
| hired-auto | What is the hired and non-owned auto limit? | (unchanged) | no_evidence | out of scope | out of scope |
| rented-equipment | What limit applies to damage to equipment they rent? | (unchanged) | no_evidence | out of scope | out of scope |
| producer-commission | What commission does the producer earn on this policy? | (unchanged) | no_evidence | out of scope | out of scope |
| claims-adjuster | Who is the claims adjuster assigned to this policy? | (unchanged) | no_evidence | out of scope | out of scope |
| endorsement-deductible | What deductible applies under this endorsement? | (unchanged) | no_evidence | out of scope | out of scope |
| business-interruption | What is the waiting period for business interruption claims? | (unchanged) | no_evidence | out of scope | out of scope |

All 108 suggestions came back answered.
<!-- results:end -->
