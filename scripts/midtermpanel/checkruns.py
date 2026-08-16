"""Which check run is the LATEST one for a context. One answer, two consumers.

## The defect this replaces

`assert_ordinary_checks_green` said "last completed wins" and implemented it by
overwriting a dict entry as it walked the list GitHub returned. That is not a
selection rule, it is a restatement of the API's ordering — and list order is
not a correctness contract. Given the same records in a different order, an
older green and a newer red produce different answers, and the direction that
matters is the one where a stale green masks a fresh red.

So selection is explicit here, and both preflight and the human merge gate call
this rather than each having a version of it.

## The order

1. `completed_at`, parsed as a real timestamp. A record whose timestamp cannot
   be parsed is refused rather than sorted to one end — "unparseable" is not
   "oldest".
2. the workflow run attempt, when the record carries one. A re-run's attempt 2
   is later than attempt 1 even if the timestamps tie.
3. the check-run id, as a deterministic tie-break. Ids ascend, and two records
   with identical timestamps and attempts still need one of them to win the
   same way every time.

## What is refused rather than resolved

- a record for a different head. Selection cannot fix that; it is a different
  question about a different commit;
- a latest record that is not `completed`. "The newest attempt is still
  running" is not a pass, and treating it as absent would silently fall back to
  an older green;
- ties that remain ties after all three keys. If two records are
  indistinguishable and disagree, there is no basis for picking one, and
  picking either would be a coin toss the caller could not see.
"""

from __future__ import annotations

import datetime

from .errors import refuse

TERMINAL = "completed"

#: The only conclusion that may be reported as green. Listed rather than
#: "not failure": GitHub adds conclusions over time (`stale`, `startup_failure`,
#: `action_required`), and a negative test would silently accept the next one.
SUCCESS = "success"


def _timestamp(value, *, name: str, conclusion=None) -> datetime.datetime:
    if not isinstance(value, str) or not value.strip():
        # The observed conclusion travels with the refusal. `observation.settle`
        # waits on this category, so without it an operator watching a run that
        # never settles would see "no timestamp" for thirty seconds and never
        # learn that the record also said `failure`.
        refuse(f"category=check_run_has_no_completed_at check={name!r} "
               f"observed_conclusion={conclusion!r} — a record with no "
               "timestamp cannot be ordered, and guessing its place is how a "
               "stale green masks a fresh red")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        refuse(f"category=check_run_timestamp_unparseable check={name!r} — "
               "refused rather than sorted to one end; unparseable is not "
               "oldest and it is not newest")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _attempt(record: dict) -> int:
    for key in ("run_attempt", "attempt", "workflow_run_attempt"):
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    nested = record.get("check_suite")
    if isinstance(nested, dict):
        value = nested.get("run_attempt")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _identifier(record: dict, *, name: str) -> int:
    value = record.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        refuse(f"category=check_run_has_no_id check={name!r} — the id is the "
               "deterministic tie-break; without it two records with the same "
               "timestamp would be resolved by list order, which is the "
               "defect this module exists to remove")
    return value


#: The place a record that has not finished occupies in the ordering. Sorted
#: LAST, because a run still going is the most recent activity on its context —
#: and because sorting it anywhere else would let an older completed green win
#: over a newer attempt, which is the fail-open this module exists to prevent.
_STILL_RUNNING = 1
_FINISHED = 0

#: A constant standing in for the timestamp a non-terminal record does not have.
#: Never compared against a real one: the rank above already separates them.
_NO_TIMESTAMP = datetime.datetime.min.replace(tzinfo=datetime.UTC)


def sort_key(record: dict, *, name: str) -> tuple:
    """Rank first, then time, then attempt, then id.

    The rank exists because a run that has not finished has no `completed_at`
    BY DESIGN — GitHub writes it when the run completes — and refusing to order
    such a record reported `check_run_has_no_completed_at` for the ordinary,
    correct state of a re-run that is still going. That message is about the
    document; the honest one is `latest_check_not_terminal`, and
    `assert_contexts_are_green` already says it.

    Nothing is loosened, and the claim is one-directional on purpose: no
    document that the old ordering PASSED can now produce a green it did not,
    because a non-terminal record sorts newest, wins its context, and is refused
    by `assert_contexts_are_green`.

    The reverse direction is not identical, and saying so matters. A record
    claiming a non-terminal status while carrying a `completed_at` used to be
    ordered by that timestamp and could be outvoted by a newer completed green;
    it now sorts newest and refuses. That is a contradictory document, refusing
    is the fail-CLOSED answer, and it is a deliberate consequence rather than an
    accident this docstring should have papered over.

    A record claiming `status: "completed"` with no timestamp still refuses
    here, because that one is not a state anything can be in: it is a document
    mid-write, and `observation.settle` waits for it."""
    if str(record.get("status") or "") != TERMINAL:
        return (_STILL_RUNNING, _NO_TIMESTAMP, _attempt(record),
                _identifier(record, name=name))
    return (_FINISHED,
            _timestamp(record.get("completed_at"), name=name,
                       conclusion=record.get("conclusion")),
            _attempt(record), _identifier(record, name=name))


