"""Publish a trusted status onto the exact candidate head SHA.

EX3-R02, and it is the defect that would have made the whole trusted lane
decorative.

D1 and D2 run from the protected default ref via `workflow_dispatch`. A
dispatched run's own job checks attach to **the dispatched ref's commit — main**.
Branch protection on the candidate pull request needs a successful status on
**the candidate head SHA**. Those are different commits, so reserving the job
name `trusted-verifier-count` in a workflow on main satisfies nothing: the PR
would sit forever waiting for a check that lands somewhere else.

So the trusted status is published *explicitly*, as an API call, onto the exact
candidate SHA:

    pending   before any provider work begins
    success   only after signed evidence exists
    failure   when the review refuses
    error     when the run dies

`pending` first is not politeness. Without it, a run that crashes between
dispatch and completion leaves the PR with **no** trusted status at all, which
under some configurations reads as "not required yet" rather than "failed".
Publishing pending first means the only way to reach a green trusted context is
for something to deliberately turn it green.

What this module does NOT do: hold a token, make an HTTP request, or decide that
a review passed. It builds and validates the exact request, and refuses the
shapes that would let an untrusted input steer it. The transport belongs to the
protected runtime; the decision belongs to the evidence.
"""

from __future__ import annotations

import hashlib
import json
import re

from .errors import refuse
from .instants import utc_iso

#: The four states GitHub accepts on a commit status. `pending` is a start
#: state, never a terminal one — a run that only ever publishes pending has not
#: finished, and the PR must not be able to merge on it.
STATES = ("pending", "success", "failure", "error")
TERMINAL_STATES = ("success", "failure", "error")

#: Only these contexts may be published by this module. The inactive job and the
#: containment gates are excluded structurally: `assert_publishable_context`
#: refuses anything outside TRUSTED_STATUSES, so no amount of caller creativity
#: turns a no-key job into a trusted context.
_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")

MAX_DESCRIPTION = 140


def assert_publishable_context(context: str) -> str:
    """Only a reserved trusted context, and only from here."""
    from .statusnames import TRUSTED_STATUSES, class_of

    if context not in TRUSTED_STATUSES:
        klass = class_of(context)
        refuse(f"category=context_not_publishable context={context!r} "
               f"class={klass!r} permitted={list(TRUSTED_STATUSES)} — only the "
               "trusted contexts are published on a candidate head; publishing "
               "an inactive or containment name there would put a green check "
               "with no review behind it onto the pull request")
    return context


def assert_candidate_sha(value, *, field: str = "candidate_head_sha") -> str:
    """A 40-hex commit SHA, never a ref name.

    `refs/heads/whatever` and `HEAD` resolve differently at different moments,
    and the whole point of the published status is that it names one immutable
    commit. A textual ref would let the status land on a commit other than the
    one that was reviewed — including a newer one the author pushed while the
    review was running."""
    if not isinstance(value, str) or not _SHA1.match(value):
        refuse(f"category=candidate_sha_not_a_commit_sha field={field} — a ref "
               "name resolves differently at different moments; the published "
               "status must name the exact immutable commit that was reviewed")
    return value


