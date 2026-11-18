# How AI tools were used

I built this project with Claude Code, Anthropic's agentic coding tool, and the coder-stack plugin. coder-stack is a set of skills for design bakeoffs, phased delegation, verification and decision logs.

In this document, "I" is me, the human. "The lead agent" is the Claude Code session that planned the work, briefed the other agents and accepted their work. "Owner agents" are the agents that wrote code, one per phase. The agents wrote almost all of the code, tests and documents. I set the scope and made the product calls. The lead agent accepted each phase only after it had run the product itself; an owner's report alone was never enough.

## The workflow

1. **Plan.** The lead agent read the case study and wrote a plan with the decisions for me to take:
   - FastAPI;
   - pgvector in the same Postgres;
   - a hand-written retrieval module, for exact page citations;
   - asking for an API key and building against a fake model until it arrives.

   I took the recommended option on each.
2. **Design bakeoff.** Three runners, each on a different model, sketched the whole backend from the same task: schema, endpoints, document lifecycle, retrieval and prompts. A read-only judge on another model scored the sketches against a rubric. The lead agent took the strongest sketch as the base and grafted in the best parts of the other two.

   The web UI went through the same process, and the judge scored the three sketches 27, 21 and 17. The lead agent first preferred the smallest design. The judge's evidence changed the pick: that design had extra page hops in the demo path and a quote matcher that drifted from the backend's.
3. **Phased build.** Each phase had one owner agent with a written brief. Each phase was split into units, and each unit ended with the tests green and one commit:
   - phase 1: scaffold, parsing, crash-safe indexing;
   - phase 2: the AI gateway, insights, chat;
   - phase 3: metrics, hardening, evals, the demo script, the docs;
   - the web UI, in three passes, then a design pass;
   - the bonus features, one phase each: streamed answers and follow-up suggestions, live document status, side-by-side comparison, summary options, document versions, then caching, a daily spending cap and request logs;
   - two more UI passes: an audit, and a final pass that brought every feature into one layout;
   - a quality pass (`/simplify` and a comment review) over the bonus phases that had skipped it.

   The phases ran one at a time because they shared files, the dev stack and the test database.
4. **Parallel work without collisions.** The UI was built on its own branch in a separate git worktree, at the same time as the backend agent worked on `main`, and merged in `cf378d7`. The UI agents used the running API read-only and never restarted containers.
5. **Independent acceptance.** Before accepting a phase, the lead agent reran the full test suite. It also asked the live API its own questions and compared the answers with [`samples/facts.yaml`](samples/facts.yaml), and it used the UI in a browser. Several findings below came from this step.

   For the bonus phases, the lead agent drove each feature itself on the running stack before accepting it:
   - it uploaded a file with `curl` and watched it reach Ready in an open browser with no reload;
   - it compared the policy with its renewal quote and opened a cell's page;
   - it rewrote a summary twice and counted the ledger rows;
   - it uploaded a second version under the same name;
   - it asked one first question in two chats and checked that the second made no model call.

## Verification beyond unit tests

- **Evals** ([`docs/evals.md`](docs/evals.md)). Retrieval, citations, refusals and insights are scored separately, so a miss shows the step where it happened. The relevance threshold was chosen on a frozen case set, never on a single question.
- **Real documents.** Before the first live run below, 19 public insurance documents ran through an isolated stack on the fake provider:
  - policy forms in two editions;
  - declarations pages;
  - cyber and professional liability wordings;
  - a 246-page flood manual;
  - a Word checklist built as one long table.

  The documents are not in the repo, because some are copyrighted. Every page count matched an independent PDF reader, and the text matched within 1% on all but one file. That file's only gap is a regulator's stamp line. Each answer's citation was checked against that reader's text of the cited page. The run found three bugs that the generated samples could not show:
  - an encrypted PDF that anyone can open failed as damaged;
  - text printed twice for bold came out as "PPoolliiccyy";
  - words broken at line ends read as "infor- mation".

  All three were fixed test-first and checked again on the same files.
- **A formal model of the document lifecycle** ([`formal/`](formal/), [`docs/lifecycle.md`](docs/lifecycle.md)). A TLA+ model covers one document moving through indexing while workers crash, the provider fails, jobs arrive twice and the user presses retry. The TLC model checker tries every order of those events within small bounds, against seven rules, such as "a document never shows Failed while a worker is still processing it". The model found a real race, F1, before any test did. It also rejected the first fix idea with a counterexample in which an unparseable file loops forever. Each rule is also shown to fail on a deliberately broken copy of the model (`python3 formal/check.py --mutants`). A project skill, [`.claude/skills/verify-lifecycle-model/`](.claude/skills/verify-lifecycle-model/), tells the next agent when a code change needs the model rechecked. Its first use was the phase 3 change to reconcile.
- **Repro-first bug fixing.** The F1 fix started with a test against real Postgres advisory locks ([`tests/test_lifecycle_race.py`](tests/test_lifecycle_race.py)), committed as an expected failure (`6c1ad38`). It failed on the old code for all three ways a run can fail, then passed after the fix (`3437628`).
- **An app verification skill** ([`.claude/skills/verify-doc-vault/`](.claude/skills/verify-doc-vault/)). It starts an isolated stack on its own ports and volumes, checks it, and drives each feature through the public API or the browser. It saves the evidence and removes only what it started. Its first run found two bugs in its own health-check script, and both were fixed before it was committed.
- **UI runs** with Playwright, with screenshots and a log of every input event, so a stray click could not pass as a result.
- **A visibility check on every screen** ([`.claude/skills/verify-doc-vault/scripts/visible.js`](.claude/skills/verify-doc-vault/scripts/visible.js)). It lists clipped text, controls that no scroll can reach, and sideways page scroll. The final UI pass ran it on 53 screens at four window sizes, and it returned nothing on any of them.

