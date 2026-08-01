"""What the lane can say about its OWN deployment, and what it cannot.

Prerequisite 15 is `approve_bootstrap_branch`: the operator approves the branch,
the head and the policy that the trusted lane is deployed from. EX5-R21 requires
that approval to be compared against the deployment actually running — otherwise
an operator approves bootstrap A while the run executes bootstrap B, and both
halves look right to whichever check reads them.

So the lane has to be able to state its own deployment. Three parts, and they
are not equally trustworthy:

* **branch and head** are told to it by the runner. A process cannot verify
  which ref dispatched it; `github.ref` and `github.sha` come from the platform.
  They are recorded as observations, and the operator's approval is what makes
  them meaningful — the comparison catches a run from the wrong branch only
  because the operator named the right one.
* **the policy digest** IS computed here, from the deployed workflow files on
  disk plus the implemented phase. That one is a real self-observation: a
  workflow edited after approval changes the digest, and the run refuses.

The distinction is kept in the record rather than smoothed over, because a
reader deciding how much the comparison is worth needs to know which half the
platform supplied.
"""

from __future__ import annotations

import hashlib
import json
import os

from .errors import refuse

#: The deployed trusted workflow files whose content the policy digest covers.
#: Named rather than globbed: a glob makes "the policy" whatever happens to be
#: in the directory, so adding a file would silently change what was approved,
#: and REMOVING one would silently narrow it.
BOOTSTRAP_POLICY_FILES = (
    "d0-trusted-lane-containment.yml",
    "d1-trusted-count.yml",
    "d2-trusted-generation.yml",
)

#: Templates count too. Before activation the deployed name is the template
#: name, and a policy digest that ignored templates would be blind to exactly
#: the period in which the operator is deciding whether to activate.
BOOTSTRAP_POLICY_ALTERNATES = {
    "d1-trusted-count.yml": "d1-trusted-count.yml.template",
    "d2-trusted-generation.yml": "d2-trusted-generation.yml.template",
}


def policy_digest(*, workflow_dir: str, implemented_phase: str) -> dict:
    """Digest the deployed trusted workflow policy, or refuse.

    Every named file must resolve to exactly one readable path — the deployed
    name or its template, not both and not neither. Both would mean two
    definitions of the same job are on disk and nothing says which runs;
    neither means the policy being approved does not exist."""
    if not isinstance(workflow_dir, str) or not os.path.isdir(workflow_dir):
        refuse("category=bootstrap_workflow_directory_absent — the path is not "
               "reported; it carries the runner layout")
    entries = {}
    for name in BOOTSTRAP_POLICY_FILES:
        candidates = [name]
        alternate = BOOTSTRAP_POLICY_ALTERNATES.get(name)
        if alternate:
            candidates.append(alternate)
        present = [c for c in candidates
                   if os.path.isfile(os.path.join(workflow_dir, c))]
        if not present:
            refuse(f"category=bootstrap_policy_file_absent file={name} — the "
                   "policy an operator approves must exist to be approved")
        if len(present) > 1:
            refuse(f"category=bootstrap_policy_file_ambiguous file={name} "
                   f"found={sorted(present)} — the deployed workflow and its "
                   "template are both on disk, and nothing here says which one "
                   "runs")
        with open(os.path.join(workflow_dir, present[0]), "rb") as handle:
            entries[name] = {
                "deployed_as": present[0],
                "content_sha256": hashlib.sha256(handle.read()).hexdigest(),
            }
    payload = {"implemented_phase": implemented_phase,
               "files": dict(sorted(entries.items()))}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {"policy_sha256": hashlib.sha256(
                b"trusted-bootstrap-policy-v1\x00" + blob).hexdigest(),
            "files": payload["files"],
            "implemented_phase": implemented_phase}


def observe(*, ref: str, head_sha: str, workflow_dir: str,
            implemented_phase: str) -> dict:
    """The deployment this run is actually running from.

    `ref` arrives as `refs/heads/<branch>` from the platform and is reduced to
    the branch name, because that is what an operator approves and what
    `docs/TRUSTED_LANE_OPERATOR_ACTIONS.md` asks them for. A ref that is not a
    branch — a tag, a bare SHA — is refused rather than reduced: the trusted
    lane is dispatched from a protected BRANCH, and a tag can be moved."""
    from .identity import assert_commit_sha

    if not isinstance(ref, str) or not ref:
        refuse("category=bootstrap_ref_missing")
    if not ref.startswith("refs/heads/"):
        refuse(f"category=bootstrap_ref_not_a_branch ref={ref!r} — the trusted "
               "lane is dispatched from a protected branch; a tag can be moved "
               "to a different commit without changing its name")
    branch = ref[len("refs/heads/"):]
    if not branch:
        refuse("category=bootstrap_branch_empty")
    assert_commit_sha(head_sha, field="bootstrap_head_sha")
    policy = policy_digest(workflow_dir=workflow_dir,
                           implemented_phase=implemented_phase)
    return {
        "branch": branch,
        "head_sha": head_sha,
        "policy_sha256": policy["policy_sha256"],
        "policy_files": policy["files"],
        "implemented_phase": implemented_phase,
        "honest_scope": (
            "the branch and head were supplied by the platform and cannot be "
            "verified from inside the process; the policy digest was computed "
            "here from the deployed workflow files, so an edit after approval "
            "changes it"),
    }
