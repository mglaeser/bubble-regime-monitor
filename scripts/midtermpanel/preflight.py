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

from . import (
    CI_WORKFLOW_NAME,
    HIGH_RISK_MARKER,
    HIGH_RISK_PATH_PREFIXES,
    REPOSITORY_NUMERIC_ID,
)
from .errors import refuse
from .status import assert_candidate_sha

#: The ordinary checks that must be green on the exact candidate head.
#: The ordinary checks that must be green on the candidate head before a
#: provider-backed run starts, and before a human merges.
#:
#: `midterm-panel-selftest` joined them on external review. It runs on
#: `pull_request` against the candidate head like the other two, and what
#: requiring it means is narrow and worth requiring: the panel's own suites,
#: including the whole no-key vertical, pass on this head. Leaving it out let a
#: pull request that broke the panel's own tests still reach the provider.
REQUIRED_ORDINARY_CHECKS = ("test (3.12)", "image", "midterm-panel-selftest")

#: The jobs that must be green INSIDE the triggering CI run itself.
#:
#: A subset of `REQUIRED_ORDINARY_CHECKS`: `midterm-panel-selftest` is its own
#: workflow, so it is not a job of the `ci` run and is covered by the exact-head
#: check-run query instead. Listing it here would refuse every real run.
REQUIRED_CI_JOBS = ("test (3.12)", "image")

#: The four review classes, and the CLOSED rule that maps a pull request to
#: exactly one of them.
#:
#: Not a per-PR allowlist. The previous design listed `pr-23`, `pr-25` and
#: `pr-29` and the workflow emitted `pr-<number>`, so PR #35 — and every routine
#: pull request after it — would have refused with
#: `review_target_not_uniquely_allowlisted` before a panel could run. That
#: contradicts the requirement this lane exists for: a review on EVERY pull
#: request.
#:
#: The class is derived from the PR number here, in trusted code, and the caller
#: supplies nothing. A free-form profile name from the environment would make
#: "which budget applies" a property of spelling.
SYNTHETIC = "SYNTHETIC"
HISTORICAL_PR25 = "HISTORICAL_PR25"
LARGE_PR23 = "LARGE_PR23"
ROUTINE_PR = "ROUTINE_PR"
REVIEW_CLASSES = (SYNTHETIC, HISTORICAL_PR25, LARGE_PR23, ROUTINE_PR)

#: The two pull requests with their own operator-approved ceilings.
CLASS_BY_PULL_REQUEST = {25: HISTORICAL_PR25, 23: LARGE_PR23}


def review_class_for(pr_number) -> str:
    """One class per pull request, total and closed.

    Total: every integer maps somewhere, so a new pull request reviews rather
    than refusing. Closed: the mapping is here and not in the environment, so
    nothing a job exports can select a bigger budget."""
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or \
            pr_number < 1:
        refuse(f"category=review_class_pull_request_not_a_positive_integer "
               f"observed={pr_number!r} — the class decides the budget, and a "
               "malformed number would decide it by accident")
    return CLASS_BY_PULL_REQUEST.get(pr_number, ROUTINE_PR)

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


#: Applicability outcomes. A privileged workflow that fires on every ordinary
#: CI run will see plenty of runs with no candidate in them, and "there is
#: nothing to review" is a normal answer, not an error.
#:
#: This distinction was missing, so every successful push to `main` produced a
#: red `midterm-panel-review`. A permanently-red signal for an expected
#: condition is worse than no signal: people learn to ignore it, and the one
#: time it goes red for a real reason it looks the same.
APPLICABLE = "APPLICABLE"
NOT_APPLICABLE_NO_PULL_REQUEST = "NOT_APPLICABLE_NO_PULL_REQUEST"
NOT_APPLICABLE_CI_NOT_SUCCESSFUL = "NOT_APPLICABLE_CI_NOT_SUCCESSFUL"

#: Underlying events this lane understands. Anything else still fails closed:
#: an unrecognised event is a workflow topology nobody designed for, and
#: treating the unknown as "not applicable" would silently skip the review for
#: every future trigger type.
KNOWN_TRIGGER_EVENTS = ("pull_request", "push")


