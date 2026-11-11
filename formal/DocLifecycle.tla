---------------------------- MODULE DocLifecycle ----------------------------
(* One uploaded document moving through doc-vault's indexing pipeline.
   Mirrors docvault/documents.py (run_index, _index, retry, reconcile),
   docvault/db.py (pipeline_steps lease) and docvault/tasks.py (index_document)
   at commit 3437628. Insights and deletes are left out. *)
EXTENDS Naturals, FiniteSets

CONSTANTS Workers, Chunks, MaxRetries, MaxCrashes, MaxFaults, QCap, MaxUserRetries

InFlight == {"queued", "parsing", "embedding"}
Holding  == {"checking", "parsing", "embedding", "failing", "unlocking"}

VARIABLES status, parsed, embedded, parses, queue, pc, tries, lock,
          crashes, faults, userRetries
vars == <<status, parsed, embedded, parses, queue, pc, tries, lock,
          crashes, faults, userRetries>>

RECURSIVE SumTo(_)
SumTo(n) == IF n = 0 THEN queue[0] ELSE queue[n] + SumTo(n - 1)
QueueSize == SumTo(MaxRetries)
Enq(r) == queue' = [queue EXCEPT ![r] = @ + 1]

Init ==
  /\ status = "queued" /\ parsed = FALSE /\ embedded = 0 /\ parses = 0
  /\ queue = [r \in 0..MaxRetries |-> IF r = 0 THEN 1 ELSE 0]
  /\ pc = [w \in Workers |-> "idle"] /\ tries = [w \in Workers |-> 0]
  /\ lock = "free" /\ crashes = 0 /\ faults = 0 /\ userRetries = 0

\* A worker takes an index_document message off the queue.
Take(w) ==
  /\ pc[w] = "idle"
  /\ \E r \in 0..MaxRetries :
       /\ queue[r] > 0
       /\ queue' = [queue EXCEPT ![r] = @ - 1]
       /\ tries' = [tries EXCEPT ![w] = r]
  /\ pc' = [pc EXCEPT ![w] = "locking"]
  /\ UNCHANGED <<status, parsed, embedded, parses, lock, crashes, faults, userRetries>>

\* pg_try_advisory_lock succeeds.
GetLease(w) ==
  /\ pc[w] = "locking" /\ lock = "free"
  /\ lock' = w /\ pc' = [pc EXCEPT ![w] = "checking"]
  /\ UNCHANGED <<status, parsed, embedded, parses, queue, tries, crashes, faults, userRetries>>

\* pg_try_advisory_lock fails: a duplicate delivery returns at once.
LeaseBusy(w) ==
  /\ pc[w] = "locking" /\ lock /= "free"
  /\ pc' = [pc EXCEPT ![w] = "idle"]
  /\ UNCHANGED <<status, parsed, embedded, parses, queue, tries, lock, crashes, faults, userRetries>>

\* pg_advisory_unlock_all releases only this worker's own locks.
Release(w, next) ==
  /\ lock' = IF lock = w THEN "free" ELSE lock
  /\ pc' = [pc EXCEPT ![w] = next]

\* A failure keeps the lease until fail() has written FAILED.
StartFailing(w) == pc' = [pc EXCEPT ![w] = "failing"] /\ UNCHANGED lock

\* run_index reads the row under the lease and decides what is left to do.
NothingToDo(w) ==
  /\ pc[w] = "checking"
  /\ \/ status = "failed"
     \/ status = "ready" /\ embedded = Chunks
     \/ ~parsed /\ status \notin {"queued", "parsing"}
     \/ parsed /\ status \notin {"queued", "parsing", "ready", "embedding"}
  /\ Release(w, "idle")
  /\ UNCHANGED <<status, parsed, embedded, parses, queue, tries, crashes, faults, userRetries>>

StartParse(w) ==
  /\ pc[w] = "checking" /\ ~parsed /\ status \in {"queued", "parsing"}
  /\ status' = "parsing" /\ pc' = [pc EXCEPT ![w] = "parsing"]
  /\ UNCHANGED <<parsed, embedded, parses, queue, tries, lock, crashes, faults, userRetries>>

ResumeEmbed(w) ==
  /\ pc[w] = "checking" /\ parsed /\ status \in {"queued", "parsing", "ready", "embedding"}
  /\ ~(status = "ready" /\ embedded = Chunks)
  /\ status' = "embedding" /\ pc' = [pc EXCEPT ![w] = "embedding"]
  /\ UNCHANGED <<parsed, embedded, parses, queue, tries, lock, crashes, faults, userRetries>>

\* Checkpoint 1: pages, chunks and the move to EMBEDDING commit together.
\* The compare-and-swap result is ignored, so pages commit even if the status moved.
ParseSaved(w) ==
  /\ pc[w] = "parsing"
  /\ parsed' = TRUE /\ parses' = parses + 1
  /\ status' = IF status \in {"queued", "parsing", "ready", "embedding"} THEN "embedding" ELSE status
  /\ pc' = [pc EXCEPT ![w] = "embedding"]
  /\ UNCHANGED <<embedded, queue, tries, lock, crashes, faults, userRetries>>

\* No text layer: a non-retryable VaultError, caught inside the lease.
BadFile(w) ==
  /\ pc[w] = "parsing"
  /\ StartFailing(w)
  /\ UNCHANGED <<status, parsed, embedded, parses, queue, tries, crashes, faults, userRetries>>

