#!/usr/bin/env python3
"""Model-check doc-vault's document lifecycle with TLC.

    python3 formal/check.py            check every rule in DocLifecycle.cfg
    python3 formal/check.py --mutants  prove each rule can fail on a broken variant

Needs Java 11 or later on PATH (or JAVA=/path/to/java). Downloads the pinned
tla2tools.jar once into ~/.cache/doc-vault and verifies its checksum.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "DocLifecycle.tla"
CONFIG = HERE / "DocLifecycle.cfg"
TLC_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
TLC_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "doc-vault"

# Each mutant mirrors one code bug. The rule it names must fail on the mutant
# and hold on the real model.
MUTANTS = [
    ("ReadyMeansSearchable", "run_index moves to READY after the first embed batch",
     '/\\ pc[w] = "embedding" /\\ embedded = Chunks', '/\\ pc[w] = "embedding" /\\ embedded >= 1', []),
    ("ParsedOnce", "run_index ignores page_count and parses on every run",
     '/\\ pc[w] = "checking" /\\ ~parsed /\\ status', '/\\ pc[w] = "checking" /\\ status', []),
    ("OneWorkerAtATime", "pipeline_steps ignores the pg_try_advisory_lock result",
     '/\\ pc[w] = "locking" /\\ lock = "free"', '/\\ pc[w] = "locking"', []),
    ("ReadyStaysReady", "retry() accepts a ready document and sends it back to the queue",
     '/\\ status = "failed" /\\ userRetries < MaxUserRetries', '/\\ status \\in {"failed", "ready"} /\\ userRetries < MaxUserRetries', []),
    ("NotFailedWhileWorking", "the lease is released before fail() writes FAILED (F1)",
     'StartFailing(w) == pc\' = [pc EXCEPT ![w] = "failing"] /\\ UNCHANGED lock', 'StartFailing(w) == Release(w, "failing")', []),
    ("FailedWaitsForYou", "TRANSITIONS lets FAILED move to PARSING without a retry",
     '~parsed /\\ status \\in {"queued", "parsing"}\n  /\\ status\' = "parsing"', '~parsed /\\ status \\in {"queued", "parsing", "failed"}\n  /\\ status\' = "parsing"', []),
    ("AlwaysSettles", "task_acks_late is off and beat no longer runs reconcile",
     '/\\ Enq(tries[w])\n', '/\\ UNCHANGED queue\n', ["-deadlock"]),
]
MUTANT_EXTRA = {"AlwaysSettles": ('/\\ status \\in InFlight /\\ QueueSize < QCap', '/\\ FALSE /\\ status \\in InFlight')}


def tlc_jar() -> Path:
    jar = CACHE / "tla2tools-1.7.4.jar"
    if not jar.exists() or hashlib.sha256(jar.read_bytes()).hexdigest() != TLC_SHA256:
        CACHE.mkdir(parents=True, exist_ok=True)
        print(f"downloading {TLC_URL}", file=sys.stderr)
        with urllib.request.urlopen(TLC_URL) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != TLC_SHA256:
            sys.exit("tla2tools.jar checksum mismatch; refusing to run it")
        jar.write_bytes(data)
    return jar


def java() -> str:
    found = os.environ.get("JAVA") or shutil.which("java")
    if not found:
        sys.exit("Java 11 or later is required (set JAVA=/path/to/java)")
    return found


def run_tlc(spec_dir: Path, config: Path, extra: list[str]) -> str:
    with tempfile.TemporaryDirectory() as meta:
        result = subprocess.run(
            [java(), "-XX:+UseParallelGC", "-cp", str(tlc_jar()), "tlc2.TLC", "-workers", "1",
             "-metadir", meta, "-config", str(config), *extra, "DocLifecycle.tla"],
            cwd=spec_dir, capture_output=True, text=True)
    return result.stdout + result.stderr


def violated(output: str) -> str | None:
    match = re.search(r"Error: (Invariant|Action property) (\w+) is violated", output)
    if match:
        return match.group(2)
    if "Temporal properties were violated" in output:
        return "AlwaysSettles"
    if "Deadlock reached" in output:
        return "NoDeadlock"
    return None


def single_rule_config(rule: str) -> str:
    base = CONFIG.read_text().split("INVARIANTS")[0]
    kind = "INVARIANTS" if rule in {"ReadyMeansSearchable", "ParsedOnce", "OneWorkerAtATime",
                                     "NotFailedWhileWorking"} else "PROPERTIES"
    return f"{base}{kind}\n  {rule}\n"


def check() -> int:
    output = run_tlc(HERE, CONFIG, [])
    states = re.findall(r"(\d[\d,]*) distinct states found", output)
    rule = violated(output)
    if rule is None and "Model checking completed. No error has been found." in output:
        print(f"PASS: every rule holds ({states[-1] if states else '?'} distinct states)")
        return 0
    print(output[output.find("Error:"):] if "Error:" in output else output)
    print(f"FAIL: {rule or 'TLC error'} (read the trace with /statechart)")
    return 1


def mutants() -> int:
    failures = 0
    for rule, bug, old, new, extra in MUTANTS:
        with tempfile.TemporaryDirectory() as scratch:
            scratch_dir = Path(scratch)
            cfg = scratch_dir / "rule.cfg"
            cfg.write_text(single_rule_config(rule))
            text = SPEC.read_text()
            (scratch_dir / "DocLifecycle.tla").write_text(text)
            holds = violated(run_tlc(scratch_dir, cfg, extra)) is None
            if old not in text:
                print(f"STALE  {rule}: the model no longer contains the text this mutant edits")
                failures += 1
                continue
            mutated = text.replace(old, new, 1)
            if rule in MUTANT_EXTRA:
                mutated = mutated.replace(*MUTANT_EXTRA[rule], 1)
            (scratch_dir / "DocLifecycle.tla").write_text(mutated)
            caught = violated(run_tlc(scratch_dir, cfg, extra)) == rule
        ok = holds and caught
        failures += not ok
        print(f"{'OK    ' if ok else 'BROKEN'} {rule}: real model {'holds' if holds else 'FAILS'}, "
              f"mutant {'caught' if caught else 'NOT caught'} ({bug})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(mutants() if "--mutants" in sys.argv else check())
