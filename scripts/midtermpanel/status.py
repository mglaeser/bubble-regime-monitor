"""Publish `midterm-panel-*` onto the exact candidate head — and nothing else.

## Why this is a second publisher rather than a widened first one

`trustedlane.statuspublish.assert_publishable_context` is a choke point with one
rule: `if context not in TRUSTED_STATUSES: refuse`. It is the reason a
mis-wired caller cannot publish `independent-verify-inactive` into a slot an
operator reserved for a real review.

The obvious way to make this package work is to add the mid-term contexts to
that tuple. That would be a mistake, and naming it is worth more than avoiding
it silently: the trusted publisher would then accept two classes of context, and
the single sentence that makes it safe — "this function publishes trusted
contexts and no others" — would stop being true. A reviewer checking whether a
trusted context can be published by something that is not the trusted lane would
have to read call sites instead of reading one tuple.

So there are two publishers, each with a hardcoded allowlist of its own class,
and neither can publish the other's contexts. `assert_publishable_context` here
refuses every trusted name explicitly rather than merely omitting it, so the
failure mode is a refusal that says why rather than a `None` that says nothing.

## Why the status must land on the candidate SHA

The panel runs via `workflow_run` from the default branch. A dispatched run's own
job checks are associated with the commit it ran from — main — not with the pull
request under review. So reserving a job name achieves nothing for the PR; the
panel has to publish the context EXPLICITLY onto the exact candidate head, which
is why `assert_candidate_sha` refuses anything that is not a 40-hex commit.
`HEAD`, `refs/heads/main` and an abbreviated SHA are all refused: each of them
would publish a real status onto the wrong thing, and a green check on the wrong
commit is worse than no check at all.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import (
    COUNT_STATUS,
    FORBIDDEN_STATUSES,
    PANEL_STATUSES,
    REPOSITORY_NUMERIC_ID,
    REVIEW_STATUS,
)
from .errors import refuse
from .evidence import public_digest_marker

#: GitHub truncates status descriptions past this length. Refused rather than
#: truncated here: a description trimmed by the API could lose the `ev=` marker
#: the merge gate reads, and the gate would then block a legitimate run for a
#: reason nobody could see from the outside.
MAX_DESCRIPTION_CHARS = 140

#: Commit-status states GitHub accepts.
STATES = ("pending", "success", "failure", "error")

#: The states that end a status's life. `finalize` exists to guarantee one of
#: these is reached on every path, because a status left `pending` forever is a
#: pull request that can never satisfy its own check.
TERMINAL_STATES = ("success", "failure", "error")

#: GitHub truncates a longer description silently, which would turn a refusal
#: reason into a half-sentence. Refused rather than truncated here.
MAX_DESCRIPTION = 140

_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


def assert_publishable_context(context: str) -> str:
    """Only the two mid-term contexts, and a named refusal for trusted ones.

    The trusted names are refused with their own message rather than falling
    through the generic branch. "midterm-panel-review is not publishable here"
    and "you tried to publish trusted-cross-vendor-review from the mid-term
    panel" are different bugs, and only the second one is an attempt to claim an
    authority this architecture does not have."""
    if context in FORBIDDEN_STATUSES:
        refuse(f"category=midterm_panel_attempted_reserved_context "
               f"context={context!r} — this context is reserved for a "
               "write-separated reviewer. The mid-term panel holds a "
               "repository-scoped secret in the repository it reviews and rests "
               "on no recorded operator prerequisite; publishing under that name "
               "would let it satisfy a requirement written for something else")
    if context not in PANEL_STATUSES:
        refuse(f"category=context_not_publishable_by_midterm_panel "
               f"context={context!r} permitted={list(PANEL_STATUSES)}")
    return context


def assert_candidate_sha(value, *, field: str = "candidate_head_sha") -> str:
    """A 40-hex lowercase commit id, or a refusal.

    `HEAD`, a branch name, `refs/heads/…` and an abbreviated SHA are each
    refused. Every one of them resolves to *something*, which is the danger: the
    status would publish successfully onto a commit nobody reviewed."""
    if not isinstance(value, str) or not _SHA1.match(value):
        refuse(f"category=candidate_sha_not_a_commit_sha field={field} — a "
               "status must land on the exact reviewed commit; a ref name "
               "resolves to whatever it points at now")
    return value


def status_request(*, repository_numeric_id: int, candidate_head_sha: str,
                   context: str, state: str, description: str,
                   target_url: str, run_id, run_attempt,
                   evidence_sha256=None) -> dict:
    """Build the exact request, validating every field before it leaves.

    Keyword-only throughout. A positional call site that swapped `context` and
    `state` would publish a state named `midterm-panel-review`, which GitHub
    would reject — but a call site that swapped `state` and `description` would
    publish successfully and mean nothing.

    A terminal `success` REQUIRES an evidence digest. That coupling is the point:
    a green status with nothing behind it is exactly the shape of claim this
    whole programme exists to refuse, and the constructor is where it is cheapest
    to make impossible."""
    from trustedlane.identity import assert_repository_numeric_id
    assert_repository_numeric_id(repository_numeric_id)
    assert_candidate_sha(candidate_head_sha)
    assert_publishable_context(context)

    if state not in STATES:
        refuse(f"category=status_state_unknown state={state!r} "
               f"permitted={list(STATES)}")
    if not isinstance(description, str) or not description.strip():
        refuse("category=status_description_empty — the description is the only "
               "thing a reviewer reads next to the check")
    if len(description) > MAX_DESCRIPTION:
        refuse(f"category=status_description_too_long length={len(description)} "
               f"max={MAX_DESCRIPTION} — GitHub truncates silently, which turns "
               "a refusal reason into half a sentence")
    if not isinstance(target_url, str) or not target_url.startswith("https://"):
        refuse("category=status_target_url_not_https")
    for name, value in (("run_id", run_id), ("run_attempt", run_attempt)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            refuse(f"category=status_{name}_not_a_positive_int")
    if state == "success":
        if not isinstance(evidence_sha256, str) or not _SHA256.match(evidence_sha256):
            refuse("category=success_status_without_evidence_digest — a green "
                   "check with no evidence behind it is a claim nobody can "
                   "check; success must name the digest of what it rests on")
    return {
        "repository_numeric_id": repository_numeric_id,
        "candidate_head_sha": candidate_head_sha,
        "context": context,
        "state": state,
        "description": description,
        "target_url": target_url,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "evidence_sha256": evidence_sha256,
        # Carried on every published status so that anything reading it back
        # knows what kind of authority produced it without consulting a document.
        "authority": "MIDTERM_SINGLE_REPO_PANEL_NOT_WRITE_SEPARATED",
    }


def assert_no_pending_left(published: list) -> dict:
    """Every context this run touched reached a terminal state.

    `finalize` calls this. A `pending` status that never resolves is not a
    neutral outcome — under a required-status configuration it blocks the pull
    request forever, and under no configuration it silently reads as "still
    working" long after the run died."""
    last = {}
    for record in published:
        context = record.get("context")
        if context in PANEL_STATUSES:
            last[context] = record.get("state")
    stuck = sorted(c for c, s in last.items() if s not in TERMINAL_STATES)
    if stuck:
        refuse(f"category=midterm_status_left_pending contexts={stuck} — a "
               "status that never resolves blocks the pull request under a "
               "required-status configuration and reads as 'still working' "
               "under any other")
    return {"contexts": sorted(last), "all_terminal": True}


