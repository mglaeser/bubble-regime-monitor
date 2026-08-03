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


def _timestamp(value, *, name: str) -> datetime.datetime:
    if not isinstance(value, str) or not value.strip():
        refuse(f"category=check_run_has_no_completed_at check={name!r} — a "
               "record with no timestamp cannot be ordered, and guessing its "
               "place is how a stale green masks a fresh red")
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


def sort_key(record: dict, *, name: str) -> tuple:
    return (_timestamp(record.get("completed_at"), name=name),
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
    """Every named context present, terminal, and `success`, on this head."""
    contexts = tuple(contexts)
    selected = latest_by_context(check_runs, head_sha=head_sha,
                                 contexts=contexts)
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

    bad = sorted(f"{name}={selected[name].get('conclusion')}"
                 for name in contexts
                 if str(selected[name].get("conclusion")) != SUCCESS)
    if bad:
        refuse(f"category=check_not_successful where={where} checks={bad} "
               f"head={head_sha[:12]}")

    return {"checks": {name: {
                "conclusion": selected[name].get("conclusion"),
                "completed_at": selected[name].get("completed_at"),
                "id": selected[name].get("id"),
                "attempt": _attempt(selected[name]),
                "details_url": selected[name].get("details_url")}
            for name in contexts},
            "head_sha": head_sha,
            "selection": "completed_at, then run attempt, then check-run id"}
