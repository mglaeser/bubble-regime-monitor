"""A durable count of provider attempts, written before each attempt is made.

## Why this exists

Panel run 31608202983 published `midterm-panel-count = pending`, refused, and
left no evidence file. `count-evidence.json` is written at the END of the count
path, so a refusal anywhere between the first provider call and that write
destroys the only record of how many calls were made. The operator asking "did
that run spend anything?" had nothing to read but the wall-clock gap between two
log lines.

The in-memory `MidtermProviderTransport.calls` list is not that record. It dies
with the process, and the process dying is exactly the case the record is for.

## The ordering that makes it evidence

The line is written and flushed BEFORE the byte reaches the socket. A call that
is recorded and then fails is over-counted by at most one; a call that is made
and then recorded is under-counted by exactly the case that matters — the one
that crashed the process. Over-counting is a conservative error against a spend
ceiling and under-counting is not, so the ordering is not a preference.

## What may be in it

Identity and size, never content. This file is uploaded as an artifact and read
by a job that does not hold the provider key, so anything in it is readable by
anyone who can read the run. The payload is recorded as a SHA-256 and a byte
length: enough to prove two runs sent the same thing and to bound what was
spent, and not enough to reconstruct a prompt, a diff or a response.
"""

from __future__ import annotations

import hashlib
import json
import os

#: One JSON object per line, appended. JSONL rather than a single JSON document
#: because a process killed mid-write leaves a truncated final line and every
#: complete line before it still parses — a partially written JSON array does
#: not parse at all, which would lose the whole ledger to the last entry.
JOURNAL_NAME = "provider-attempts.jsonl"

COUNT_OPERATION = "count"
GENERATION_OPERATION = "generation"

#: Capability label -> operation. The transport knows its capability; the
#: journal wants the operation, and mapping here keeps the two vocabularies
#: from leaking into each other.
OPERATION_FOR_CAPABILITY = {"COUNT_TRANSPORT": COUNT_OPERATION,
                            "GENERATION_TRANSPORT": GENERATION_OPERATION}

#: Fields a journal line may carry. Asserted by test against a real line, so a
#: future field carrying request or response text fails rather than ships.
PERMITTED_FIELDS = ("operation", "attempt", "endpoint", "payload_sha256",
                    "payload_bytes", "run_id", "run_attempt")


def journal_path(temp: str) -> str:
    return os.path.join(str(temp), "midterm", JOURNAL_NAME)


def record_attempt(path: str, *, operation: str, attempt: int, endpoint: str,
                   payload: bytes, run_id=None, run_attempt=None) -> dict:
    """Append one attempt and get it onto the disk before returning.

    `flush` then `fsync`: a buffered write is still in the process when the
    process is what fails, and this whole module exists for the run that does
    not get to finish."""
    entry = {"operation": str(operation),
             "attempt": int(attempt),
             "endpoint": str(endpoint),
             "payload_sha256": hashlib.sha256(bytes(payload)).hexdigest(),
             "payload_bytes": len(bytes(payload)),
             "run_id": str(run_id or ""),
             "run_attempt": str(run_attempt or "")}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def read_entries(path: str) -> list:
    """Every complete line. A truncated final line is dropped, not fatal.

    A journal that raises when read is a journal that reports nothing on the
    one path it was written for."""
    if not path or not os.path.exists(path):
        return []
    entries = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def counts(path: str) -> dict:
    """Exact attempt counts, or explicit zeroes with the journal's absence said.

    `journal_present` is reported rather than inferred from zero. "No journal"
    and "a journal recording no attempts" are different claims, and only the
    second one is evidence that nothing was spent."""
    present = bool(path) and os.path.exists(path)
    entries = read_entries(path)
    generation = sum(1 for e in entries
                     if e.get("operation") == GENERATION_OPERATION)
    count = sum(1 for e in entries if e.get("operation") == COUNT_OPERATION)
    return {"provider_attempts": len(entries),
            "count_attempts": count,
            "generation_attempts": generation,
            "journal_present": present}