def publish(request: dict, *, opener, token: str) -> dict:
    """POST the status. The opener and the token are PARAMETERS.

    Neither is obtained here, and that is the seam the whole package is built
    around: a function that fetches its own credential cannot be tested without
    one, and cannot be prevented from using one. Tests pass a fake opener and a
    fake token; the workflow passes the real ones. The phase gate in the trusted
    lane sits on OBTAINING a capability for exactly this reason, and the mid-term
    lane keeps the same shape even though its gate is different.

    The request is re-validated rather than trusted. It arrived as a dict, and a
    dict can be edited between construction and here."""
    status_request(
        repository_numeric_id=request["repository_numeric_id"],
        candidate_head_sha=request["candidate_head_sha"],
        context=request["context"], state=request["state"],
        description=request["description"], target_url=request["target_url"],
        run_id=request["run_id"], run_attempt=request["run_attempt"],
        evidence_sha256=request.get("evidence_sha256"))
    if not token or not isinstance(token, str):
        refuse("category=status_token_absent — the panel cannot publish without "
               "a token, and must say so rather than reporting a status it "
               "never sent")

    url = (f"https://api.github.com/repositories/"
           f"{request['repository_numeric_id']}/statuses/"
           f"{request['candidate_head_sha']}")
    # The description is the ONLY field GitHub will show a human, and it is the
    # only place a digest can travel. `status_request` has always accepted an
    # `evidence_sha256`; this used to drop it, so the digest existed inside the
    # process and nowhere a reader could see it — and the human merge gate then
    # searched the published description for a string nothing had put there,
    # which meant a real successful run could not pass its own gate.
    description = request["description"]
    if request.get("evidence_sha256"):
        marker = public_digest_marker(request["evidence_sha256"])
        if marker not in description:
            description = f"{description} {marker}"
    if len(description) > MAX_DESCRIPTION_CHARS:
        refuse(f"category=status_description_too_long chars={len(description)} "
               f"limit={MAX_DESCRIPTION_CHARS} — GitHub truncates, and a "
               "truncated description can lose the evidence marker the merge "
               "gate reads")
    body = json.dumps({
        "state": request["state"],
        "context": request["context"],
        "description": description,
        "target_url": request["target_url"],
    }).encode("utf-8")
    # S310: the URL is not caller-supplied. It is an f-string over a fixed
    # https://api.github.com base, a repository id `assert_repository_numeric_id`
    # has already checked, and a 40-hex SHA `assert_candidate_sha` has already
    # checked. No `file:` or custom scheme can reach it.
    http = urllib.request.Request(url, data=body, method="POST")  # noqa: S310
    http.add_header("Authorization", f"Bearer {token}")
    http.add_header("Accept", "application/vnd.github+json")
    http.add_header("Content-Type", "application/json")
    try:
        with opener(http) as response:
            code = int(getattr(response, "status", 0) or 0)
    except urllib.error.HTTPError as exc:
        # The status code is reported; the body is not. A GitHub error body can
        # echo request headers, and the one header here is a bearer token.
        refuse(f"category=status_publish_failed context={request['context']} "
               f"http_status={exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        refuse(f"category=status_publish_transport_failed "
               f"context={request['context']} "
               f"exception_class={type(exc).__name__}")
    if code not in (200, 201):
        refuse(f"category=status_publish_unexpected_code http_status={code}")
    return {"context": request["context"], "state": request["state"],
            "candidate_head_sha": request["candidate_head_sha"],
            "description": description,
            "evidence_sha256": request.get("evidence_sha256"),
            "http_status": code}


def pending(*, candidate_head_sha: str, context: str, target_url: str,
            run_id, run_attempt, repository_numeric_id: int = REPOSITORY_NUMERIC_ID
            ) -> dict:
    """The `pending` a job must publish BEFORE it spends anything.

    Published first so that a run which dies mid-provider-call leaves a visible
    unfinished check rather than nothing at all. A pull request with no status is
    indistinguishable from one the panel never started on."""
    what = ("counting" if context == COUNT_STATUS else "panel review")
    return status_request(
        repository_numeric_id=repository_numeric_id,
        candidate_head_sha=candidate_head_sha, context=context, state="pending",
        description=f"mid-term single-repo {what} in progress (not write-separated)",
        target_url=target_url, run_id=run_id, run_attempt=run_attempt)


__all__ = [
    "STATES", "TERMINAL_STATES", "MAX_DESCRIPTION",
    "assert_publishable_context", "assert_candidate_sha", "status_request",
    "assert_no_pending_left", "publish", "pending",
    "COUNT_STATUS", "REVIEW_STATUS",
]