def latest_by_context(check_runs, *, head_sha: str, contexts) -> dict:
    """The newest terminal record per requested context, deterministically.

    Only the requested contexts are considered. A repository accumulates check
    names, and refusing to parse an unrelated one would make this gate fail for
    reasons that have nothing to do with it."""
    if not isinstance(check_runs, list):
        refuse("category=check_runs_not_a_list")
    wanted = set(contexts)
    grouped = {}
    for record in check_runs:
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or record.get("context") or "")
        if name not in wanted:
            continue
        observed_head = record.get("head_sha") or record.get("sha")
        if observed_head is not None and str(observed_head) != head_sha:
            # A record about another commit is not a stale record about this
            # one. Dropped, not compared.
            continue
        grouped.setdefault(name, []).append(record)

    selected = {}
    for name, records in grouped.items():
        ranked = sorted(records, key=lambda r: sort_key(r, name=name))
        newest = ranked[-1]
        if len(ranked) > 1:
            runner_up = ranked[-2]
            if sort_key(newest, name=name) == sort_key(runner_up, name=name) \
                    and newest.get("conclusion") != runner_up.get("conclusion"):
                refuse(f"category=check_runs_tie_with_conflicting_results "
                       f"check={name!r} — two records are identical in "
                       "timestamp, attempt and id yet disagree; there is no "
                       "basis for choosing one, and choosing either would be a "
                       "coin toss the caller could not see")
        selected[name] = newest
    return selected


def assert_contexts_are_green(check_runs, *, head_sha: str, contexts,
                              where: str) -> dict:
    """Every named context present, terminal, and `success`, on this head.

    ORDER IS THE CONTROL, and adversarial review is what established it. These
    refusals are no longer interchangeable: `observation.settle` waits on the
    write-lag ones and must never wait on a real failure. With "absent" and
    "still running" checked first, a check that had FINISHED AND FAILED was
    masked by a sibling that had not arrived yet — the run polled for thirty
    seconds and then refused under a category that named the wrong problem.

    So a real red is evaluated FIRST, over whatever was selected, and wins.
    """
    contexts = tuple(contexts)
    selected = latest_by_context(check_runs, head_sha=head_sha,
                                 contexts=contexts)

    # 1. A RED, over whatever is present. Never retried, never masked.
    bad = sorted(f"{name}={selected[name].get('conclusion')}"
                 for name in contexts
                 if name in selected
                 and str(selected[name].get("status")) == TERMINAL
                 and selected[name].get("conclusion") is not None
                 and str(selected[name].get("conclusion")) != SUCCESS)
    if bad:
        refuse(f"category=check_not_successful where={where} checks={bad} "
               f"head={head_sha[:12]}")

    # 2. TERMINAL WITH NO CONCLUSION. The other half of the same write lag the
    #    missing `completed_at` comes from, and it used to be absorbed by the
    #    check above as `test (3.12)=None` — a permanent refusal for a state
    #    that resolves itself in a second. Its own category, so it can be
    #    waited on and a real red still cannot.
    unwritten = sorted(name for name in contexts
                       if name in selected
                       and str(selected[name].get("status")) == TERMINAL
                       and selected[name].get("conclusion") is None)
    if unwritten:
        refuse(f"category=check_conclusion_not_written where={where} "
               f"checks={unwritten} head={head_sha[:12]} — the record says the "
               "run completed and does not say how. That is a document "
               "mid-write, not an outcome")

    missing = [name for name in contexts if name not in selected]
    if missing:
        refuse(f"category=check_absent_on_head where={where} checks={missing} "
               f"head={head_sha[:12]} observed={sorted(selected)}")

    incomplete = sorted(name for name in contexts
                        if str(selected[name].get("status")) != TERMINAL)
    if incomplete:
        refuse(f"category=latest_check_not_terminal where={where} "
               f"checks={incomplete} head={head_sha[:12]} — the newest attempt "
               "has not finished. Falling back to an older completed record "
               "would report the result of a run that has been superseded")

    return {"checks": {name: {
                "conclusion": selected[name].get("conclusion"),
                "completed_at": selected[name].get("completed_at"),
                "id": selected[name].get("id"),
                "attempt": _attempt(selected[name]),
                "details_url": selected[name].get("details_url")}
            for name in contexts},
            "head_sha": head_sha,
            "selection": "completed_at, then run attempt, then check-run id"}
