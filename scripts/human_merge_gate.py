"""`python scripts/human_merge_gate.py --pr N` — check, then hand the human a command.

## What this is for

The mid-term panel publishes two commit statuses and stops. Nothing in this
architecture merges anything: a machine that could merge on its own green check
is a machine whose compromise is a merge, and the whole reason this lane exists
is that its reviewer runs with a provider credential.

So the last step is a person. This tool exists because that person needs to
check five things that are tedious to check by hand and catastrophic to get
wrong, and because the merge command itself has one flag that must always be
present and two that must never be.

## The exact-head problem

A green check is a statement about a COMMIT, not about a pull request. Between
the moment a reviewer reads the panel's verdict and the moment they press merge,
the author can push. GitHub will happily merge the new head under the old
review's afterglow: the checks tab shows the latest run, the reviewer remembers
the one they read, and nothing in between says they are different commits.

`gh pr merge --match-head-commit <sha>` is the fix and it is not optional here.
It makes the merge refuse server-side if the head has moved. This tool prints
the command WITH that flag filled in from the head it actually verified, so the
sha in the command and the sha in the evidence are the same string by
construction rather than by care.

## Why it prints rather than merges

Handing the operator a command keeps the decision, and the credential that
performs it, with the human. A tool that merged would need write access, and
then the interesting question about this repository would become "what can
reach that token" rather than "did a person decide".

It also refuses to print a command containing `--admin` or `--auto`:

  * `--admin` overrides branch protection. If protection is what is blocking
    the merge, the answer is to satisfy it or to change it deliberately, not to
    step over it in the same breath as reviewing.
  * `--auto` merges LATER, when checks pass — which is precisely the merge no
    human is present for, on a head nobody has seen.

## What it cannot check

That the panel's verdict was right. Three models approved a diff; that is
evidence, not proof, and this lane's own evidence says so in every record. The
human is the reviewer of last resort and this tool is a checklist, not a
substitute.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from midtermpanel import (  # noqa: E402
    COUNT_STATUS,
    FORBIDDEN_EVIDENCE_CLASSES,
    REPOSITORY_NUMERIC_ID,
    REVIEW_STATUS,
)
from midtermpanel.checkruns import assert_contexts_are_green  # noqa: E402
from midtermpanel.errors import PanelRefusal  # noqa: E402
from midtermpanel.preflight import REQUIRED_ORDINARY_CHECKS  # noqa: E402

#: Both statuses must be green on the exact head. Named, not counted: "two
#: green checks" is satisfied by the same check twice.
REQUIRED_STATUSES = (COUNT_STATUS, REVIEW_STATUS)

#: Flags that must never appear in a merge command this tool prints.
FORBIDDEN_MERGE_FLAGS = ("--admin", "--auto")

API = "https://api.github.com"

#: The workflow that is allowed to have produced the panel statuses, by path.
PANEL_WORKFLOW_PATH = ".github/workflows/midterm-panel-review.yml"

#: The branch whose copy of that file is trusted. `workflow_run` runs the
#: default branch's definition; a run whose `head_branch` is anything else did
#: not.
DEFAULT_BRANCH = "main"

#: Both credential-bearing jobs must have succeeded in the named run.
REQUIRED_PANEL_JOBS = ("midterm-count", "midterm-panel")

#: GitHub's mergeable states that mean "no conflict". `dirty` is a conflict;
#: `unknown` means the answer is still being computed and must not be read as
#: yes.
PERMITTED_MERGEABLE_STATES = ("clean", "has_hooks", "unstable")

#: Paths whose change makes this a security review rather than a code review.
HIGH_RISK_PREFIXES = (".github/workflows/", ".github/actions/",
                      "scripts/trustedlane/", "scripts/midtermpanel/")


class MergeGateRefusal(Exception):
    """Refused. The message says which check failed and on which sha."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def refuse(reason: str):
    raise MergeGateRefusal(reason) from None