def classify_triggering_run(run: dict) -> dict:
    """Applicable, not applicable, or refused — and which, by name.

    Separated from `assert_triggering_run` so that the two questions stay
    distinct: "should this run review anything" is an operational question
    with three answers, and "is this candidate safe to review" is a security
    question with two."""
    name = str(run.get("name") or "")
    event = str(run.get("event") or "")
    conclusion = str(run.get("conclusion") or "")
    if name != CI_WORKFLOW_NAME:
        refuse(f"category=triggering_workflow_is_not_ci name={name!r} "
               f"expected={CI_WORKFLOW_NAME!r}")
    if event not in KNOWN_TRIGGER_EVENTS:
        refuse(f"category=preflight_unknown_triggering_event event={event!r} "
               f"known={list(KNOWN_TRIGGER_EVENTS)} — an event nobody designed "
               "for is refused rather than treated as 'nothing to do', because "
               "the second reading skips the review silently")
    if event == "push":
        return {"applicability": NOT_APPLICABLE_NO_PULL_REQUEST,
                "proceed": False,
                "reason": ("a push run reviews the default branch; there is no "
                           "candidate pull request to review and nothing was "
                           "spent finding that out")}
    if conclusion != "success":
        return {"applicability": NOT_APPLICABLE_CI_NOT_SUCCESSFUL,
                "proceed": False,
                "reason": (f"the deterministic gate concluded {conclusion!r}; "
                           "a panel on top of a red tree spends money to "
                           "review something already known broken")}
    return {"applicability": APPLICABLE, "proceed": True,
            "reason": "ordinary CI passed on a pull request"}


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
    same_repository = assert_candidate_is_same_repository(pull)
    return {
        "pr_number": int(pull.get("number")),
        "head_sha": assert_candidate_sha(str((pull.get("head") or {}).get("sha")),
                                         field="pull_request.head.sha"),
        "base_sha": assert_candidate_sha(str(base.get("sha") or ""),
                                         field="pull_request.base.sha"),
        "base_ref": str(base.get("ref")),
        **same_repository,
    }


def assert_candidate_is_same_repository(pull: dict) -> dict:
    """The candidate branch must live in THIS repository. Forks are refused.

    ## Why this is a precondition for the whole acquisition model

    The privileged jobs check out with `persist-credentials: false`, which
    deliberately removes the checkout token from every later git command. That
    is the right boundary and it is not going to be weakened — but it means a
    later `git fetch` that has to reach this PRIVATE origin cannot
    authenticate.

    So the candidate's objects have to be present already, and they are:
    `fetch-depth: 0` makes `actions/checkout` fetch every branch's history
    while it still holds its own token. A same-repository pull request's head
    is a branch in origin, so its commit arrives with that checkout.

    A FORK's head is not a branch in origin. `fetch-depth: 0` does not bring
    it in, and fetching it would need exactly the authenticated round trip this
    design removed — the one case where the old fetch really did have to talk
    to the remote, and so the one case where it really would have failed.
    Rather than silently half-support forks — a run that reaches the count job
    and dies without a candidate — the lane refuses here, before any credential
    is read.

    Fork support is separate work with a separate design. Claiming it by not
    mentioning it would be the more expensive mistake."""
    head_repository = (pull.get("head") or {}).get("repo")
    if not isinstance(head_repository, dict):
        refuse("category=candidate_head_repository_absent — a pull request "
               "whose head repository cannot be identified may be a fork whose "
               "source was deleted; the candidate's objects cannot be assumed "
               "present in this repository's checkout")
    observed = head_repository.get("id")
    if isinstance(observed, bool) or not isinstance(observed, int):
        refuse(f"category=candidate_head_repository_id_malformed "
               f"observed={observed!r} — matched by NUMERIC ID, never by name: "
               "a repository can be renamed and a name can be reused, and the "
               "id is what does not move")
    if observed != REPOSITORY_NUMERIC_ID:
        refuse(f"category=candidate_repository_not_the_reviewed_repository "
               f"head_repository_id={observed} "
               f"expected={REPOSITORY_NUMERIC_ID} — this mid-term lane reviews "
               "same-repository pull requests only. A fork's head is not a "
               "branch in this origin, so `fetch-depth: 0` does not bring its "
               "objects in, and fetching them would need the authenticated "
               "round trip that `persist-credentials: false` deliberately "
               "removes. Fork support is separate work")
    return {"candidate_repository_numeric_id": observed,
            "candidate_is_same_repository": True}


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


