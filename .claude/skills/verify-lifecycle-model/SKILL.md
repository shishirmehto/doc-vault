---
name: verify-lifecycle-model
description: Model-check doc-vault's document indexing lifecycle (TLA+ with TLC) against seven rules, such as "Ready means every chunk is searchable" and "only one worker at a time". Use when a change touches docvault/documents.py (TRANSITIONS, sources_of, transition, _move, run_index, _index, _parse, _embed_pending_chunks, MAX_RETRIES, retry, reconcile), docvault/db.py pipeline_steps, or the delivery settings and index_document in docvault/tasks.py.
---

# Verify the document lifecycle model

`formal/DocLifecycle.tla` models one document moving through indexing while workers crash, the AI provider fails, Celery redelivers or retries jobs, the minute check re-queues stale documents and the user presses retry. TLC explores every order of those events within the bounds in `formal/DocLifecycle.cfg` and checks seven rules. Human-facing explanation and the chart: `docs/lifecycle.md`.

## The rules

| Rule | Plain sentence | Harm if it breaks |
|---|---|---|
| `ReadyMeansSearchable` | Ready means chat can search every chunk. | Chat answers "not found" for text the document contains. |
| `ParsedOnce` | Pages and chunks are written once. | A rerun hits the pages primary key and fails a good document, or doubles chunks. |
| `OneWorkerAtATime` | One worker per document at a time. | Two workers pay for the same embeddings and race on status. |
| `NotFailedWhileWorking` | Never Failed while a worker is processing. | The user sees Failed for a document that is being indexed. |
| `ReadyStaysReady` | A ready document stays ready. | A late failure hides a working document from chat. |
| `FailedWaitsForYou` | Failed changes only on retry. | A document flips state with no user action. |
| `AlwaysSettles` | Every in-flight document ends Ready or Failed. | A document spins forever. |

## Does my change touch it?

Run `git diff <sync commit> -- docvault/documents.py docvault/db.py docvault/tasks.py docvault/schema.sql` (the sync commit is below). If a covered function in the table changed, this skill applies.

| Model action | Code |
|---|---|
| `Take`, `Crash` (redelivery) | `tasks.py` Celery config (`task_acks_late`, `task_reject_on_worker_lost`) and `index_document`, which only turns a raised `VaultError` into `self.retry` |
| `GetLease`, `LeaseBusy` | `db.pipeline_steps` (`pg_try_advisory_lock`, `pg_advisory_unlock_all`) |
| `NothingToDo`, `StartParse`, `ResumeEmbed` | `documents._index` (the row read, then `transition` to PARSING or EMBEDDING) |
| `ParseSaved` | `documents._parse` (one transaction: pages, chunks, parse facts, `transition` to EMBEDDING, result ignored) |
| `BadFile`, `ProviderFault` | the `except` branches in `documents.run_index`: re-raise a retryable `VaultError` while `attempt <= MAX_RETRIES` (Celery retries), otherwise go to the FAILED write with the lease still held (`StartFailing`) |
| `EmbedBatch` | `documents._embed_pending_chunks` (one transaction per batch) |
| `EmbedDone` | `transition(conn, doc_id, READY)` at the end of `_index` |
| `MarkFailed` | `_move(steps.conn, doc_id, FAILED, sources_of(FAILED), failure)` in `run_index`, inside `pipeline_steps` |
| `Unlock` | the `finally` of `db.pipeline_steps` (`pg_advisory_unlock_all`), a separate step after the FAILED write |
| `Reconcile` | `documents.reconcile` and the beat schedule in `tasks.py` |
| `UserRetry` | `documents.retry` (`transition` FAILED to QUEUED, then `jobs.index`) |
| Status sets in guards | `TRANSITIONS` and `sources_of` in `documents.py` |

Each model step is one atomic step in the code: one SQL statement, one transaction, or one lock call. `StartParse` and `ResumeEmbed` fold the row read into the compare-and-swap. That is sound because `parsed` changes only under the lease and the compare-and-swap re-checks `status`.

**Sync commit:** `f21d4de`. The model was last confirmed against the code at this commit. Move it forward only after you have read every changed covered function and confirmed the model still describes it.

## Run

```bash
python3 formal/check.py             # all rules; PASS or FAIL with TLC's trace, about 2 s
python3 formal/check.py --mutants   # each rule must fail on a code-shaped mutant and hold on the model, about 15 s
```

Needs Java 11+ on PATH (or `JAVA=/path/to/java`). The script downloads `tla2tools.jar` 1.7.4 once into `~/.cache/doc-vault` and refuses it if the SHA-256 differs.

Read every FAIL with the `/statechart` skill on `formal/DocLifecycle.tla` and `formal/DocLifecycle.cfg`, with the view in `formal/view.ts`. It lists each broken rule's shortest path in plain words. With the doc config (`formal/DocLifecycle-doc.cfg`) it redraws the chart in `docs/lifecycle.md`.

## Known findings

- **F1, fixed in `3437628`.** `index_document` used to call `documents.fail()` after `pipeline_steps` had released the lease, so a second worker could be mid-parse when the first marked the document Failed. `run_index` now writes FAILED on the lease connection before the lease is released. `tests/test_lifecycle_race.py` is the regression test, and the `NotFailedWhileWorking` mutant in `formal/check.py` is the same bug in the model. Do not "simplify" the FAILED write onto a pool connection or after the `with pipeline_steps` block: both bring F1 back.
- **Rejected design.** A `fail()` that takes the lease and skips when it is held breaks `AlwaysSettles`, because a file that never parses loops forever. `docs/lifecycle.md` explains it.

`check.py` prints PASS and exits 0 on the current model. Any FAIL is new.

## A real bug or a modelling error?

Replay the trace against the code, one step per action, using the table above. If each step is something the code allows, it is a real bug: report it with the steps in the code's words and add it to Known findings. Do not weaken a rule or the model to make it pass. If a step is something the code cannot do, the model is wrong: fix the action so it matches the code, and say which line proves it.

## Intended changes

When you change covered code on purpose, change the matching action in the same commit, rerun both commands, and update the mutant text in `formal/check.py` if `--mutants` reports STALE. Then move the sync commit.

## Bounds and what is left out

2 workers, 2 chunks, 1 crash, 2 provider faults, 1 Celery retry, 1 user retry, at most 2 queued jobs. Time is not modelled: the two-minute stale threshold is "may happen at any moment", which only adds orders. Insights (`run_analyze`), deletes and re-embedding after an embedding-model change are not modelled. A PASS covers these bounds, not every run.
