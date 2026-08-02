"""Decide whether this run may proceed, before any credential is in scope.

## What preflight is actually for

The panel spends money and publishes a check a human will read. Both of those
are irreversible in the way that matters — you cannot un-review a commit, and you
cannot un-publish a green status somebody has already merged on. So everything
that can be established BEFORE a provider key exists in the process is
established here, in a job that has none.

## The identity problem

A `workflow_run` event hands the privileged workflow a payload about a run that
has already finished. Between that run finishing and this one starting, the pull
request can be updated, closed, merged, retargeted, or have its base branch move
underneath it. The event payload does not change; the world does.

So nothing here trusts the payload. Every fact is re-fetched from the API and
compared:

    payload says head X  ->  the PR's CURRENT head must also be X
    payload says success ->  the exact head's check runs must ALSO say success
    payload names a PR   ->  exactly one open PR must match, and its base must
                             be main, and main must not have moved past the base
                             the ordinary checks actually ran against

That last one is the subtle one, and it is why `assert_base_is_current` exists.
Ordinary CI ran against a merge of the candidate head with main AS IT WAS. If
main has since moved, a green `test (3.12)` describes a tree that no longer
corresponds to what merging would produce. Reviewing that combination is
reviewing something nobody tested, so the correct answer is to block and require
a CI rerun rather than to review a stale combination and publish a green check
on it.
"""

from __future__ import annotations

import hmac
import json

from . import CI_WORKFLOW_NAME, HIGH_RISK_MARKER, HIGH_RISK_PATH_PREFIXES
from .errors import refuse
from .status import assert_candidate_sha

#: The ordinary checks that must be green on the exact candidate head.
REQUIRED_ORDINARY_CHECKS = ("test (3.12)", "image")

#: Manual dispatch must name the head it is authorising.
APPROVAL_PREFIX = "REVIEW_EXACT_HEAD_"

#: Largest API body this module will parse.
#:
#: A cap rather than trust. The bodies here are PR and check-run metadata, which
#: are kilobytes; anything vastly larger is either an API change nobody reviewed
#: or a response that is not what it claims to be, and parsing it to find out is
#: the wrong order of operations.
MAX_BODY_BYTES = 2 * 1024 * 1024


def approval_phrase_for(head_sha: str) -> str:
    """The exact phrase a manual dispatch must carry for THIS head.

    A generic word like `REVIEW` authorises any dispatch, including one typed
    against an earlier head and re-submitted after the branch moved. Binding the
    phrase to the SHA makes an authorisation for one commit useless for
    another — the operator has to look at what they are actually approving."""
    return APPROVAL_PREFIX + assert_candidate_sha(head_sha)


def assert_approval_phrase(provided, head_sha: str) -> dict:
    """Constant-time comparison against the phrase for the resolved head.

    `hmac.compare_digest` rather than `==`. The phrase is not a secret, so the
    timing channel is not the real reason; the real reason is that this is the
    shape every comparison-of-an-authorisation should have, and a codebase where
    some of them are `==` invites the question of which ones matter."""
    expected = approval_phrase_for(head_sha)
    if not isinstance(provided, str) or not provided:
        refuse("category=approval_phrase_absent — a manual dispatch must name "
               f"the exact head it authorises: {APPROVAL_PREFIX}<40-hex>")
    if not hmac.compare_digest(provided.strip(), expected):
        refuse("category=approval_phrase_does_not_match_resolved_head — the "
               "phrase authorises a different commit than the one this run "
               "resolved; an approval typed before the branch moved is not an "
               "approval of what is there now")
    return {"approval_phrase_bound_to": head_sha}


