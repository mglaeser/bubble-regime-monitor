"""A durable count of provider attempts, written before each attempt is made.

## Why this exists

Panel run 31608202983 published `midterm-panel-count = pending`, refused, and
left no evidence file. `count-evidence.json` is written at the END of the count
path, so a refusal anywhere between the first provider call and that write
destroys the only record of how many calls were made. The operator asking "did
that run spend anything?" had nothing to read but the wall-clock gap between two
log lines.

## Accounting is a PRECONDITION, not a side effect

The first version of this module swallowed a failed journal write and let the
call proceed. That is a fail-open in the evidence: the call happened, the ledger
said zero, and nothing in the summary said the ledger was untrustworthy. A
number that is silently wrong is worse than no number, because the reader has no
signal to distrust it.

So a provider attempt may not proceed unless its record was durably written
first. `initialize` runs before a live transport is returned and refuses if the
journal cannot be created; `record_attempt` raises rather than returning, and
the transport turns that into a typed refusal without opening a socket.

## Unavailable is not zero

Four states, kept distinct all the way into the job summary:

    ATTEMPTS_KNOWN_ZERO             an initialized journal with no entries
    ATTEMPTS_KNOWN_NONZERO          a valid journal with entries
    ATTEMPT_ACCOUNTING_UNAVAILABLE  no journal, or a truncated final line
    ATTEMPT_JOURNAL_INVALID         a malformed or self-contradicting journal

A missing journal is NOT reported as zero. Absence proves nothing about whether
a transport was ever constructed, and reporting it as zero would be the same
fail-open in a different place.

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
import re

#: One JSON object per line, appended. JSONL rather than a single JSON document
#: because a process killed mid-write leaves a truncated final line and every
#: complete line before it still parses — a partially written JSON array does
#: not parse at all, which would lose the whole ledger to the last entry.
JOURNAL_NAME = "provider-attempts.jsonl"

COUNT_OPERATION = "count"
GENERATION_OPERATION = "generation"

#: Capability label -> operation.
OPERATION_FOR_CAPABILITY = {"COUNT_TRANSPORT": COUNT_OPERATION,
                            "GENERATION_TRANSPORT": GENERATION_OPERATION}

#: The endpoint each operation is allowed to name. A count line claiming the
#: generation endpoint is not a typo to tolerate: it is either a corrupted
#: ledger or a transport that reached the endpoint that costs money.
ENDPOINT_FOR_OPERATION = {COUNT_OPERATION: "/v1/responses/input_tokens",
                          GENERATION_OPERATION: "/v1/responses"}

#: Exactly these keys. Not "at least": an extra key is an unreviewed field in a
#: file that must never carry request or response text.
PERMITTED_FIELDS = ("operation", "attempt", "endpoint", "payload_sha256",
                    "payload_bytes", "run_id", "run_attempt")

ATTEMPTS_KNOWN_ZERO = "ATTEMPTS_KNOWN_ZERO"
ATTEMPTS_KNOWN_NONZERO = "ATTEMPTS_KNOWN_NONZERO"
ATTEMPT_ACCOUNTING_UNAVAILABLE = "ATTEMPT_ACCOUNTING_UNAVAILABLE"
ATTEMPT_JOURNAL_INVALID = "ATTEMPT_JOURNAL_INVALID"

#: States in which the numbers may be believed.
KNOWN_STATES = (ATTEMPTS_KNOWN_ZERO, ATTEMPTS_KNOWN_NONZERO)

#: What `attemptscli` prints and `finalize` reports when a count is not known.
UNKNOWN = "UNKNOWN"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def journal_path(temp: str) -> str:
    return os.path.join(str(temp), "midterm", JOURNAL_NAME)


def initialize(path: str, *, run_id=None, run_attempt=None) -> str:
    """Prove the ledger is writable BEFORE a transport that can spend exists.

    Creates the parent directory and the file, then flushes and fsyncs it, so
    that "the disk is full" or "the directory is not writable" is discovered
    here — where nothing has been spent — rather than at the first attempt,
    where the old code swallowed it and called the provider anyway.

    Raises `OSError`. The caller converts that into a typed refusal; this
    module deliberately does not know about refusals."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    return path


def record_attempt(path: str, *, operation: str, attempt: int, endpoint: str,
                   payload: bytes, run_id=None, run_attempt=None) -> dict:
    """Append one attempt and get it onto the disk before returning.

    `flush` then `fsync`: a buffered write is still in the process when the
    process is what fails, and this whole module exists for the run that does
    not get to finish.

    Raises `OSError` on any failure. It must NOT return quietly — the caller's
    next action is a provider call, and a call whose record did not reach the
    disk is a call the run cannot afterwards account for."""
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