## The live runs

Two runs used the OpenAI key.

The first, on 2026-11-12, sent eight passages to the answer step. It cost $3.17 on the ledger, $1.18 of it for the eval set. It covered:
- **The eval set.** Retrieval found 34 of 34 expected facts, 30 of 34 were cited on the right page, and 15 of 16 out-of-scope questions were declined. The 2026-11-17 rerun gave the same three numbers, and [`docs/evals.md`](docs/evals.md) reports that rerun.
- **The 19 real documents.** Before any paid call, the lead agent wrote the expected answer and page for 31 broker questions from the documents' own text. Six of the questions have no answer in their document.
  - Of the 25 answerable questions, 21 were answered right.
  - 4 were partial:
    - the waiting period in the 246-page manual was not retrieved, and the answer said so;
    - two answers on ransomware cover missed that remediation costs include extortion costs;
    - one answer on defamation cited only the legal-expenses section and missed the professional liability cover for statements made in good faith.
  - Of the 6 questions with no answer in their document, 5 were declined. The sixth, on pets under workers' compensation, was answered "No" by inference from the coverage grant.
  - No answer invented a figure. That held on a certificate whose limits are blank.
  - Every citation was on the page it named. Where a quote's words differed from a reference reader's, the page held the same letters with different spacing.
- **Other features on the real documents:**
  - 7 comparisons took 15 to 28 s, inside the 90 s limit, with 1 cell dropped out of about 320;
  - 4 summaries took 4 to 16 s, at the requested lengths and focus, with no source dropped;
  - streamed answers arrived in about 90 pieces, the first after 2.7 s.

What the first run left open:
- Three limits that the README lists: "Differs" compares text and not meaning, a chat sent eight passages to the answer step (16 since 2026-11-17), and insights read the first 240,000 characters.
- A word broken at a line end, with a stray space inside it, stays split ("se t-tlements") when the document never uses it whole. That happens in the oldest standard forms.
- The eval's case set was written for the sample documents. The 31 real-document questions are a hand-graded spot check, not a scored set.

The second, on 2026-11-17 at `f9f69eb`, sent 16 passages to the answer step, and the opening passages in chats of up to three documents. It covered:
- The eval set again, for $1.08 on the ledger. `docs/evals.md` reports this run.
- 23 real insurance documents, 3,263 pages, on a separate copy of the stack. 50 questions were asked 91 times, in chats of 1, 3 and 23 documents. Of the 80 asks with an answer in the documents, a script passed 71 with the expected value cited on an expected page, and a review of each miss passed 2 more. The other 7 were declined. Of the 11 asks with no answer, 8 came back `not_found` and 3 declined in words. [`docs/design.md`](docs/design.md) has the details, under Real documents.

## Where the AI was wrong, and what caught it