def _get(path: str, *, token: str, opener=None):
    opener = opener or urllib.request.urlopen
    # S310: `API` is a module constant and `path` is built from an integer PR
    # number and a 40-hex sha, both checked before they get here.
    request = urllib.request.Request(f"{API}{path}", method="GET")  # noqa: S310
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    try:
        with opener(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Status only. A GitHub error body can echo request headers.
        refuse(f"category=github_api_error path={path} http_status={exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        refuse(f"category=github_api_unreachable path={path} "
               f"exception_class={type(exc).__name__}")


def assert_sha(value, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(
            c not in "0123456789abcdef" for c in value):
        refuse(f"category=malformed_sha field={field}")
    return value


def resolve_head(pr_number: int, *, token: str, opener=None) -> dict:
    """The head THIS MOMENT, from the API, not from the reviewer's memory."""
    pull = _get(f"/repositories/{REPOSITORY_NUMERIC_ID}/pulls/{pr_number}",
                token=token, opener=opener)
    if pull.get("state") != "open":
        refuse(f"category=pull_request_not_open state={pull.get('state')!r}")
    if pull.get("draft"):
        refuse("category=pull_request_is_a_draft — a draft is not offered for "
               "merge, and merging one merges work its author has not finished")
    return {"pr_number": pr_number,
            "head_sha": assert_sha((pull.get("head") or {}).get("sha"),
                                   field="head.sha"),
            "base_ref": (pull.get("base") or {}).get("ref"),
            # The base SHA, not only the branch name. "the base is main" is a
            # statement about a moving target; the panel counted `base..head`
            # against one exact commit and merging into a different one merges
            # content nobody reviewed.
            "base_sha": assert_sha((pull.get("base") or {}).get("sha"),
                                   field="base.sha"),
            "mergeable_state": pull.get("mergeable_state"),
            "title": pull.get("title")}


def panel_statuses(head_sha: str, *, token: str, opener=None) -> dict:
    """The two statuses, on this sha, in their LATEST state.

    GitHub returns every status ever posted for a commit, newest first. Taking
    the first occurrence per context is what "current state" means; scanning for
    any success anywhere in the list would let a run that went green, then was
    re-run and went red, still read as green."""
    payload = _get(
        f"/repositories/{REPOSITORY_NUMERIC_ID}/commits/{head_sha}/statuses",
        token=token, opener=opener)
    if not isinstance(payload, list):
        refuse("category=status_listing_not_a_list")
    latest = {}
    for entry in payload:
        context = entry.get("context")
        if context in REQUIRED_STATUSES and context not in latest:
            latest[context] = entry
    return latest


def assert_panel_is_green(head_sha: str, *, token: str, opener=None) -> dict:
    """Both named statuses, both success, both on this exact commit."""
    latest = panel_statuses(head_sha, token=token, opener=opener)
    missing = [name for name in REQUIRED_STATUSES if name not in latest]
    if missing:
        refuse(f"category=panel_status_absent contexts={missing} "
               f"head_sha={head_sha[:12]} — the panel has not reported on THIS "
               "commit. A green check on an earlier head is a review of an "
               "earlier head")
    not_green = {name: latest[name].get("state") for name in REQUIRED_STATUSES
                 if latest[name].get("state") != "success"}
    if not_green:
        refuse(f"category=panel_status_not_success states={not_green} "
               f"head_sha={head_sha[:12]}")
    return {name: {"state": latest[name].get("state"),
                   "description": latest[name].get("description"),
                   "target_url": latest[name].get("target_url"),
                   "updated_at": latest[name].get("updated_at")}
            for name in REQUIRED_STATUSES}


def assert_ordinary_checks_are_green(head_sha: str, *, token: str,
                                     opener=None) -> dict:
    """Ordinary CI, on the exact head, latest attempt.

    The gate used to check the two panel statuses and stop, which did not
    implement the operator's rule at all: merge only after CI **and** panel are
    green on the exact head. A pull request whose tests were red could satisfy
    the old gate as long as the panel had approved the diff.

    Selection is `midtermpanel.checkruns`, the same function preflight uses, so
    "latest" cannot mean one thing when the panel decides to run and a different
    thing when a human decides to merge."""
    payload = _get(
        f"/repositories/{REPOSITORY_NUMERIC_ID}/commits/{head_sha}/check-runs",
        token=token, opener=opener)
    runs = payload.get("check_runs") if isinstance(payload, dict) else payload
    try:
        return assert_contexts_are_green(runs, head_sha=head_sha,
                                         contexts=REQUIRED_ORDINARY_CHECKS,
                                         where="human_merge_gate")
    except PanelRefusal as exc:
        # Re-raised as this tool's own type so the CLI reports one kind of
        # refusal; the reason is carried through verbatim.
        refuse(exc.reason)


def assert_panel_run_is_real(run_id: int, *, head_sha: str, base_sha: str,
                             token: str, opener=None) -> dict:
    """A privileged run, from the default branch, that produced these statuses.

    ## Why a green status is not enough

    A commit status is not self-authenticating. In a one-repository
    architecture any workflow holding `statuses: write` can post
    `midterm-panel-count = success`, and the creator still shows as
    `github-actions[bot]`. The old gate accepted exactly that.

    So the human names the run, and this proves the run is the privileged
    workflow: its path is the deployed file, its event is `workflow_run` (the
    only trigger that workflow now has), and its head branch is the default
    branch — which is what makes its definition and checkout trusted. A run of
    the same name from a pull-request branch fails all three.
    """
    run = _get(f"/repositories/{REPOSITORY_NUMERIC_ID}/actions/runs/{run_id}",
               token=token, opener=opener)
    problems = []
    if str(run.get("path")) != PANEL_WORKFLOW_PATH:
        problems.append(f"path={run.get('path')!r} "
                        f"expected={PANEL_WORKFLOW_PATH!r}")
    if str(run.get("event")) != "workflow_run":
        problems.append(f"event={run.get('event')!r} expected='workflow_run'")
    if str(run.get("head_branch")) != DEFAULT_BRANCH:
        problems.append(f"head_branch={run.get('head_branch')!r} "
                        f"expected={DEFAULT_BRANCH!r} — the privileged "
                        "definition and checkout are trusted only because they "
                        "come from the default branch")
    if str(run.get("status")) != "completed":
        problems.append(f"status={run.get('status')!r}")
    if str(run.get("conclusion")) != "success":
        problems.append(f"conclusion={run.get('conclusion')!r}")
    if problems:
        refuse(f"category=panel_run_is_not_a_privileged_run run_id={run_id} "
               f"problems={problems} — two green commit statuses prove nothing "
               "on their own; any workflow with `statuses: write` can post "
               "them")

    jobs_payload = _get(
        f"/repositories/{REPOSITORY_NUMERIC_ID}/actions/runs/{run_id}/jobs",
        token=token, opener=opener)
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else []
    by_name = {str(job.get("name")): job for job in (jobs or [])}
    for name in REQUIRED_PANEL_JOBS:
        job = by_name.get(name)
        if job is None:
            refuse(f"category=panel_run_missing_job job={name!r} run_id={run_id} "
                   f"observed={sorted(by_name)} — the statuses claim a count "
                   "and a panel; the run must contain both")
        if str(job.get("conclusion")) != "success":
            refuse(f"category=panel_run_job_not_successful job={name!r} "
                   f"conclusion={job.get('conclusion')!r} run_id={run_id}")

    # The statuses must point AT this run. A status whose target_url names a
    # different run is a status somebody else posted about somebody else's work.
    return {"run_id": run_id, "path": run.get("path"),
            "event": run.get("event"), "head_branch": run.get("head_branch"),
            "run_attempt": run.get("run_attempt"),
            "triggering_run_id": (run.get("triggering_actor") or {}).get("id"),
            "jobs": {name: by_name[name].get("conclusion")
                     for name in REQUIRED_PANEL_JOBS},
            "expected_head_sha": head_sha, "expected_base_sha": base_sha}


def assert_statuses_point_at_the_run(statuses: dict, *, run_id: int) -> dict:
    """Each status's target must be the run the human named.

    Without this the run check is decorative: it would verify a real privileged
    run exists while the green statuses on the commit came from somewhere
    else."""
    stray = {}
    for name, status in statuses.items():
        target = str(status.get("target_url") or "")
        if f"/actions/runs/{run_id}" not in target:
            stray[name] = target
    if stray:
        refuse(f"category=status_does_not_point_at_the_named_run "
               f"run_id={run_id} statuses={stray} — a real privileged run "
               "exists and these statuses are not from it")
    return {"statuses_point_at_run": run_id}


def assert_evidence_digests_match(statuses: dict, *, count_evidence_sha256: str,
                                  panel_evidence_sha256: str) -> dict:
    """The published statuses must name the evidence the operator retained.

    The count status carries its evidence digest; the panel status carries its
    own. Comparing them to the files the operator actually has is what binds
    "a run happened" to "this is what it produced"."""
    expected = {COUNT_STATUS: assert_artifact_digest(count_evidence_sha256,
                                                     field="--count-evidence-sha256"),
                REVIEW_STATUS: assert_artifact_digest(panel_evidence_sha256,
                                                      field="--panel-evidence-sha256")}
    mismatched = {}
    for name, digest in expected.items():
        description = str((statuses.get(name) or {}).get("description") or "")
        if digest[:16] not in description and digest not in description:
            mismatched[name] = description[:80]
    if mismatched:
        refuse(f"category=evidence_digest_not_in_status statuses={mismatched} "
               "— the operator's retained evidence is not the evidence these "
               "statuses were published for")
    return {"count_evidence_sha256": expected[COUNT_STATUS],
            "panel_evidence_sha256": expected[REVIEW_STATUS]}


def assert_artifact_digest(value, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
            c not in "0123456789abcdef" for c in value):
        refuse(f"category=malformed_digest field={field} — expected 64 "
               "lower-case hex characters")
    return value


def assert_base_and_mergeability(pull: dict, *, expected_base: str,
                                 token: str, opener=None) -> dict:
    """The base has not moved, and the merge would not conflict.

    A review of a diff against one base is not a review of the same diff against
    another. The panel counted `base..head`; if main advanced underneath, the
    merge produces content nobody reviewed."""
    base = assert_sha(expected_base, field="--expected-base")
    if pull["base_sha"] != base:
        refuse(f"category=base_moved_since_review reviewed_base={base[:12]} "
               f"current_base={pull['base_sha'][:12]} — the panel counted a "
               "diff against a base that is no longer where main is")
    main = _get(f"/repositories/{REPOSITORY_NUMERIC_ID}/commits/{DEFAULT_BRANCH}",
                token=token, opener=opener)
    main_sha = assert_sha(main.get("sha"), field="main.sha")
    if main_sha != base:
        refuse(f"category=default_branch_moved main={main_sha[:12]} "
               f"reviewed_base={base[:12]} — merging now merges into a main "
               "the panel never saw")
    state = str(pull.get("mergeable_state") or "")
    if state not in PERMITTED_MERGEABLE_STATES:
        refuse(f"category=pull_request_not_cleanly_mergeable state={state!r} "
               f"permitted={list(PERMITTED_MERGEABLE_STATES)}")
    return {"base_sha": base, "main_sha": main_sha, "mergeable_state": state}


def assert_high_risk_review_when_required(pull: dict, *, head_sha: str,
                                          approval_path, token: str,
                                          opener=None) -> dict:
    """A PR touching the panel's own machinery needs a human security review.

    The panel can read a workflow-changing diff as data. It cannot stop that
    PR's own workflows from referencing the repository secret once merged, so a
    person has to have looked."""
    files = _get(
        f"/repositories/{REPOSITORY_NUMERIC_ID}/pulls/{pull['pr_number']}/files",
        token=token, opener=opener)
    paths = [str(entry.get("filename") or "") for entry in (files or [])]
    risky = sorted(p for p in paths
                   if any(p.startswith(prefix) for prefix in HIGH_RISK_PREFIXES))
    if not risky:
        return {"high_risk_paths": [], "review_required": False}
    if not approval_path:
        refuse(f"category=high_risk_change_without_review paths={risky[:6]} "
               f"count={len(risky)} — this pull request edits the machinery "
               "that holds the provider key; --human-approval is required")
    try:
        with open(approval_path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        refuse(f"category=human_approval_unreadable "
               f"exception_class={type(exc).__name__}")
    if record.get("workflow_security_review_completed") is not True:
        refuse("category=human_approval_does_not_assert_review")
    if record.get("reviewed_head_sha") != head_sha:
        refuse(f"category=human_approval_names_another_head "
               f"approved={str(record.get('reviewed_head_sha'))[:12]} "
               f"current={head_sha[:12]}")
    for field in ("reviewer", "reviewed_at"):
        if not str(record.get(field) or "").strip():
            refuse(f"category=human_approval_incomplete field={field}")
    return {"high_risk_paths": risky, "review_required": True,
            "reviewer": record["reviewer"],
            "reviewed_at": record["reviewed_at"]}


def assert_no_forbidden_claim(statuses: dict) -> dict:
    """No status may describe this review as trusted or write-separated.

    Cheap, and it catches the thing this whole programme is careful about: a
    description edited to read like the trusted lane's would put a claim in
    front of the one human whose judgement the architecture depends on."""
    blob = json.dumps(statuses)
    found = [name for name in FORBIDDEN_EVIDENCE_CLASSES if name in blob]
    if found:
        refuse(f"category=status_claims_a_forbidden_evidence_class "
               f"classes={found} — this lane is a mid-term single-repository "
               "panel; it is not write-separated and no third party attested "
               "to it")
    for name, status in statuses.items():
        description = str(status.get("description") or "")
        if "mid-term" not in description:
            refuse(f"category=status_description_does_not_say_what_it_is "
                   f"context={name} — every published description states the "
                   "lane it came from, and one that does not is either not "
                   "this lane's or has been edited")
    return {"forbidden_claims": [], "descriptions_declare_the_lane": True}


def assert_reviewer_read_this_head(head_sha: str, *, reviewed_sha: str) -> dict:
    """The commit the human read must be the commit about to be merged.

    This is the one check no API can do for the reviewer: they type the sha
    they actually looked at, and the tool compares. If it disagrees, the answer
    is not to override it — it is to go and read the new head."""
    reviewed = assert_sha(reviewed_sha, field="--reviewed-head")
    if reviewed != head_sha:
        refuse(f"category=head_moved_since_review reviewed={reviewed[:12]} "
               f"current={head_sha[:12]} — the author pushed after the review. "
               "The panel's verdict is about the commit it ran on; read the "
               "new head and re-run this")
    return {"reviewed_head_sha": reviewed, "current_head_sha": head_sha,
            "head_is_unchanged": True}


def merge_command(*, pr_number: int, head_sha: str) -> str:
    """The command, with the sha filled in from what was verified.

    `--match-head-commit` is what makes the check above hold at merge time
    rather than at check time: the server refuses if the head has moved in the
    seconds since. Building the string here rather than documenting it means
    the sha in the command cannot be a different sha from the one that passed."""
    command = (f"gh pr merge {pr_number} "
               f"--match-head-commit {head_sha} --squash")
    present = [flag for flag in FORBIDDEN_MERGE_FLAGS if flag in command]
    if present:
        refuse(f"category=forbidden_merge_flag flags={present}")
    return command


def assert_command_is_permitted(command: str) -> str:
    """For the operator to run against a command they were given elsewhere.

    `--admin` steps over branch protection; `--auto` merges later, when no
    human is present, on a head nobody has seen. Neither belongs in the same
    breath as a review."""
    present = [flag for flag in FORBIDDEN_MERGE_FLAGS
               if flag in str(command).split()]
    if present:
        refuse(f"category=forbidden_merge_flag flags={present} — `--admin` "
               "overrides branch protection and `--auto` merges when nobody "
               "is looking. If protection is blocking the merge, satisfy it "
               "or change it deliberately")
    if "--match-head-commit" not in str(command):
        refuse("category=merge_command_without_match_head_commit — without it "
               "the merge takes whatever the head is at the moment it runs, "
               "which is not necessarily the commit that was reviewed")
    return command


def check(pr_number: int, *, reviewed_sha: str, expected_base: str,
          panel_run_id: int, count_evidence_sha256: str,
          panel_evidence_sha256: str, human_approval=None, token: str,
          opener=None) -> dict:
    """Every gate, in order, then the command.

    The order is deliberate: identity before anything expensive, then the
    cheap green checks, then the run that proves the greens were produced by
    the privileged workflow rather than posted by anything with
    `statuses: write`."""
    pull = resolve_head(pr_number, token=token, opener=opener)
    head = pull["head_sha"]
    identity = assert_reviewer_read_this_head(head, reviewed_sha=reviewed_sha)
    base = assert_base_and_mergeability(pull, expected_base=expected_base,
                                        token=token, opener=opener)
    ordinary = assert_ordinary_checks_are_green(head, token=token,
                                                opener=opener)
    statuses = assert_panel_is_green(head, token=token, opener=opener)
    claims = assert_no_forbidden_claim(statuses)
    run = assert_panel_run_is_real(panel_run_id, head_sha=head,
                                   base_sha=base["base_sha"], token=token,
                                   opener=opener)
    pointing = assert_statuses_point_at_the_run(statuses, run_id=panel_run_id)
    evidence = assert_evidence_digests_match(
        statuses, count_evidence_sha256=count_evidence_sha256,
        panel_evidence_sha256=panel_evidence_sha256)
    high_risk = assert_high_risk_review_when_required(
        pull, head_sha=head, approval_path=human_approval, token=token,
        opener=opener)
    command = merge_command(pr_number=pr_number, head_sha=head)
    assert_command_is_permitted(command)
    return {
        "decision": "READY_FOR_HUMAN_MERGE",
        "pull_request": pull,
        "identity": identity,
        "base": base,
        "ordinary_checks": ordinary,
        "statuses": statuses,
        "claims": claims,
        "panel_run": run,
        "status_provenance": pointing,
        "evidence": evidence,
        "high_risk": high_risk,
        "merge_command": command,
        "honest_scope": (
            "ordinary CI and both mid-term panel statuses are green on this "
            "exact commit; the greens were produced by a real privileged "
            "`workflow_run` of the deployed workflow from the default branch, "
            "not merely posted onto the commit; the base has not moved and the "
            "merge is clean; the retained evidence digests are the ones the "
            "statuses were published for; and, where the change touches the "
            "panel's own machinery, a human recorded a security review of this "
            "head. This says nothing about whether the panel's verdict was "
            "RIGHT: three models approved a diff, which is evidence and not "
            "proof. The person running this is the reviewer of last resort"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a pull request against ordinary CI, the mid-term "
                    "panel's published evidence and the privileged run that "
                    "produced it, then print the merge command.")
    parser.add_argument("--pr", type=int, required=True,
                        help="pull request number")
    parser.add_argument("--reviewed-head", required=True,
                        help="the 40-hex sha you actually read the panel's "
                             "verdict for")
    parser.add_argument("--expected-base", required=False,
                        help="the 40-hex base sha the panel counted against")
    parser.add_argument("--panel-run-id", type=int, required=False,
                        help="the Actions run id of the privileged panel run")
    parser.add_argument("--count-evidence-sha256", required=False,
                        help="digest of the count evidence you retained")
    parser.add_argument("--panel-evidence-sha256", required=False,
                        help="digest of the panel evidence you retained")
    parser.add_argument("--human-approval", default=None,
                        help="path to a workflow security review record; "
                             "required when the PR touches workflows, actions, "
                             "trustedlane or midtermpanel")
    parser.add_argument("--check-command", default=None,
                        help="validate a merge command instead of building "
                             "one, and exit")
    arguments = parser.parse_args(argv)

    try:
        if arguments.check_command:
            assert_command_is_permitted(arguments.check_command)
            print(json.dumps({"decision": "COMMAND_PERMITTED",
                              "command": arguments.check_command}, indent=2))
            return 0
        # Required for a real check, but not by argparse: `--check-command`
        # legitimately needs none of them, and marking them required would make
        # the flag-validation path demand evidence it does not use.
        missing = [name for name, value in (
            ("--expected-base", arguments.expected_base),
            ("--panel-run-id", arguments.panel_run_id),
            ("--count-evidence-sha256", arguments.count_evidence_sha256),
            ("--panel-evidence-sha256", arguments.panel_evidence_sha256))
            if value in (None, "")]
        if missing:
            refuse(f"category=merge_gate_evidence_missing arguments={missing} "
                   "— two green statuses are not evidence that a privileged "
                   "run produced them; the gate needs the run and the digests")
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            refuse("category=github_token_absent variable=GITHUB_TOKEN — this "
                   "tool reads the pull request, its checks and its run; it "
                   "never writes, and the token it needs is a read token")
        result = check(arguments.pr, reviewed_sha=arguments.reviewed_head,
                       expected_base=arguments.expected_base,
                       panel_run_id=arguments.panel_run_id,
                       count_evidence_sha256=arguments.count_evidence_sha256,
                       panel_evidence_sha256=arguments.panel_evidence_sha256,
                       human_approval=arguments.human_approval, token=token)
    except MergeGateRefusal as exc:
        print(f"MERGE_GATE_REFUSED: {exc.reason}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    print("\nRun this yourself. This tool does not merge:\n"
          f"  {result['merge_command']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
