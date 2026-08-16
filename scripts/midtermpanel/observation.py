"""Read GitHub again before believing it said no.

## The friction this removes, measured rather than assumed

The panel fires on `workflow_run`, which GitHub delivers within a second or two
of ordinary CI completing. At that moment the check-run objects for that run are
still being written: the record for `test (3.12)` arrives with
`status: "completed"` and `completed_at: null`, which is a self-contradictory
document — a run that has finished but has no finishing time.

`checkruns.sort_key` refuses it, correctly, because a record with no timestamp
cannot be ordered and guessing its place is how a stale green masks a fresh red.
The refusal is right about the record. It is wrong about the world: nothing has
failed, GitHub simply has not finished writing.

Measured over the five panel attempts since the lane went active, this refused
THREE. Every one cost nothing — preflight holds no credential, so no money and
no provider call was involved — and every one blocked the panel until a person
noticed and re-ran ordinary CI by hand. A gate that a human has to nudge two
times in three is not a gate anyone will keep.

## Why a re-read and not a relaxation

The obvious fix is to accept a record with no `completed_at`. That would be
wrong in exactly the way this codebase keeps finding: it makes the gate agree
with a document that contradicts itself, and it would accept the same shape when
it means something else. The rule stays exactly as strict; only the OBSERVATION
is repeated, because the question "has CI passed on this commit" has a settled
answer that this process asked slightly too early.

So: read, assert, and if the assertion refuses for a reason that means "not
yet", wait a moment and read the whole thing again. The last observation is the
authoritative one — nothing is merged across attempts, so a green can only be
reported when one complete, fresh read satisfies every rule on its own.

## What is never retried

A real red. `check_not_successful` and `triggering_run_job_not_successful` mean
a required check finished and finished badly; waiting cannot change that, and a
loop that retried them would be a loop that eventually reported the answer it
wanted. They refuse on the first observation, exactly as before.

The retryable set is an ALLOWLIST of the categories that describe GitHub's own
write lag, and it is small on purpose. Transport faults are deliberately absent:
a 502 is also transient, but it is a different problem with a different remedy,
and a retry loop that covers "the API is unreachable" is a retry loop that hides
an outage behind a slow refusal.

## Bounded, injected, and recorded

Six observations, with 2/4/6/8/10-second waits between them — at most thirty
seconds, in a job that holds no credential. The delays are a literal tuple
rather than a formula because a formula is a thing to get wrong, and the total
should be readable at a glance.

`sleep` is a parameter, so the tests run instantly and none of them can be made
to pass by waiting. The record says how many observations were needed and which
transient category each one hit, because a lane that quietly retried would hide
the very thing an operator would want to know: whether the lag is getting worse.
"""

from __future__ import annotations

import re
import time

from .errors import PanelRefusal, refuse

#: Waits BETWEEN observations. Six observations, five waits, thirty seconds.
#:
#: Escalating rather than flat: the lag is usually about a second, so the first
#: retry should be quick, and the tail should still cover a slower write without
#: six fast reads burning through it.
SETTLE_DELAYS_SECONDS = (2, 4, 6, 8, 10)

#: Observations, not retries. The first one is an observation too.
SETTLE_OBSERVATIONS = len(SETTLE_DELAYS_SECONDS) + 1

#: The categories that mean "GitHub has not finished writing", and NOTHING else.
#:
#: An allowlist, and each entry earns its place:
#:
#: `check_run_has_no_completed_at`
#:     `status: "completed"` with `completed_at: null` — the observed race, and
#:     a self-contradictory record rather than a state anything can be in.
#: `check_absent_on_head`
#:     the check run has not been attached to the commit yet. A check that will
#:     never exist still refuses, after the bound.
#: `latest_check_not_terminal`
#:     the newest attempt is still running. Often a sibling required job a few
#:     seconds behind the one that triggered this panel.
#: `triggering_run_job_incomplete`
#:     the same lag on the run's own jobs. Split out of
#:     `triggering_run_job_not_successful` precisely so that this one may be
#:     waited on and a genuine red may not.
RETRYABLE_CATEGORIES = frozenset({
    "check_run_has_no_completed_at",
    "check_absent_on_head",
    "latest_check_not_terminal",
    "triggering_run_job_incomplete",
})

