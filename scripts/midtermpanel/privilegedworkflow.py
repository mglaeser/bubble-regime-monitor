"""Static policy over the ONE privileged workflow that holds the panel secret.

## The hazard this file exists for

`livepolicy.py` enforces the rule that made V-TRUST a defect:

    a workflow that runs code a pull request can control must hold no
    provider credential.

It implements that by refusing any secret in a `pull_request`-triggered
workflow, and its refusal message prescribes the remedy verbatim: *"Move it to a
workflow_run job that checks out trusted code and takes the candidate as data."*

This package takes that advice. Which means `livepolicy` now passes the new
workflow trivially — it is not PR-controlled — and stops being the control. The
privileged workflow introduces a DIFFERENT hazard class that `livepolicy` has no
concept of, because until now the repository had no privileged workflow:

    a `workflow_run` workflow runs the DEFAULT BRANCH definition with full
    repository secret scope, while the event that triggered it was produced by
    a pull request. Everything reachable from that run — the PR head tree, the
    triggering run's artifacts, the cache it wrote — is candidate-controlled
    content arriving in a job that holds a provider key.

`workflow_run` is safe only because of what the workflow does NOT do. That is
precisely the kind of safety property that decays silently, so it is asserted
here rather than described in the workflow's own comments.

## The one property everything else serves

**The candidate's tree is never materialised in the workspace.**

Not "we do not run the candidate's tests" — that is a consequence. The property
is that the bytes never become files that anything could execute, import, source,
or install. Every check below is a way that property has been broken in real
workflows:

  * `actions/checkout` with a `ref:` — the direct form;
  * `git checkout`/`git switch`/`git merge` inside a `run:` — the same thing
    spelled in shell, which a checkout-shaped check never sees;
  * `actions/download-artifact` — the triggering run wrote those artifacts, and
    a zip is a tree;
  * `actions/cache` — the PR's CI populated it, and a restored cache is a tree
    someone else chose the contents of;
  * a local `uses: ./…` action — a directory in the workspace, which is only
    trustworthy while the workspace is;
  * job-level `uses:` — a whole reusable workflow that step-level pinning never
    walks.

Fetching candidate commits as inert git OBJECTS is allowed and is the point:
`git fetch --no-checkout` moves bytes into `.git` where nothing runs them. The
distinction between an object and a worktree is the entire safety argument, so
the checks are written to permit the first and refuse the second rather than to
pattern-match on the word "candidate".

## What is deliberately NOT refused

The candidate head SHA. It has to flow — into API calls, into `git fetch`, into
the status the panel publishes on that exact commit, into the evidence. A rule
that banned the string would ban the architecture.

So the rule is POSITIONAL, not textual: a candidate SHA may appear anywhere that
treats it as data, and nowhere that treats it as a selector for code. That is why
`assert_no_candidate_checkout` refuses `ref:` outright instead of trying to
decide whether a particular expression resolves to something dangerous — the
laundering game (`env.`, `steps.`, `needs.`) was already lost once in
`workflowfile.CALLER_CONTROLLED_PREFIXES`, and the lesson recorded there is to
refuse the shape rather than to inspect the value.
"""

from __future__ import annotations

import os

import yaml

from . import (
    CI_WORKFLOW_NAME,
    SECRET_NAME,
    WORKFLOW_FILENAME,
    WORKFLOW_NAME,
)
from .errors import refuse

#: Triggers the privileged workflow may declare. Anything else is refused.
#:
#: An allowlist, not a denylist of the scary ones. `issue_comment`,
#: `pull_request_target` and `repository_dispatch` are all obviously wrong here,
#: and enumerating them would leave whatever GitHub ships next quietly permitted.
PERMITTED_TRIGGERS = ("workflow_run", "workflow_dispatch")

#: Jobs permitted to reference the provider secret.
#:
#: `preflight` is excluded on purpose and it is the important exclusion: it is
#: the job that decides whether the run may proceed at all, and a decision made
#: in a process holding a credential is a decision whose failure modes include
#: leaking it. `finalize` is excluded because it runs on every path including
#: failure, which is the worst place for a key to be in scope.
SECRET_BEARING_JOBS = ("count", "panel")