def status_request(*, repository_numeric_id: int, candidate_head_sha: str,
                   context: str, state: str, description: str,
                   target_url: str, trusted_run_id, trusted_run_attempt,
                   evidence_sha256=None, expected_app_id=None,
                   published_at=None) -> dict:
    """Build one status publication, or refuse.

    Every field an operator or a later reader needs to tell this status apart
    from a plausible impostor: which repository by numeric id, which commit,
    which context, which trusted run and attempt produced it, and which evidence
    it is claiming. `external_id` binds run/attempt/evidence into one opaque
    string so the same context published twice from different runs is
    distinguishable."""
    from .identity import assert_repository_numeric_id

    assert_repository_numeric_id(repository_numeric_id)
    assert_candidate_sha(candidate_head_sha)
    assert_publishable_context(context)

    if state not in STATES:
        refuse(f"category=status_state_not_permitted state={state!r} "
               f"permitted={list(STATES)}")
    if not isinstance(description, str) or not description.strip():
        refuse("category=status_description_missing")
    if len(description) > MAX_DESCRIPTION:
        refuse(f"category=status_description_too_long len={len(description)} "
               f"max={MAX_DESCRIPTION} — GitHub truncates, and a truncated "
               "description is a claim whose end nobody reads")
    if not isinstance(target_url, str) or not target_url.startswith("https://"):
        refuse("category=status_target_url_not_https — the status links to the "
               "evidence; a non-https target is not a link a reader can trust")

    for label, value in (("trusted_run_id", trusted_run_id),
                         ("trusted_run_attempt", trusted_run_attempt)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            refuse(f"category=trusted_run_field_invalid field={label} — a "
                   "status that cannot name the run that produced it cannot be "
                   "traced back to one")

    terminal = state in TERMINAL_STATES
    if state == "success":
        if evidence_sha256 is None:
            refuse("category=success_without_evidence context="
                   f"{context} — a green trusted context asserts that a review "
                   "happened; publishing one with no evidence digest is the "
                   "self-signed claim this whole lane exists to prevent")
        if not _SHA256.match(str(evidence_sha256)):
            refuse("category=evidence_digest_malformed")
    if state != "success" and evidence_sha256 is not None:
        if not _SHA256.match(str(evidence_sha256)):
            refuse("category=evidence_digest_malformed")

    external = {
        "trusted_run_id": trusted_run_id,
        "trusted_run_attempt": trusted_run_attempt,
        "evidence_sha256": evidence_sha256,
        "context": context,
        "candidate_head_sha": candidate_head_sha,
    }
    blob = json.dumps(external, sort_keys=True, separators=(",", ":")).encode()
    request = {
        "repository_numeric_id": repository_numeric_id,
        "candidate_head_sha": candidate_head_sha,
        "context": context,
        "state": state,
        "description": description.strip(),
        "target_url": target_url,
        "external_id": hashlib.sha256(
            b"trusted-status-external-v1\x00" + blob).hexdigest(),
        "trusted_run_id": trusted_run_id,
        "trusted_run_attempt": trusted_run_attempt,
        "evidence_sha256": evidence_sha256,
        "expected_app_id": expected_app_id,
        "terminal": terminal,
    }
    if published_at is not None:
        request["published_at"] = utc_iso(published_at, field="published_at")
    request["honest_scope"] = (
        "a validated request, not a published status. Nothing here contacted "
        "GitHub; the transport belongs to the protected runtime and the "
        "decision belongs to the evidence")
    return request


def assert_pending_preceded_terminal(publications) -> dict:
    """A terminal status must be preceded by a pending one, on the same SHA.

    Without pending, a run that dies between dispatch and completion leaves the
    pull request with NO trusted status — which under some configurations reads
    as "not required yet" rather than "failed". Publishing pending first means
    the only way to a green trusted context is for something to deliberately
    turn it green.

    Also refuses a terminal publication whose SHA or context differs from the
    pending one it claims to conclude: that is a status about a different
    commit, arriving at exactly the moment a reader is least likely to check."""
    ordered = list(publications)
    if not ordered:
        refuse("category=no_status_published — a trusted run that publishes "
               "nothing leaves the candidate with no trusted context at all")
    first = ordered[0]
    if first.get("state") != "pending":
        refuse(f"category=first_publication_not_pending "
               f"state={first.get('state')!r} — pending must exist before any "
               "provider work, so a run that dies mid-flight leaves a visible "
               "unfinished status rather than none")
    seen_terminal = None
    for index, publication in enumerate(ordered[1:], start=1):
        if publication.get("candidate_head_sha") != first.get("candidate_head_sha"):
            refuse(f"category=publication_sha_changed index={index} — a "
                   "terminal status on a different commit than the pending one "
                   "is a status about code nobody reviewed under this run")
        if publication.get("context") != first.get("context"):
            refuse(f"category=publication_context_changed index={index}")
        if publication.get("state") == "pending":
            continue
        if seen_terminal is not None:
            refuse(f"category=multiple_terminal_publications index={index} — "
                   "two terminal statuses for one run means one of them is a "
                   "correction nobody recorded as one")
        seen_terminal = publication
    if seen_terminal is None:
        refuse("category=run_never_reached_a_terminal_status — the candidate is "
               "left pending forever, which blocks rather than fails, and hides "
               "that the run is over")
    return {
        "context": first["context"],
        "candidate_head_sha": first["candidate_head_sha"],
        "final_state": seen_terminal["state"],
        "publications": len(ordered),
    }


def assert_status_covers_head(publication: dict, *, observed_head_sha: str,
                              context: str) -> dict:
    """The reader's half: does this status apply to the head that is merging?

    A trusted review is about the commit it reviewed. If the author pushes
    again, the earlier status must not satisfy the requirement — and the check
    for that is comparing SHAs, not trusting that GitHub will forget."""
    assert_candidate_sha(observed_head_sha, field="observed_head_sha")
    if publication.get("context") != context:
        refuse(f"category=status_context_mismatch "
               f"published={publication.get('context')!r} required={context!r}")
    if publication.get("candidate_head_sha") != observed_head_sha:
        refuse(f"category=status_is_for_an_earlier_head "
               f"published_for={publication.get('candidate_head_sha')} "
               f"current_head={observed_head_sha} — the review describes a "
               "commit that is no longer what would merge; a new push must "
               "invalidate it")
    if publication.get("state") != "success":
        refuse(f"category=status_not_success state={publication.get('state')!r}")
    if not publication.get("evidence_sha256"):
        refuse("category=success_status_without_evidence")
    return {"satisfied": True, "context": context,
            "candidate_head_sha": observed_head_sha,
            "evidence_sha256": publication["evidence_sha256"],
            "honest_scope": "the status names this head, this context and an "
                            "evidence digest; whether that evidence is genuine "
                            "is the signature's question, not this one"}


def publish(request: dict, **_kwargs):
    """Refuses. The transport needs a token this branch must not hold."""
    if not isinstance(request, dict) or "external_id" not in request:
        refuse("category=status_request_not_validated — build it with "
               "status_request() so the refusals run before anything is sent")
    refuse("category=status_publication_not_available_in_D0 — the request "
           "shape, its refusals and the pending/terminal ordering are defined "
           "and tested; publishing needs an installation token with Checks or "
           "Statuses write, held by the approved integration and never by a "
           "reviewed branch")
