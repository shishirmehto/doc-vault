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
   - the web UI, in three passes.
4. **Parallel work without collisions.** The UI was built on its own branch in a separate git worktree, at the same time as the backend agent worked on `main`. The UI agents used the running API read-only and never restarted containers.
5. **Independent acceptance.** Before accepting a phase, the lead agent reran the full test suite. It also asked the live API its own questions and compared the answers with `samples/facts.yaml`, and it used the UI in a browser. Several findings below came from this step.
6. **Decision log.** Every decision went into a log with what was decided, why, the evidence and the result. The log holds more than 40 decisions and is kept outside the repo.

## Verification beyond unit tests

- **A formal model of the document lifecycle** (`formal/`, `docs/lifecycle.md`). A TLA+ model covers one document moving through indexing while workers crash, the provider fails, jobs arrive twice and the user presses retry. The TLC model checker tries every order of those events within small bounds, against seven rules, such as "a document never shows Failed while a worker is still processing it". The model found a real race, F1, before any test did. It also rejected the first fix idea with a counterexample in which an unparseable file loops forever. Each rule is also shown to fail on a deliberately broken copy of the model (`python3 formal/check.py --mutants`). A project skill, `.claude/skills/verify-lifecycle-model/`, tells the next agent when a code change needs the model rechecked. Its first use was the phase 3 change to reconcile.
- **Repro-first bug fixing.** The F1 fix started with a test against real Postgres advisory locks (`tests/test_lifecycle_race.py`), committed as an expected failure. It failed on the old code for all three ways a run can fail, then passed after the fix.
- **An app verification skill** (`.claude/skills/verify-doc-vault/`). It starts an isolated stack on its own ports and volumes, checks it, and drives each feature through the public API or the browser. It saves the evidence and removes only what it started. Its first run found two bugs in its own health-check script, and both were fixed before it was committed.
- **Evals** (`docs/evals.md`). Retrieval, citations, refusals and insights are scored separately, so a miss shows the step where it happened. The relevance threshold was fitted on a frozen case set, never on a single question.
- **UI runs** with Playwright, with screenshots and a log of every input event, so a stray click could not pass as a result.

## Where the AI was wrong, and what caught it

| What went wrong | What caught it |
|---|---|
| Every agent role first ran on the most expensive model | I set a model per role: cheaper models for exploring, the strongest for judgement |
| The UI design runners could read the judge's rubric | The runners' own reports. The rubric moved out of their folder, and the pick was checked against the judge's evidence, not the scores |
| The first F1 fix would loop forever on a file that never parses | The model checker's counterexample |
| The model check reported wrong state counts. It read a progress line, not the final count | Three runs on the same inputs gave three different numbers |
| The fake provider refuses a comparison that uses a word no chunk holds ("differ"), and missed an elliptical follow-up ("And the premium?") | The UI agent ran the demo questions word for word and would not reword them to pass. Both are now eval cases |
| An automated comment-removal pass deleted comments that explain non-obvious reasons | The owner agent reviewed the edits and reverted them |
| A UI agent saw the page change while it was not touching it | An idle watcher with input logging showed that no code path did it. The cause was a click in the visible test browser |
| The verification skill's health check printed FAIL after every OK, and one recipe expected progress on a finished document | Running the skill once, end to end, before committing it |

## AI inside the product

- Every prompt lives in `docvault/prompts.py`. Each is versioned and carries a note on why it is worded the way it is.
- **Grounding.** Every quote in an answer is located on its cited page, and the page's own text replaces the model's copy. Citations that cannot be found are dropped. Key facts in insights must quote their page, and those that don't are dropped and counted.
- **Relevance gate.** It runs before the answer call, so an off-topic question costs no answer tokens.
- **Follow-ups.** Each follow-up is rewritten into a standalone question, which is stored and returned as `interpreted_as`.
- **Ledger.** Every provider call is written to an append-only ledger with its tokens and cost. The metrics endpoints read it, so cost and latency are measured, not estimated.
- **Fake provider.** A deterministic fake makes the tests, the evals and a no-key run possible.

## What I decided

- The scope, and the decisions in the plan.
- The model-per-role sheet for the agents.
- Adding the web UI, the lifecycle chart as documentation, the verification skills, and the F1 fix.

## Limits

- The OpenAI path has been tested against recorded responses only. It needs one run with a real key, and the relevance threshold for OpenAI embeddings needs calibrating with the command in `docs/evals.md`.
- The eval numbers in `docs/evals.md` come from the fake provider. They measure the plumbing (retrieval, grounding, refusals), not answer quality.

## Reproduce the checks

```bash
docker compose up -d db && uv run pytest && uv run ruff check     # unit and integration tests
python3 formal/check.py && python3 formal/check.py --mutants      # the lifecycle model (needs Java 11+)
.claude/skills/verify-doc-vault/scripts/launch.sh                 # isolated stack for the end-to-end checks
API=http://localhost:18000 scripts/demo.sh                        # the demo path, with citations
.claude/skills/verify-doc-vault/scripts/cleanup.sh
```