def assert_triggering_ci_tested_this_exact_combination(
        run: dict, jobs, *, event_run_id: int, event_head_sha: str,
        current_head_sha: str, current_base_sha: str,
        main_head_sha: str) -> dict:
    """The green that authorises this panel must be about THIS base and head.

    ## The defect this replaces

    The previous check was `assert_base_is_current(pr_base_sha=...,
    main_head_sha=...)`, and its docstring said "main must not have moved past
    the base ordinary CI actually tested". It did not read what CI tested. Both
    values came from the world as it is now: the PR object's `base.sha` tracks
    the branch, so when main advances from B1 to B2 the PR's base becomes B2,
    current main is B2, and the comparison passes.

    The scenario it was written to catch was therefore the one scenario it
    could not catch. CI proved a merge of the candidate with B1; the panel
    would review `B2..head` and publish a status on a combination the
    deterministic gate never ran.

    ## What is read instead

    The triggering run, by exact id, and its own jobs. A `pull_request` CI run
    checks out a generated `refs/pull/N/merge` commit, and the run's
    `pull_requests[0]` records the head and base that merge was built from.
    That is a fact about the past and it does not move when main does.

    Four equalities, and each one is a different way the chain can break:

        tested head == event head          the payload describes this run
        tested head == current PR head     the author has not pushed since
        tested base == current PR base     the PR still targets what CI used
        tested base == current main        main has not advanced underneath

    The exact-head check-run query stays as a second, independent control. It
    answers "did something report green on this commit", which is worth
    knowing and is not the same question."""
    if not isinstance(run, dict):
        refuse("category=triggering_run_not_an_object")

    observed_id = run.get("id")
    if not isinstance(observed_id, int) or isinstance(observed_id, bool) or \
            observed_id != int(event_run_id):
        refuse(f"category=triggering_run_id_mismatch "
               f"observed={observed_id!r} event={int(event_run_id)} — the run "
               "that was read is not the run that fired this workflow")
    if str(run.get("name")) != CI_WORKFLOW_NAME:
        refuse(f"category=triggering_run_wrong_workflow "
               f"name={run.get('name')!r} expected={CI_WORKFLOW_NAME!r}")
    if str(run.get("event")) != "pull_request":
        refuse(f"category=triggering_run_wrong_event "
               f"event={run.get('event')!r} expected='pull_request' — a push "
               "run tests the branch tip, not the merge a pull request would "
               "produce, so its green is about a different tree")
    if str(run.get("conclusion")) != "success":
        refuse(f"category=triggering_run_not_successful "
               f"conclusion={run.get('conclusion')!r}")

    pulls = run.get("pull_requests")
    if not isinstance(pulls, list) or len(pulls) != 1:
        refuse(f"category=triggering_run_pull_request_not_unique "
               f"count={len(pulls) if isinstance(pulls, list) else 'n/a'} — "
               "the run must belong to exactly one pull request; zero means "
               "the tested base cannot be recovered and more than one means it "
               "is ambiguous")
    pull = pulls[0]
    tested_head = assert_candidate_sha(
        ((pull or {}).get("head") or {}).get("sha"), field="tested_head_sha")
    tested_base = assert_candidate_sha(
        ((pull or {}).get("base") or {}).get("sha"), field="tested_base_sha")

    mismatches = []
    if tested_head != event_head_sha:
        mismatches.append(f"tested_head={tested_head[:12]} "
                          f"event_head={event_head_sha[:12]}")
    if tested_head != current_head_sha:
        mismatches.append(f"tested_head={tested_head[:12]} "
                          f"current_head={current_head_sha[:12]}")
    if tested_base != current_base_sha:
        mismatches.append(f"tested_base={tested_base[:12]} "
                          f"current_base={current_base_sha[:12]}")
    if tested_base != main_head_sha:
        mismatches.append(f"tested_base={tested_base[:12]} "
                          f"current_main={main_head_sha[:12]}")
    if mismatches:
        refuse(f"category=ordinary_ci_tested_a_different_combination "
               f"found={mismatches} — the deterministic gate proved a "
               "combination that is not the one about to be reviewed. Rerun "
               "ordinary CI on the current head and base")

    assert_triggering_ci_jobs_are_green(jobs, run_id=int(event_run_id))
    attempt = run.get("run_attempt")
    return {"triggering_ci_run_id": int(event_run_id),
            "triggering_ci_run_attempt": (attempt if isinstance(attempt, int)
                                          and not isinstance(attempt, bool)
                                          else 1),
            "tested_head_sha": tested_head,
            "tested_base_sha": tested_base,
            "current_main_sha": main_head_sha}