def _entry_is_well_formed(entry) -> bool:
    if not isinstance(entry, dict) or set(entry) != set(PERMITTED_FIELDS):
        return False
    operation = entry.get("operation")
    if operation not in ENDPOINT_FOR_OPERATION:
        return False
    if entry.get("endpoint") != ENDPOINT_FOR_OPERATION[operation]:
        return False
    attempt = entry.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return False
    digest = entry.get("payload_sha256")
    if not isinstance(digest, str) or not _DIGEST.match(digest):
        return False
    payload_bytes = entry.get("payload_bytes")
    if (isinstance(payload_bytes, bool)
            or not isinstance(payload_bytes, int) or payload_bytes < 0):
        return False
    return all(isinstance(entry.get(field), str)
               for field in ("run_id", "run_attempt"))


def read_journal(path: str) -> dict:
    """Parse and STRICTLY validate. Never silently drops a line.

    A malformed line anywhere but the end invalidates the journal: it means
    something wrote a record this module did not write, and a ledger with an
    unexplained record is not evidence. A truncated FINAL line is different —
    it is the expected shape of a process killed mid-write — and is reported as
    incomplete rather than invalid, but never as complete."""
    if not path or not os.path.exists(path):
        return {"journal_present": False, "accounting_complete": False,
                "accounting_state": ATTEMPT_ACCOUNTING_UNAVAILABLE,
                "entries": [],
                "reason": "no journal file; absence does not prove no transport "
                          "was constructed"}
    with open(path, encoding="utf-8") as handle:
        raw = [line for line in handle.read().split("\n")]
    # A file ending in a newline yields a trailing empty element that is not a
    # truncated record.
    trailing_newline = bool(raw) and raw[-1] == ""
    if trailing_newline:
        raw = raw[:-1]

    entries, complete = [], True
    for index, line in enumerate(raw):
        is_final = index == len(raw) - 1
        if not line.strip():
            if is_final and not trailing_newline:
                complete = False
                continue
            return _invalid(path, "a blank record")
        try:
            entry = json.loads(line)
        except ValueError:
            if is_final and not trailing_newline:
                # Truncated by a dying process: recognised, and NOT dropped.
                complete = False
                continue
            return _invalid(path, "a malformed record before the end of the "
                                  "journal")
        if not _entry_is_well_formed(entry):
            return _invalid(path, "a record whose shape this module never "
                                  "writes")
        entries.append(entry)

    ordinals = [e["attempt"] for e in entries]
    if len(set(ordinals)) != len(ordinals):
        return _invalid(path, "a duplicated attempt ordinal")
    if ordinals != list(range(1, len(ordinals) + 1)):
        return _invalid(path, "attempt ordinals that are not dense from 1")
    identities = {(e["run_id"], e["run_attempt"]) for e in entries}
    if len(identities) > 1:
        return _invalid(path, "records from more than one run")

    if not complete:
        return {"journal_present": True, "accounting_complete": False,
                "accounting_state": ATTEMPT_ACCOUNTING_UNAVAILABLE,
                "entries": entries,
                "reason": "the final record is truncated, so the true count is "
                          "at least the number of complete records and may be "
                          "more"}
    state = ATTEMPTS_KNOWN_NONZERO if entries else ATTEMPTS_KNOWN_ZERO
    return {"journal_present": True, "accounting_complete": True,
            "accounting_state": state, "entries": entries,
            "reason": "an initialized journal, read whole"}


def _invalid(path: str, why: str) -> dict:
    return {"journal_present": True, "accounting_complete": False,
            "accounting_state": ATTEMPT_JOURNAL_INVALID, "entries": [],
            "reason": f"the journal contains {why}"}


def counts(path: str) -> dict:
    """Attempt counts, or `UNKNOWN` — never a zero standing in for a gap."""
    report = read_journal(path)
    known = report["accounting_state"] in KNOWN_STATES
    entries = report["entries"]
    if known:
        provider = len(entries)
        count = sum(1 for e in entries if e["operation"] == COUNT_OPERATION)
        generation = sum(1 for e in entries
                         if e["operation"] == GENERATION_OPERATION)
    else:
        provider = count = generation = UNKNOWN
    return {"provider_attempts": provider,
            "count_attempts": count,
            "generation_attempts": generation,
            "journal_present": report["journal_present"],
            "accounting_complete": report["accounting_complete"],
            "accounting_state": report["accounting_state"],
            "accounting_reason": report["reason"]}
