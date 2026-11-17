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
4. **Parallel work without collisions.** The UI was built on its own branch in a separate git worktree, at the same time as the backend agent worked on `main`. The UI agents used the running API read-only and never restarted containers.
5. **Independent acceptance.** Before accepting a phase, the lead agent reran the full test suite. It also asked the live API its own questions and compared the answers with `samples/facts.yaml`, and it used the UI in a browser. Several findings below came from this step.

   For the bonus phases, the lead agent drove each feature itself on the running stack before accepting it:
   - it uploaded a file with `curl` and watched it reach Ready in an open browser with no reload;
   - it compared the policy with its renewal quote and opened a cell's page;
   - it rewrote a summary twice and counted the ledger rows;
   - it uploaded a second version under the same name;
   - it asked one first question in two chats and checked that the second made no model call.

## Verification beyond unit tests

- **Evals** (`docs/evals.md`). Retrieval, citations, refusals and insights are scored separately, so a miss shows the step where it happened. The relevance threshold was fitted on a frozen case set, never on a single question.
- **Real documents.** Before the live run below, 19 public insurance documents ran through an isolated stack on the fake provider:
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
- **A formal model of the document lifecycle** (`formal/`, `docs/lifecycle.md`). A TLA+ model covers one document moving through indexing while workers crash, the provider fails, jobs arrive twice and the user presses retry. The TLC model checker tries every order of those events within small bounds, against seven rules, such as "a document never shows Failed while a worker is still processing it". The model found a real race, F1, before any test did. It also rejected the first fix idea with a counterexample in which an unparseable file loops forever. Each rule is also shown to fail on a deliberately broken copy of the model (`python3 formal/check.py --mutants`). A project skill, `.claude/skills/verify-lifecycle-model/`, tells the next agent when a code change needs the model rechecked. Its first use was the phase 3 change to reconcile.
- **Repro-first bug fixing.** The F1 fix started with a test against real Postgres advisory locks (`tests/test_lifecycle_race.py`), committed as an expected failure. It failed on the old code for all three ways a run can fail, then passed after the fix.
- **An app verification skill** (`.claude/skills/verify-doc-vault/`). It starts an isolated stack on its own ports and volumes, checks it, and drives each feature through the public API or the browser. It saves the evidence and removes only what it started. Its first run found two bugs in its own health-check script, and both were fixed before it was committed.
- **UI runs** with Playwright, with screenshots and a log of every input event, so a stray click could not pass as a result.
- **A visibility check on every screen** (`.claude/skills/verify-doc-vault/scripts/visible.js`). It lists clipped text, controls that no scroll can reach, and sideways page scroll. The final UI pass ran it on 53 screens at four window sizes, and it returned nothing on any of them.

## The live run

One live run with the OpenAI key, on 2026-11-12, cost $3.17 on the ledger, $1.18 of it for the eval set. It covered:
- **The eval set:** the numbers are in `docs/evals.md`.
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

What the run left open:
- Three limits that the README lists: "Differs" compares text and not meaning, a chat sends eight passages to the answer step, and insights read the first 240,000 characters.
- A word broken at a line end, with a stray space inside it, stays split ("se t-tlements") when the document never uses it whole. That happens in the oldest standard forms.
- The eval's case set was written for the sample documents. The 31 real-document questions are a hand-graded spot check, not a scored set.

## Where the AI was wrong, and what caught it

| What went wrong | What caught it |
|---|---|
| The first F1 fix would loop forever on a file that never parses | The model checker's counterexample |
| While changing comparisons, a fixer agent removed the repair turn for insights | The phase owner's review of the fixer's diff. A table test now pins every AI purpose's retries, repair and timeout |
| Streamed answers arrived all at once at the end | Measuring the gap between pieces through the web server: its rewrite compressed the stream. The API now marks the stream as not to be transformed |
| The fix for doubled bold text would also have halved template placeholders, so "YYYY" read "YY" | The lead agent ran the new extraction on the real documents before accepting it. The flood manual has seven "YYYY" date fields |
| The calibration's best-fit gate threshold (0.518) would have refused 12 of 28 answerable questions | The lead agent tabulated how many answerable and out-of-scope turns each threshold passes, and set 0.20, below the lowest answerable turn |
| After a delete, a capped document list shrank and stayed short | An independent review agent. The list now reads again after a delete |
| The fake provider refuses a comparison that uses a word no chunk holds ("differ"), and missed an elliptical follow-up ("And the premium?") | The UI agent ran the demo questions word for word and would not reword them to pass. Both are now eval cases |
| The first citation check on real documents flagged correct quotes as off the page | Reading the pages: the reference reader's layout mode had merged two columns. The check switched to reading order and word overlap |
| The question box disappeared in my own browser on a chat with three turns, and the lead agent could not reproduce it | Anything placed above the app, such as a browser extension's bar or a margin on the page, pushed the box off screen. The UI audit pass reproduced this by injecting such elements and rebuilt the layout so the content scrolls above a fixed footer row |
| The spending-cap docs said search keeps working over the cap, but chat is the only search, and it is capped | The second owner of that phase. The docs now say uploads are still indexed |
| Every agent role first ran on the most expensive model | I set a model per role: cheaper models for exploring, the strongest for judgement |
| The UI design runners could read the judge's rubric | The runners' own reports. The rubric moved out of their folder, and the pick was checked against the judge's evidence, not the scores |
| An automated comment-removal pass deleted comments that explain non-obvious reasons | The owner agent reviewed the edits and reverted them |

## AI inside the product

- Every prompt lives in `docvault/prompts.py`. Each is versioned and carries a note on why it is worded the way it is.
- **Grounding.** Every quote in an answer is located on its cited page, and the page's own text replaces the model's copy. Citations that cannot be found are dropped. Key facts in insights must quote their page, and those that don't are dropped and counted.
- **Relevance gate.** It runs before the answer call, so an off-topic question costs no answer tokens.
- **Follow-ups.** Each follow-up is rewritten into a standalone question, which is stored and returned as `interpreted_as`.
- **Ledger.** Every provider call is written to an append-only ledger with its tokens and cost. The metrics endpoints read it, so cost and latency are measured, not estimated.
- **Fake provider.** A deterministic fake makes the tests, the evals and a no-key run possible.
- **Comparisons.** Every comparison cell must quote its page, or it is dropped. Whether two values differ is computed in code, not by the model.

## What I decided

- The scope, and the decisions in the plan.
- The model-per-role sheet for the agents.
- Adding the web UI, the lifecycle chart as documentation, the verification skills, and the F1 fix.
- Adding all the bonus features, and the rule that everything in the UI must be visible: nothing clipped, hidden, or reachable only by URL.
- No paid model calls until a key was provided. Everything ran on the fake provider until then.
- Testing on real insurance documents as well as the generated samples. That run found the three parsing bugs above.
- Reverting a late change that bound the compose ports to localhost only, because the case study does not ask for it.
- Adding Up and Down recall of earlier questions, and answers that show in every open tab of a chat, after testing the UI in two tabs.

## Reproduce the checks

```bash
docker compose up -d db && uv run pytest && uv run ruff check     # unit and integration tests
python3 formal/check.py && python3 formal/check.py --mutants      # the lifecycle model (needs Java 11+)
.claude/skills/verify-doc-vault/scripts/launch.sh                 # isolated stack for the end-to-end checks
API=http://localhost:18000 scripts/demo.sh                        # the demo path, with citations
.claude/skills/verify-doc-vault/scripts/cleanup.sh
```