| What went wrong | What caught it | Shown in |
|---|---|---|
| The first F1 fix would loop forever on a file that never parses | The model checker's counterexample | Finding F1 in [`docs/lifecycle.md`](docs/lifecycle.md); the fix is `3437628` |
| While changing comparisons, a fixer agent removed the repair turn for insights | The phase owner's review of the fixer's diff. A table test now pins every AI purpose's retries, repair and timeout | `a9842fe`, and `test_each_model_purpose_keeps_its_attempts_repair_turn_and_timeout` in [`tests/test_ai.py`](tests/test_ai.py) |
| Streamed answers arrived all at once at the end | Measuring the gap between pieces through the web server: its rewrite compressed the stream. The API now marks the stream as not to be transformed | `d4d795b` |
| The fix for doubled bold text would also have halved template placeholders, so "YYYY" read "YY" | The lead agent ran the new extraction on the real documents before accepting it. The flood manual has seven "YYYY" date fields | `541fd48` |
| The calibration's best-fit gate threshold (0.518) would have refused 12 of 28 answerable questions | The lead agent tabulated how many answerable and out-of-scope turns each threshold passes, and set 0.20, below the lowest answerable turn | `5bf0f25`, and Calibrate the OpenAI gate in [`docs/evals.md`](docs/evals.md) |
| After a delete, a capped document list shrank and stayed short | An independent review agent. The list now reads again after a delete | `c591734` |
| The first citation check on real documents flagged correct quotes as off the page | Reading the pages: the reference reader's layout mode had merged two columns. The check switched to reading order and word overlap | A scratch script, run outside the repo |
| The question box disappeared in my own browser on a chat with three turns, and the lead agent could not reproduce it | Anything placed above the app, such as a browser extension's bar or a margin on the page, pushed the box off screen. The UI audit pass reproduced this by injecting such elements and rebuilt the layout so the content scrolls above a fixed footer row | `9655412` |
| The spending-cap docs said search keeps working over the cap, but chat is the only search, and it is capped | The second owner of that phase. The docs now say uploads are still indexed | `9c5ebe8` |
| Every agent role first ran on the most expensive model | I set a model per role: cheaper models for exploring, the strongest for judgement | The agent settings, kept outside the repo |
| The UI design runners could read the judge's rubric | The runners' own reports. The rubric moved out of their folder, and the pick was checked against the judge's evidence, not the scores | The design bakeoff, run outside the repo |
| An automated comment-removal pass deleted comments that explain non-obvious reasons | The owner agent reviewed the edits and reverted them | `85e5034` lists the comments that stayed |
| The evals page opened with the fake provider's full results, so its $0 cost rows read as the main result | My reading of the published page. The eval docs now report the OpenAI run only | `7e598b1`, `53c4250` |
| The docs said the OpenAI gate refuses off-topic questions, but on the eval set it let all 44 questions through, and the answer step declined the out-of-scope ones | An agent that re-read the live run's output while the evals page was rewritten | `7e598b1` |
| The gate refused "What is this about?" on a 922-page handbook, because a question about a whole document scores as low as an off-topic one | My own test in the browser. When nothing matches, the answer step now gets each document's opening passage | `f9f69eb` |
| To show one citation number per sentence, a change paired a source's markers with its quotes by position, but the model lists quotes in any order, so a sentence could cite another sentence's quote | An independent review agent reproduced it, then broke both fixes that followed: matching by shared figures and words, and a one-to-one check on those matches. The change was withdrawn, and each marker again shows every verified quote of its source | `613dfb5`, `a6e39d7`, `0ccc135` |
| A change to text decoding read cp1252 text with an accent next to a curly quote as broken UTF-8, so French accents became U+FFFD | The same review agent, with a reproduction | `613dfb5` |
| The README gave the real-document scores after review only, while the run's own report treats the strict scores as the headline | A grader agent that scored the repo against the case study | `bbe6354` |

## AI inside the product

- Every prompt lives in [`docvault/prompts.py`](docvault/prompts.py). Each is versioned and carries a note on why it is worded the way it is.
- **Grounding.** Every quote in an answer is located on its cited page, and the page's own text replaces the model's copy. Citations that cannot be found are dropped. Key facts in insights must quote their page, and those that don't are dropped and counted.
- **Relevance gate.** It runs before the answer call. A question whose best match falls below the threshold goes to the answer step with only each document's opening passage, which answers "What is this about?" and refuses the rest. On the eval set every question passed it, so the answer step declined the out-of-scope ones.
- **Follow-ups.** Each follow-up is rewritten into a standalone question, which is stored and returned as `interpreted_as`.
- **Ledger.** Every provider call is written to an append-only ledger with its tokens and cost. The metrics endpoints read it, so cost and latency are measured, not estimated.
- **Fake provider.** A deterministic fake lets the tests and a run with no key work without a model. Answer quality is reported only for the OpenAI models.
- **Comparisons.** Every comparison cell must quote its page, or it is dropped. Whether two values differ is computed in code, not by the model.

## What I decided

- The scope, and the decisions in the plan.
- The model-per-role sheet for the agents.
- Adding the web UI, the lifecycle chart as documentation, the verification skills, and the F1 fix.
- Adding all the bonus features, and the rule that everything in the UI must be visible: nothing clipped, hidden, or reachable only by URL.
- No paid model calls until a key was provided. Everything ran on the fake provider until then.
- Testing on real insurance documents as well as the generated samples. That run found the three parsing bugs above.
- Adding Up and Down recall of earlier questions, and answers that show in every open tab of a chat, after testing the UI in two tabs.
- Reporting answer quality on the OpenAI models only. The fake provider's numbers say nothing about answers, so the docs leave them out.

## Reproduce the checks

```bash
docker compose up -d db && uv run pytest && uv run ruff check     # unit and integration tests
python3 formal/check.py && python3 formal/check.py --mutants      # the lifecycle model (needs Java 11+)
.claude/skills/verify-doc-vault/scripts/launch.sh                 # isolated stack; with a key in .env it asks for VERIFY_LIVE=1 first
API=http://localhost:18000 scripts/demo.sh                        # the demo path, with citations
.claude/skills/verify-doc-vault/scripts/cleanup.sh
```
