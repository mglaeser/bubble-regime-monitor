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

#: Both statuses must be green on the exact head. Named, not counted: "two
#: green checks" is satisfied by the same check twice.
REQUIRED_STATUSES = (COUNT_STATUS, REVIEW_STATUS)

#: Flags that must never appear in a merge command this tool prints.
FORBIDDEN_MERGE_FLAGS = ("--admin", "--auto")

API = "https://api.github.com"


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


def check(pr_number: int, *, reviewed_sha: str, token: str,
          opener=None) -> dict:
    """Every gate, in order, then the command."""
    pull = resolve_head(pr_number, token=token, opener=opener)
    head = pull["head_sha"]
    identity = assert_reviewer_read_this_head(head, reviewed_sha=reviewed_sha)
    statuses = assert_panel_is_green(head, token=token, opener=opener)
    claims = assert_no_forbidden_claim(statuses)
    command = merge_command(pr_number=pr_number, head_sha=head)
    assert_command_is_permitted(command)
    return {
        "decision": "READY_FOR_HUMAN_MERGE",
        "pull_request": pull,
        "identity": identity,
        "statuses": statuses,
        "claims": claims,
        "merge_command": command,
        "honest_scope": (
            "the two mid-term panel statuses are green on this exact commit, "
            "the commit has not moved since it was reviewed, and no status "
            "claims a trusted or write-separated review. This says nothing "
            "about whether the panel's verdict was RIGHT: three models "
            "approved a diff, which is evidence and not proof. The person "
            "running this is the reviewer of last resort"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a pull request against the mid-term panel's "
                    "published evidence and print the merge command.")
    parser.add_argument("--pr", type=int, required=True,
                        help="pull request number")
    parser.add_argument("--reviewed-head", required=True,
                        help="the 40-hex sha you actually read the panel's "
                             "verdict for")
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
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            refuse("category=github_token_absent variable=GITHUB_TOKEN — this "
                   "tool reads the pull request and its statuses; it never "
                   "writes, and the token it needs is a read token")
        result = check(arguments.pr, reviewed_sha=arguments.reviewed_head,
                       token=token)
    except MergeGateRefusal as exc:
        print(f"MERGE_GATE_REFUSED: {exc.reason}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    print("\nRun this yourself. This tool does not merge:\n"
          f"  {result['merge_command']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