#: Actions that hand this job content produced by the triggering (candidate) run.
FORBIDDEN_ACTION_PREFIXES = (
    "actions/download-artifact",
    "actions/cache",
    "actions/upload-pages-artifact",
    "dawidd6/action-download-artifact",
)

#: Shell that materialises a tree, in a job whose whole safety argument is that
#: no tree is materialised. `git fetch` is absent deliberately — it is allowed.
TREE_MATERIALISING_COMMANDS = (
    "git checkout",
    "git switch",
    "git merge",
    "git cherry-pick",
    "git rebase",
    "git apply",
    "git stash",
    "git worktree add",
    "gh run download",
    "gh pr checkout",
)

#: Executing, importing or installing from a candidate path.
CANDIDATE_EXECUTION_MARKERS = (
    "pip install -e", "pip install .", "pip install -r",
    "setup.py", "python -m verifier", "scripts/verifier",
    "npm install", "npm ci", "yarn install", "make ",
    "bash candidate", "source candidate",
)


def _read(path: str) -> dict:
    """Parse the privileged workflow, refusing anything unparseable.

    Refused rather than skipped, for the reason `livepolicy._read` records: "could
    not read it, so it passed" is how a check reports success for the one file it
    understood least — and this is the file that holds the key."""
    try:
        with open(path, "rb") as handle:
            document = yaml.safe_load(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        refuse(f"category=privileged_workflow_unreadable path={path} "
               f"exception_class={type(exc).__name__}")
    if not isinstance(document, dict):
        refuse(f"category=privileged_workflow_not_a_mapping path={path}")
    return document


def _on_block(document: dict) -> dict:
    """The trigger block, surviving YAML 1.1 folding `on:` to the boolean True.

    Same trap `livepolicy.trigger_names` documents: a validator that reads only
    `document["on"]` sees nothing on every real workflow and passes everything."""
    for key in ("on", True):
        if key in document:
            block = document[key]
            if not isinstance(block, dict):
                refuse("category=privileged_workflow_on_block_not_mapping")
            return block
    refuse("category=privileged_workflow_has_no_on_block")


def _steps(document: dict):
    """Every (job_id, index, step) in declaration order."""
    for job_id, job in (document.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            yield str(job_id), index, (step or {})


def assert_triggers(document: dict) -> dict:
    """Only `workflow_run` on completed `ci`, plus optional manual dispatch.

    The `workflows:` filter is checked, not just the trigger name. A
    `workflow_run` with no filter fires on the completion of EVERY workflow in
    the repository, including one a pull request just added — which would let a
    candidate choose when the privileged run starts and what event payload it
    reads."""
    block = _on_block(document)
    declared = sorted(str(k) for k in block)
    forbidden = [t for t in declared if t not in PERMITTED_TRIGGERS]
    if forbidden:
        refuse(f"category=privileged_workflow_trigger_not_permitted "
               f"triggers={forbidden} permitted={list(PERMITTED_TRIGGERS)} — a "
               "privileged workflow holding a repository secret may only be "
               "started by the completion of trusted CI or by a human dispatch")
    if "workflow_run" not in declared:
        refuse("category=privileged_workflow_has_no_workflow_run_trigger — "
               "without it the panel never runs automatically, which is the "
               "entire feature")

    run_block = block.get("workflow_run") or {}
    if not isinstance(run_block, dict):
        refuse("category=workflow_run_block_not_mapping")
    workflows = run_block.get("workflows")
    if not isinstance(workflows, list) or not workflows:
        refuse("category=workflow_run_does_not_filter_by_workflow — an "
               "unfiltered workflow_run fires on the completion of every "
               "workflow in the repository, including one a pull request just "
               "added; the candidate would choose when the privileged run "
               "starts")
    if [str(w) for w in workflows] != [CI_WORKFLOW_NAME]:
        refuse(f"category=workflow_run_filters_wrong_workflow "
               f"workflows={[str(w) for w in workflows]} "
               f"expected={[CI_WORKFLOW_NAME]}")
    types = run_block.get("types")
    if types is not None and [str(t) for t in types] != ["completed"]:
        refuse(f"category=workflow_run_types_not_completed types={types}")
    return {"triggers": declared, "workflow_run_filter": [str(w) for w in workflows]}


def assert_no_candidate_checkout(document: dict) -> dict:
    """Every checkout takes the default branch, with no persisted credential.

    `ref:` is refused OUTRIGHT — not inspected, not expression-matched. For a
    `workflow_run` event the default checkout is the default-branch commit the
    workflow definition itself came from, which is exactly what a privileged job
    wants; naming any ref can only move it away from that. Refusing the key
    rather than judging its value is the lesson `workflowfile` records after its
    prefix denylist was walked around with `env.` and `steps.`.

    A run-time check re-verifies the checked-out commit, because a static rule
    about a YAML key is a statement about the file, not about the runner."""
    checked = []
    for job_id, index, step in _steps(document):
        uses = str(step.get("uses") or "")
        if "actions/checkout" not in uses:
            continue
        where = f"{job_id}.steps[{index}]"
        with_block = step.get("with") or {}
        if with_block.get("persist-credentials") is not False:
            refuse(f"category=privileged_checkout_persists_credentials "
                   f"where={where} — a checkout that leaves a usable token in "
                   ".git/config hands write access to anything that later runs "
                   "in a workspace that also holds a provider key")
        if "repository" in with_block:
            refuse(f"category=privileged_checkout_selects_repository "
                   f"where={where} — the privileged job checks out its own "
                   "default branch only")
        if "ref" in with_block:
            refuse(f"category=privileged_checkout_names_a_ref where={where} "
                   f"ref={str(with_block.get('ref'))[:60]!r} — for a "
                   "workflow_run event the default checkout is already the "
                   "default-branch commit this workflow came from; naming a ref "
                   "can only move it toward candidate-controlled code, and "
                   "whether a particular expression is safe is not a question "
                   "this policy is willing to answer")
        checked.append(where)
    if not checked:
        refuse("category=privileged_workflow_has_no_checkout_step — a workflow "
               "that never checks out cannot have been checked for checking out "
               "the wrong thing; this guard exists so the check cannot silently "
               "cover nothing")
    return {"checkout_steps": checked}


def assert_no_candidate_artifacts_or_cache(document: dict) -> dict:
    """The triggering run's artifacts and cache are candidate-authored bytes.

    The classic `workflow_run` escalation: privileged workflow downloads the
    artifact the PR's CI uploaded, unzips it, and runs or trusts what is inside.
    A zip is a tree, and a restored cache is a tree whose contents someone else
    chose. Both are refused by action name, and `gh run download` is refused in
    `assert_no_tree_materialisation` because it is the same act in shell."""
    offenders = []
    for job_id, index, step in _steps(document):
        uses = str(step.get("uses") or "")
        for prefix in FORBIDDEN_ACTION_PREFIXES:
            if uses.startswith(prefix):
                offenders.append(f"{job_id}.steps[{index}] uses={uses}")
    if offenders:
        refuse(f"category=privileged_workflow_consumes_candidate_artifacts "
               f"found={offenders} — the triggering run is the candidate's CI; "
               "its artifacts and its cache are content a pull request authored, "
               "and unpacking either of them into a job that holds a provider "
               "key is the canonical workflow_run escalation")
    return {"artifact_or_cache_steps": 0}


def assert_no_local_or_unpinned_actions(document: dict) -> dict:
    """No `./` action, no docker action, no job-level reusable workflow.

    A local action is a directory in the workspace, and it is only as trustworthy
    as the workspace — which is the thing the rest of this file is busy
    constraining. Job-level `uses:` is worse: it pulls in a whole file of
    someone else's steps that step-level pin checking never walks, the same
    bypass `workflowfile.FORBIDDEN_JOB_KEYS` refuses for the trusted lane."""
    offenders = []
    for job_id, job in (document.get("jobs") or {}).items():
        job = job or {}
        for key in ("uses", "secrets", "container", "services"):
            if key in job:
                offenders.append(f"jobs.{job_id}.{key}")
    for job_id, index, step in _steps(document):
        uses = str(step.get("uses") or "")
        if not uses:
            continue
        if uses.startswith("./") or uses.startswith(".\\") or uses.startswith("docker://"):
            offenders.append(f"{job_id}.steps[{index}] uses={uses}")
    if offenders:
        refuse(f"category=privileged_workflow_uses_untrusted_action "
               f"found={offenders} — a local `./` action is a directory in the "
               "workspace, a docker action is an unpinned image, and job-level "
               "`uses:`/`secrets:`/`container:`/`services:` each pull in code or "
               "credentials that step-level pinning never inspects")
    return {"untrusted_action_uses": 0}


def assert_no_tree_materialisation(document: dict) -> dict:
    """No `run:` may turn candidate objects into candidate files.

    This is the check that makes `git fetch --no-checkout` meaningful. Fetching
    is allowed — it moves bytes into `.git`, where nothing executes them. The
    moment a step runs `git checkout`, `git merge`, `git apply` or
    `gh run download`, those bytes become a worktree, and every other guarantee
    in this module is about a workspace that no longer exists.

    Deliberately matches on the COMMAND, not on whether a candidate SHA appears
    near it. `git checkout $SHA` and `git checkout FETCH_HEAD` are the same act,
    and only one of them mentions anything candidate-shaped."""
    offenders = []
    for job_id, index, step in _steps(document):
        script = str(step.get("run") or "")
        hits = [c for c in TREE_MATERIALISING_COMMANDS if c in script]
        if hits:
            offenders.append(f"{job_id}.steps[{index}]:{hits}")
    if offenders:
        refuse(f"category=privileged_workflow_materialises_a_tree "
               f"found={offenders} — the candidate is fetched as inert git "
               "objects and must stay that way; a command that produces a "
               "worktree turns reviewed data back into runnable code inside a "
               "job that holds a provider key")
    return {"tree_materialising_steps": 0}


def assert_no_candidate_execution(document: dict) -> dict:
    """No `run:` may execute, import or install from candidate content."""
    offenders = []
    for job_id, index, step in _steps(document):
        script = str(step.get("run") or "")
        hits = [m for m in CANDIDATE_EXECUTION_MARKERS if m in script]
        if hits:
            offenders.append(f"{job_id}.steps[{index}]:{hits}")
    if offenders:
        refuse(f"category=privileged_workflow_executes_candidate_content "
               f"found={offenders}")
    return {"candidate_execution_steps": 0}


def _expression_strings(node):
    """Every string in a nested structure (keys included)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                yield key
            yield from _expression_strings(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _expression_strings(item)


def _names_secret(node) -> bool:
    """Does this subtree read the `secrets` context, by any syntax?

    Reuses the trusted lane's scanner rather than substring-matching
    `secrets.` — `secrets['NAME']` and `toJSON(secrets)` contain no dot and were
    the exact bug that scanner was rewritten to catch."""
    from trustedlane.workflowfile import _secret_references
    return bool(_secret_references(node))


def assert_secret_is_scoped(document: dict) -> dict:
    """The provider secret appears only in the jobs allowed to hold it.

    Workflow-level `env:` is checked first and separately: it is inherited by
    EVERY job, so a secret there is a secret in `preflight` and in `finalize`,
    and neither of those jobs is ever supposed to be able to spend money.

    The named secret is also checked: this workflow may reference exactly one,
    and a second credential arriving here would widen the blast radius of the
    same accepted residual without anyone re-accepting it."""
    if _names_secret(document.get("env") or {}):
        refuse("category=privileged_workflow_level_secret — workflow-level "
               "`env:` is inherited by every job including preflight and "
               "finalize; scope a secret to the job that spends it")

    holders = []
    for job_id, job in (document.get("jobs") or {}).items():
        if _names_secret(job or {}):
            holders.append(str(job_id))
    unexpected = sorted(set(holders) - set(SECRET_BEARING_JOBS))
    if unexpected:
        refuse(f"category=privileged_workflow_secret_in_unexpected_job "
               f"jobs={unexpected} permitted={list(SECRET_BEARING_JOBS)} — "
               "preflight decides whether the run may proceed and finalize runs "
               "on every failure path; neither should have a credential in "
               "scope when it does so")

    referenced = set()
    for text in _expression_strings(document):
        if SECRET_NAME in text:
            referenced.add(SECRET_NAME)
    others = []
    for text in _expression_strings(document):
        if "secrets." not in text and "secrets[" not in text:
            continue
        for token in text.replace("[", ".").replace("]", ".").replace("'", ".").split("."):
            token = token.strip(" }{$")
            if token.isupper() and len(token) > 3 and token != SECRET_NAME:
                others.append(token)
    others = sorted({o for o in others if o not in ("GITHUB_TOKEN",)})
    if others:
        refuse(f"category=privileged_workflow_names_another_secret "
               f"secrets={others} expected={SECRET_NAME!r} — the accepted "
               "residual covers one provider key; a second credential here "
               "widens it without anyone having accepted the wider version")
    return {"secret_bearing_jobs": sorted(holders),
            "named_secrets": sorted(referenced)}


#: The exact permission set the plan grants. Written as a mapping rather than a
#: maximum, because "no more than these" and "exactly these" differ: a workflow
#: that silently drops `statuses: write` would still pass a subset check and then
#: fail at run time, having already spent the provider budget.
REQUIRED_PERMISSIONS = {
    "contents": "read",
    "actions": "read",
    "pull-requests": "read",
    "statuses": "write",
}


def assert_permissions(document: dict) -> dict:
    """Exactly four scopes, and `statuses: write` is the only write.

    An unset block inherits the repository default, which on many repositories
    is write-all — so absence is refused rather than defaulted."""
    permissions = document.get("permissions")
    if permissions is None:
        refuse("category=privileged_workflow_permissions_unset — an unset "
               "permissions block inherits the repository default, which may "
               "grant every write scope to a job that holds a provider key")
    if not isinstance(permissions, dict):
        refuse("category=privileged_workflow_permissions_not_mapping")
    got = {str(k): str(v) for k, v in permissions.items()}
    if got != REQUIRED_PERMISSIONS:
        refuse(f"category=privileged_workflow_permissions_mismatch got={got} "
               f"expected={REQUIRED_PERMISSIONS} — `statuses: write` is the only "
               "write scope this panel needs; anything more is a capability "
               "nobody asked for, and anything less fails after the money is "
               "already spent")
    for job_id, job in (document.get("jobs") or {}).items():
        job_permissions = (job or {}).get("permissions")
        if job_permissions is None:
            continue
        writes = sorted(f"{k}={v}" for k, v in job_permissions.items()
                        if v not in ("read", "none")
                        and not (k == "statuses" and v == "write"))
        if writes:
            refuse(f"category=privileged_job_permissions_grant_write "
                   f"job={job_id} scopes={writes}")
    return {"permissions": got}


def validate(*, root: str = ".") -> dict:
    """Every rule over the privileged workflow, as one record.

    Ordered most-serious-first for the same reason `livepolicy.validate_live_workflows`
    is: a refusal stops the walk, so whichever fires is the one the operator
    reads. Credential scope and candidate execution come before shape and
    pinning, because only the first two can hand a key or a shell to a pull
    request."""
    from trustedlane import statusnames
    from trustedlane.workflowfile import assert_actions_pinned

    path = os.path.join(root, ".github", "workflows", WORKFLOW_FILENAME)
    if not os.path.exists(path):
        refuse(f"category=privileged_workflow_missing path={path}")
    document = _read(path)

    declared_name = str(document.get("name") or "")
    if declared_name != WORKFLOW_NAME:
        refuse(f"category=privileged_workflow_name_mismatch "
               f"declared={declared_name!r} expected={WORKFLOW_NAME!r} — the "
               "declared name is what `workflow_run` filters and what the "
               "status policy registers; a mismatch means one of them is "
               "watching a workflow that does not exist")

    return {
        "path": path,
        "name": declared_name,
        # 1. can a pull request reach the key, or a shell?
        **assert_secret_is_scoped(document),
        **assert_no_candidate_checkout(document),
        **assert_no_tree_materialisation(document),
        **assert_no_candidate_artifacts_or_cache(document),
        **assert_no_candidate_execution(document),
        **assert_no_local_or_unpinned_actions(document),
        # 2. is the run started by something trusted?
        **assert_triggers(document),
        # 3. capability surface
        **assert_permissions(document),
        # 4. supply chain
        **assert_actions_pinned(document, name=WORKFLOW_FILENAME),
        # 5. does every published check name mean what an operator would think?
        **statusnames.assert_workflow_statuses_are_registered(
            document, name=WORKFLOW_FILENAME),
        "honest_scope": (
            "this validates the privileged workflow's SHAPE. It says nothing "
            "about what the referenced scripts do once they run, and nothing "
            "about the accepted residual that a pull request's OWN workflows "
            "can reference the repository secret"),
    }