def assert_triggering_ci_jobs_are_green(jobs, *, run_id: int) -> dict:
    """The required jobs, inside THIS run.

    `image` is checked as well as `test (3.12)`, and by name. A run whose test
    job passed while its image job was absent or red is a run that proved less
    than the required set, and reading only the first would report the answer
    the caller hoped for."""
    if not isinstance(jobs, list):
        refuse("category=triggering_run_jobs_not_a_list")
    observed = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "")
        if name in REQUIRED_CI_JOBS:
            if str(job.get("status")) != "completed":
                observed[name] = "incomplete"
            elif job.get("conclusion") is None:
                # Completed with no conclusion: the same write lag as a check
                # run with no `completed_at`, and it used to be reported as
                # `=None` under the not-successful category — a permanent
                # refusal for a state that resolves itself in a second.
                observed[name] = "unwritten"
            else:
                observed[name] = str(job.get("conclusion"))
    # SAME ORDER, SAME REASON as `checkruns.assert_contexts_are_green`. A red
    # is evaluated first and wins: with "incomplete" checked ahead of it, a job
    # that had finished and FAILED was reported under the retryable
    # `triggering_run_job_incomplete` whenever any sibling was still running,
    # so the run polled for thirty seconds and then named the wrong problem.
    bad = sorted(f"{name}={observed[name]}" for name in REQUIRED_CI_JOBS
                 if name in observed
                 and observed[name] not in ("incomplete", "unwritten", "success"))
    if bad:
        refuse(f"category=triggering_run_job_not_successful jobs={bad} "
               f"run_id={run_id}")
    unwritten = sorted(name for name in REQUIRED_CI_JOBS
                       if observed.get(name) == "unwritten")
    if unwritten:
        refuse(f"category=triggering_run_job_conclusion_not_written "
               f"jobs={unwritten} run_id={run_id} — the job says it completed "
               "and does not say how; that is a document mid-write")
    # STILL RUNNING and FAILED are two different facts, and they were reported
    # under one category. `triggering_run_job_not_successful jobs=['test
    # (3.12)=incomplete']` reads as a failure and is a job that has not
    # finished — and the distinction is now load-bearing, because
    # `observation.settle` may wait for the first and must never wait for the
    # second.
    incomplete = sorted(name for name in REQUIRED_CI_JOBS
                        if observed.get(name) == "incomplete")
    if incomplete:
        refuse(f"category=triggering_run_job_incomplete jobs={incomplete} "
               f"run_id={run_id} — the job has not finished. This is the one "
               "state that is worth re-reading: the panel fires within a second "
               "of the run completing, and a sibling required job can still be "
               "being written")
    missing = [name for name in REQUIRED_CI_JOBS if name not in observed]
    if missing:
        # LAST, and retryable. The exact analogue of `check_absent_on_head`: a
        # job the API has not listed yet is not a job that will never exist.
        refuse(f"category=triggering_run_missing_required_job jobs={missing} "
               f"run_id={run_id} observed={sorted(observed)}")
    return {"jobs": {name: observed[name] for name in REQUIRED_CI_JOBS},
            "run_id": run_id}


def assert_ordinary_checks_green(check_runs, *, head_sha: str) -> dict:
    """Every required ordinary check must be `success` ON THE EXACT HEAD.

    Re-queried rather than inferred from the triggering run's conclusion. The
    workflow_run payload reports one workflow's outcome; the required set spans
    jobs, and a repository can add a required check the payload knows nothing
    about. Reading the head's own check runs asks the question the merge gate
    will ask.

    The legacy combined-status endpoint is deliberately not used: it reports
    `pending` with `total_count: 0` when no legacy statuses exist, which is the
    normal state here and reads as 'still running' to anything that trusts it.

    Selection is `checkruns.assert_contexts_are_green`, shared with the human
    merge gate. It used to be inline, and it said "last completed wins" while
    implementing that as an overwrite in whatever order the API returned —
    which is not a rule, it is a restatement of the ordering, and the direction
    that matters is the one where a stale green masks a fresh red."""
    from .checkruns import assert_contexts_are_green
    return assert_contexts_are_green(check_runs, head_sha=head_sha,
                                     contexts=REQUIRED_ORDINARY_CHECKS,
                                     where="preflight")


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