#: Categories that are explicitly NOT retryable even though they are adjacent to
#: the ones that are. Listed so the distinction is a decision on the page rather
#: than an absence a later edit could fill in by accident.
NEVER_RETRIED = frozenset({
    "check_not_successful",
    "triggering_run_job_not_successful",
    "triggering_run_not_successful",
    "check_runs_tie_with_conflicting_results",
    "check_run_timestamp_unparseable",
    "check_run_has_no_id",
    "ordinary_ci_tested_a_different_combination",
})

#: `category=` must be the FIRST thing in the reason. Every refusal in this
#: package is written that way, and anchoring matters: a category name can also
#: appear in the explanatory prose that follows, and a substring search would
#: then retry a refusal that merely mentioned a retryable one.
_CATEGORY = re.compile(r"\Acategory=([a-z0-9_]+)")


def category_of(exc) -> str | None:
    """The refusal's machine-readable category, or None."""
    reason = getattr(exc, "reason", None)
    if not isinstance(reason, str):
        return None
    found = _CATEGORY.match(reason.strip())
    return found.group(1) if found else None


def is_retryable(exc) -> bool:
    """Only an allowlisted consistency category. Unknown means no.

    Fail-closed in the direction that matters: a category this module has never
    heard of is not waited on, so a new refusal added elsewhere does not
    silently acquire a thirty-second retry loop nobody asked for."""
    return category_of(exc) in RETRYABLE_CATEGORIES


def settle(*, read, assertion, where: str, sleep=None,
           delays=SETTLE_DELAYS_SECONDS) -> dict:
    """Observe until the answer settles, or refuse with what it never stopped saying.

    `read` returns a FRESH document each time it is called; `assertion` is the
    unmodified gate, applied to that document. Both are parameters because the
    property being defended is that this function adds no rule of its own — it
    decides only WHEN to look, never what counts as green.

    Every observation is complete and independent. Nothing is carried between
    attempts, so a pass can only come from one read that satisfied every rule by
    itself; there is no way for two partial observations to add up to a green.
    """
    if not callable(read) or not callable(assertion):
        refuse(f"category=settle_without_a_reader where={where}")
    pause = time.sleep if sleep is None else sleep
    delays = tuple(delays)
    transient = []
    last = None
    for index in range(len(delays) + 1):
        try:
            record = assertion(read())
        except PanelRefusal as exc:
            if not is_retryable(exc):
                # A real red, or a defect. Refused on the first observation,
                # exactly as it was before this function existed.
                raise
            last = exc
            transient.append(category_of(exc))
            if index < len(delays):
                pause(delays[index])
            continue
        observation = {
            "where": where,
            "observations": index + 1,
            "waited_seconds": sum(delays[:index]),
            "transient_categories": list(transient),
            "settled": True,
        }
        if isinstance(record, dict):
            return {**record, "observation": observation}
        return {"result": record, "observation": observation}

    # Exhausted. The ORIGINAL category leads the message, so anything reading
    # for it still finds it, and the waiting is reported after rather than
    # instead — "we looked six times over thirty seconds and it never settled"
    # is a different fact from "it was not ready", and an operator needs both.
    refuse(f"{last.reason} [settle where={where} "
           f"observations={len(delays) + 1} "
           f"waited_seconds={sum(delays)} categories={sorted(set(transient))} "
           "— re-read to the bound and the answer never settled; this is no "
           "longer GitHub's write lag]")


def summarise(*records) -> dict:
    """What the waiting cost, across every settled read in one run.

    Reported rather than hidden. A lane that quietly retried would conceal the
    one number an operator would want: whether the lag is getting worse."""
    observations = [r.get("observation") for r in records
                    if isinstance(r, dict) and isinstance(r.get("observation"),
                                                          dict)]
    return {
        "reads_settled": len(observations),
        "extra_observations": sum(max(0, o["observations"] - 1)
                                  for o in observations),
        "waited_seconds": sum(o["waited_seconds"] for o in observations),
        "transient_categories": sorted({c for o in observations
                                        for c in o["transient_categories"]}),
        "per_read": [{"where": o["where"], "observations": o["observations"],
                      "waited_seconds": o["waited_seconds"]}
                     for o in observations],
        "honest_scope": ("the gate was not relaxed. Every observation applied "
                         "the same rules to a fresh read, and only a complete "
                         "one could pass"),
    }


__all__ = [
    "SETTLE_DELAYS_SECONDS", "SETTLE_OBSERVATIONS", "RETRYABLE_CATEGORIES",
    "NEVER_RETRIED", "category_of", "is_retryable", "settle", "summarise",
]