def parse_api_json(raw: bytes, *, where: str):
    """Strict JSON: size-capped, duplicate keys refused, errors sanitized.

    Duplicate-key rejection matters more than it looks. A response declaring
    `"head"` twice parses fine under the default loader and silently keeps the
    last one, so the value this code binds to would depend on the order a server
    happened to serialise — which is exactly the kind of thing an attacker
    controls and a reviewer does not read."""
    if not isinstance(raw, (bytes, bytearray)):
        refuse(f"category=api_body_not_bytes where={where}")
    if len(raw) > MAX_BODY_BYTES:
        refuse(f"category=api_body_too_large where={where} bytes={len(raw)} "
               f"max={MAX_BODY_BYTES}")

    def _no_duplicates(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                refuse(f"category=api_body_duplicate_key where={where} "
                       f"key={key!r}")
            seen[key] = value
        return seen

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except UnicodeDecodeError:
        refuse(f"category=api_body_not_utf8 where={where}")
    except json.JSONDecodeError as exc:
        # Line number only. A malformed body's CONTENT is not echoed: it is
        # server-controlled text arriving in a job that will shortly hold a key.
        refuse(f"category=api_body_not_json where={where} line={exc.lineno}")


def assert_triggering_run(run: dict) -> dict:
    """The triggering run must be ordinary CI, on a pull request, and green.

    All three, and each for its own reason. A `push` run reviews main rather
    than a candidate. A failed run means the deterministic gate did not pass, and
    a panel on top of a red tree spends money to review something already known
    broken. A run of some other workflow is a workflow a pull request may have
    added."""
    name = str(run.get("name") or "")
    event = str(run.get("event") or "")
    conclusion = str(run.get("conclusion") or "")
    if name != CI_WORKFLOW_NAME:
        refuse(f"category=triggering_workflow_is_not_ci name={name!r} "
               f"expected={CI_WORKFLOW_NAME!r}")
    if event != "pull_request":
        refuse(f"category=triggering_event_is_not_pull_request event={event!r} "
               "— a push run reviews the default branch, not a candidate")
    if conclusion != "success":
        refuse(f"category=triggering_run_not_successful conclusion={conclusion!r} "
               "— the deterministic gate is the precondition for spending "
               "anything on a panel")
    head = assert_candidate_sha(str(run.get("head_sha") or ""),
                                field="workflow_run.head_sha")
    return {"workflow": name, "event": event, "conclusion": conclusion,
            "head_sha": head}


def resolve_pull_request(pulls, *, run_head_sha: str) -> dict:
    """Exactly one open pull request, whose CURRENT head is the run's head.

    Ambiguity is refused rather than resolved by picking the first. Two open PRs
    matching one head is a state nobody designed for, and choosing one of them
    silently means the status lands on a pull request the reviewer was not
    looking at."""
    if not isinstance(pulls, list):
        refuse("category=pull_request_list_not_a_list")
    open_matching = [
        p for p in pulls
        if isinstance(p, dict)
        and str(p.get("state")) == "open"
        and not p.get("merged_at")
        and str((p.get("head") or {}).get("sha") or "") == run_head_sha
    ]
    if not open_matching:
        refuse(f"category=no_open_pull_request_for_head "
               f"head={run_head_sha[:12]} — the pull request was closed, "
               "merged, or its head moved after ordinary CI finished")
    if len(open_matching) > 1:
        numbers = sorted(int(p.get("number", 0)) for p in open_matching)
        refuse(f"category=ambiguous_pull_request_mapping numbers={numbers} — "
               "two open pull requests share this head; publishing a status "
               "would land it on whichever one this code picked first")
    pull = open_matching[0]
    base = pull.get("base") or {}
    if str(base.get("ref")) != "main":
        refuse(f"category=pull_request_base_is_not_main "
               f"base={str(base.get('ref'))!r}")
    return {
        "pr_number": int(pull.get("number")),
        "head_sha": assert_candidate_sha(str((pull.get("head") or {}).get("sha")),
                                         field="pull_request.head.sha"),
        "base_sha": assert_candidate_sha(str(base.get("sha") or ""),
                                         field="pull_request.base.sha"),
        "base_ref": str(base.get("ref")),
    }


def assert_head_is_unmoved(*, run_head_sha: str, current_head_sha: str) -> dict:
    """The payload's head and the PR's current head must be the same commit.

    They diverge whenever the author pushes while the panel is starting. The
    review would then describe the old commit while the status landed on... also
    the old commit, which is worse than useless: a stale green sitting on a
    commit nobody will merge, next to a new head with no check at all."""
    if run_head_sha != current_head_sha:
        refuse(f"category=candidate_head_moved_since_ci "
               f"ci_head={run_head_sha[:12]} current_head={current_head_sha[:12]} "
               "— the pull request was updated after ordinary CI finished; the "
               "new head needs its own CI run and its own panel")
    return {"head_sha": current_head_sha, "moved": False}


def assert_base_is_current(*, pr_base_sha: str, main_head_sha: str) -> dict:
    """Main must not have moved past the base ordinary CI actually tested.

    This is the check that is easy to leave out and expensive to leave out.
    `test (3.12)` ran against a merge of the candidate with main as it was. If
    main has advanced, that green describes a combination that no longer exists,
    and the panel would review a tree nobody ran the deterministic gate on.

    Blocking and requiring a CI rerun is the honest answer. Reviewing the stale
    combination and publishing a green check on it is how a merge happens on
    evidence that was true about different code."""
    if pr_base_sha != main_head_sha:
        refuse(f"category=base_moved_since_ordinary_ci "
               f"tested_base={pr_base_sha[:12]} current_main={main_head_sha[:12]} "
               "— ordinary CI proved a combination that no longer exists; rerun "
               "CI on the updated base rather than reviewing an untested tree")
    return {"base_sha": pr_base_sha, "moved": False}


def assert_ordinary_checks_green(check_runs, *, head_sha: str) -> dict:
    """Every required ordinary check must be `success` ON THE EXACT HEAD.

    Re-queried rather than inferred from the triggering run's conclusion. The
    workflow_run payload reports one workflow's outcome; the required set spans
    jobs, and a repository can add a required check the payload knows nothing
    about. Reading the head's own check runs asks the question the merge gate
    will ask.

    The legacy combined-status endpoint is deliberately not used: it reports
    `pending` with `total_count: 0` when no legacy statuses exist, which is the
    normal state here and reads as 'still running' to anything that trusts it."""
    if not isinstance(check_runs, list):
        refuse("category=check_runs_not_a_list")
    outcome = {}
    for run in check_runs:
        if not isinstance(run, dict):
            continue
        if str(run.get("head_sha") or head_sha) != head_sha:
            continue
        name = str(run.get("name") or "")
        if str(run.get("status")) != "completed":
            outcome.setdefault(name, "incomplete")
            continue
        # Last completed wins; a rerun that turned red must not be masked by an
        # earlier green with the same name.
        outcome[name] = str(run.get("conclusion") or "")
    missing = [c for c in REQUIRED_ORDINARY_CHECKS if c not in outcome]
    if missing:
        refuse(f"category=ordinary_check_absent_on_head checks={missing} "
               f"head={head_sha[:12]} observed={sorted(outcome)}")
    bad = sorted(f"{c}={outcome[c]}" for c in REQUIRED_ORDINARY_CHECKS
                 if outcome[c] != "success")
    if bad:
        refuse(f"category=ordinary_check_not_successful checks={bad} "
               f"head={head_sha[:12]}")
    return {"checks": {c: outcome[c] for c in REQUIRED_ORDINARY_CHECKS},
            "head_sha": head_sha}


def classify_changed_files(paths) -> dict:
    """Flag the highest-risk class while a repository-scoped secret exists.

    The panel can review a workflow-changing PR's diff as data. It cannot stop
    that PR's OWN workflows from referencing the repository secret, so the
    marker exists to make a human look at the diff rather than at the check."""
    if not isinstance(paths, (list, tuple)):
        refuse("category=changed_files_not_a_list")
    names = [str(p) for p in paths]
    risky = sorted(p for p in names
                   if any(p.startswith(prefix)
                          for prefix in HIGH_RISK_PATH_PREFIXES))
    return {
        "changed_files": len(names),
        "high_risk": bool(risky),
        "high_risk_paths": risky,
        "marker": HIGH_RISK_MARKER if risky else "",
        "warning": (
            "CRITICAL RESIDUAL: this pull request changes GitHub Actions or "
            "lane code while a repository-scoped provider secret exists. The "
            "panel review does not prevent the pull request's own workflows "
            "from referencing that secret." if risky else ""),
    }