\* A provider timeout: Celery retries with backoff; the last attempt fails under the lease.
ProviderFault(w) ==
  /\ pc[w] \in {"parsing", "embedding"} /\ faults < MaxFaults
  /\ faults' = faults + 1
  /\ IF tries[w] < MaxRetries
       THEN Release(w, "idle") /\ Enq(tries[w] + 1)
       ELSE StartFailing(w) /\ UNCHANGED queue
  /\ UNCHANGED <<status, parsed, embedded, parses, tries, crashes, userRetries>>

EmbedBatch(w) ==
  /\ pc[w] = "embedding" /\ embedded < Chunks
  /\ embedded' = embedded + 1
  /\ UNCHANGED <<status, parsed, parses, queue, pc, tries, lock, crashes, faults, userRetries>>

EmbedDone(w) ==
  /\ pc[w] = "embedding" /\ embedded = Chunks
  /\ status' = IF status \in {"embedding", "ready"} THEN "ready" ELSE status
  /\ Release(w, "idle")
  /\ UNCHANGED <<parsed, embedded, parses, queue, tries, crashes, faults, userRetries>>

\* run_index writes FAILED on the lease connection; the lease is still held.
MarkFailed(w) ==
  /\ pc[w] = "failing"
  /\ status' = IF status \in InFlight THEN "failed" ELSE status
  /\ pc' = [pc EXCEPT ![w] = "unlocking"]
  /\ UNCHANGED <<parsed, embedded, parses, queue, tries, lock, crashes, faults, userRetries>>

\* pipeline_steps exits and unlocks, a separate step: a retry can land in between.
Unlock(w) ==
  /\ pc[w] = "unlocking"
  /\ Release(w, "idle")
  /\ UNCHANGED <<status, parsed, embedded, parses, queue, tries, crashes, faults, userRetries>>

\* SIGKILL or OOM: Postgres drops the lease, Celery redelivers the unacked message.
Crash(w) ==
  /\ pc[w] \in Holding /\ crashes < MaxCrashes
  /\ crashes' = crashes + 1
  /\ lock' = IF lock = w THEN "free" ELSE lock
  /\ pc' = [pc EXCEPT ![w] = "idle"]
  /\ Enq(tries[w])
  /\ UNCHANGED <<status, parsed, embedded, parses, tries, faults, userRetries>>

\* Beat every 60 s: an in-flight document untouched for 2 minutes is enqueued again.
Reconcile ==
  /\ status \in InFlight /\ QueueSize < QCap
  /\ Enq(0)
  /\ UNCHANGED <<status, parsed, embedded, parses, pc, tries, lock, crashes, faults, userRetries>>

\* POST /documents/{id}/retry
UserRetry ==
  /\ status = "failed" /\ userRetries < MaxUserRetries
  /\ status' = "queued" /\ userRetries' = userRetries + 1
  /\ Enq(0)
  /\ UNCHANGED <<parsed, embedded, parses, pc, tries, lock, crashes, faults>>

\* Doc chart only: at most two things go wrong in one run.
Budget == crashes + faults <= 2
\* Doc chart only: the minute check re-queues only when no worker is busy.
QuietReconcile == (QueueSize' > QueueSize /\ pc' = pc /\ status' = status) => \A w \in Workers : pc[w] = "idle"

Settled ==
  /\ status \in {"ready", "failed"} /\ QueueSize = 0
  /\ \A w \in Workers : pc[w] = "idle"
  /\ UNCHANGED vars

Next ==
  \/ \E w \in Workers :
       \/ Take(w) \/ GetLease(w) \/ LeaseBusy(w) \/ NothingToDo(w)
       \/ StartParse(w) \/ ResumeEmbed(w) \/ ParseSaved(w) \/ BadFile(w)
       \/ ProviderFault(w) \/ EmbedBatch(w) \/ EmbedDone(w) \/ MarkFailed(w) \/ Unlock(w) \/ Crash(w)
  \/ Reconcile \/ UserRetry \/ Settled

Progress(w) == Take(w) \/ GetLease(w) \/ LeaseBusy(w) \/ NothingToDo(w) \/ StartParse(w)
               \/ ResumeEmbed(w) \/ ParseSaved(w) \/ EmbedBatch(w) \/ EmbedDone(w) \/ MarkFailed(w) \/ Unlock(w)

Spec == Init /\ [][Next]_vars /\ \A w \in Workers : WF_vars(Progress(w)) /\ WF_vars(Reconcile)

-----------------------------------------------------------------------------
\* The rules a reader agrees to.

\* "Ready" means chat can search every chunk.
ReadyMeansSearchable == status = "ready" => parsed /\ embedded = Chunks

\* Pages and chunks are written once; a rerun never parses again.
ParsedOnce == parses <= 1

\* Only one worker ever works on a document at a time.
OneWorkerAtATime == \A w \in Workers : pc[w] \in Holding => lock = w

\* A document never shows Failed while a worker is still processing it.
NotFailedWhileWorking == status = "failed" => \A w \in Workers : pc[w] \notin {"parsing", "embedding"}

\* A ready document stays ready.
ReadyStaysReady == [][status = "ready" => status' = "ready"]_vars

\* Failed changes only when you press retry.
FailedWaitsForYou == [][status = "failed" => status' \in {"failed", "queued"}]_vars

\* Every document in flight ends ready or failed, whatever crashes or duplicates happen.
AlwaysSettles == (status \in InFlight) ~> (status \in {"ready", "failed"})
=============================================================================
