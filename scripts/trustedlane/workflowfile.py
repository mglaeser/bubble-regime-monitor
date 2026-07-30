"""Validate the proposed protected workflow as a FILE, not as a convention.

`workflowpolicy.py` states the rules over plain values. This module applies them
to the actual YAML that would be deployed, so a future edit that reintroduces
`pull_request_target`, adds a `ref` input, drops `persist-credentials: false`,
or lets the no-secret job reach a secret fails a test rather than a code review.

Two properties are worth naming because they are easy to lose:

* **The phases are three files, not one file with a `phase` input.** One file
  is one edit away from activating the phase it was not approved for. D0 is a
  real `.yml` and is the deployment target; D1 and D2 are `.yml.template`, and
  the extension is the containment — GitHub reads only `.yml`/`.yaml` under
  `.github/workflows/`, so copying the directory there activates the no-secret
  job and nothing else.
* **`on` is a YAML 1.1 boolean.** `yaml.safe_load` turns the `on:` key into
  `True`. A validator that looks only for the string key silently validates
  nothing, which is the failure mode where the test passes and the policy is
  unenforced.
"""

from __future__ import annotations

import hashlib
import os
import re

import yaml

from . import actionpolicy
from .errors import refuse
from .workflowpolicy import (
    assert_no_ref_selection,
    assert_trigger_permitted,
)

#: Where the lane's workflow definitions live, relative to the repository root.
WORKFLOW_DIR = os.path.join("scripts", "trustedlane", "workflow")

#: Directory that makes a workflow live.
LIVE_WORKFLOW_DIR = os.path.join(".github", "workflows")

#: The three phases, split into three files, because one file with a `phase`
#: input is one edit away from activating the phase it was not approved for.
#:
#: D0 is a real `.yml` and is the deployment target. D1 and D2 are
#: `.yml.template`, and the extension IS the containment: GitHub reads only
#: `.yml`/`.yaml` under `.github/workflows/`, so copying this directory there
#: activates the no-secret job and nothing else. Renaming a template is then a
#: deliberate, reviewable act rather than a side effect.
D0_FILE = "d0-trusted-lane-containment.yml"
D1_FILE = "d1-trusted-count.yml.template"
D2_FILE = "d2-trusted-generation.yml.template"

#: filename -> policy for that file.
#:
#: `secrets_permitted=False` is the whole difference between D0 and the rest:
#: the containment workflow may not so much as name a secret, so there is
#: nothing for a future edit to widen.
TRUSTED_WORKFLOWS = {
    D0_FILE: {
        "phase": "D0",
        "deployable_now": True,
        "secrets_permitted": False,
        "environments_permitted": False,
    },
    D1_FILE: {
        "phase": "D1",
        "deployable_now": False,
        "secrets_permitted": True,
        "environments_permitted": True,
    },
    D2_FILE: {
        "phase": "D2",
        "deployable_now": False,
        "secrets_permitted": True,
        "environments_permitted": True,
    },
}

#: The job that must gate every credential-bearing job.
CONTAINMENT_JOB = "d0-containment"

#: Workflow inputs that would let a caller choose the candidate SOURCE rather
#: than merely name a commit in it. The source is fixed in trusted policy.
SOURCE_SELECTING_INPUTS = ("repository", "remote", "remote_url", "url",
                           "owner", "org", "fork", "source_repository")

#: Expressions that let a caller choose which code runs.
CALLER_CONTROLLED_PREFIXES = ("inputs.", "github.event.",
                              "github.head_ref", "needs.")

_CHECKOUT_MARKER = "actions/checkout"


def _on_block(document: dict) -> dict:
    """Return the trigger block whether PyYAML kept `on` or folded it to True."""
    for key in ("on", True):
        if key in document:
            block = document[key]
            if not isinstance(block, dict):
                refuse("category=workflow_on_block_not_mapping")
            return block
    refuse("category=workflow_has_no_on_block")


def _strings(node):
    """Every string anywhere in a nested structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                yield key
            yield from _strings(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _strings(item)


#: A GitHub Actions expression, and the `secrets` context as a bare identifier
#: inside one.
#:
#: Substring-matching `secrets.` was the bug. GitHub documents index syntax —
#: `secrets['NAME']` — and `toJSON(secrets)`, which dumps EVERY secret into one
#: value. Neither contains `secrets.`, so both were invisible: a D0 job could
#: name a secret, and worse, a D1/D2 job could reach one without being
#: recognised as credential-bearing at all — escaping the environment gate and
#: the containment gate together. Found by the bootstrap PR review.
#:
#: Matching the identifier inside `${{ … }}` covers dot, index, function-call
#: and any future syntax, because they all have to name the context.
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_SECRETS_CONTEXT = re.compile(r"(?<![A-Za-z0-9_.])secrets(?![A-Za-z0-9_])")


def _secret_references(node) -> list:
    """Every string in `node` that reads the `secrets` context, by any syntax."""
    found = []
    for text in _strings(node):
        for expression in _EXPRESSION.findall(text):
            if _SECRETS_CONTEXT.search(expression):
                found.append(text)
                break
    return found


def _references_secret(node) -> bool:
    return bool(_secret_references(node))


def load_workflow(name: str = D0_FILE, *, root: str = ".") -> dict:
    """Read and parse one trusted-lane workflow definition."""
    if name not in TRUSTED_WORKFLOWS:
        refuse(f"category=unknown_trusted_workflow name={name!r}")
    path = os.path.join(root, WORKFLOW_DIR, name)
    if not os.path.exists(path):
        refuse(f"category=trusted_lane_workflow_missing name={name}")
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        refuse(f"category=trusted_lane_workflow_unparseable name={name} "
               f"exception_class={type(exc).__name__}")
    if not isinstance(document, dict):
        refuse(f"category=trusted_lane_workflow_not_mapping name={name}")
    return {"name": name, "document": document, "raw": raw,
            "policy": dict(TRUSTED_WORKFLOWS[name]),
            "sha256": hashlib.sha256(raw).hexdigest()}


def assert_no_template_is_live(*, root: str = ".") -> dict:
    """D1 and D2 must never appear in the live workflows directory.

    Checked by the *deployable* name, not the template name: the hazard is
    someone renaming `d1-trusted-count.yml.template` to `.yml` and dropping it
    in, which is precisely the step that must stay deliberate. D0 is allowed
    there — it is the deployment target — so this is not a blanket ban on the
    directory."""
    live_dir = os.path.join(root, LIVE_WORKFLOW_DIR)
    live = []
    for name, policy in TRUSTED_WORKFLOWS.items():
        if policy["deployable_now"]:
            continue
        deployed_name = name[:-len(".template")]
        if os.path.exists(os.path.join(live_dir, deployed_name)):
            live.append(deployed_name)
    if live:
        refuse(f"category=undeployable_phase_is_live files={live} — D1/D2 "
               "activation is a separate approval (operator prerequisites and "
               "a protected environment), not a file rename")
    return {"undeployable_phases_live": 0}


def assert_actions_pinned(document: dict, *, name: str = "") -> dict:
    """Every `uses:` must be an approved immutable commit pin."""
    pinned = []
    for job_name, job in (document.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            uses = (step or {}).get("uses")
            if uses is None:
                continue
            pinned.append(actionpolicy.assert_pinned(
                str(uses), where=f"{name}:{job_name}.steps[{index}]"))
    if not pinned:
        refuse(f"category=workflow_uses_no_actions name={name} — a workflow "
               "with no `uses:` cannot have been checked for pinning; this is "
               "a guard against the check silently covering nothing")
    return {"pinned_actions": pinned}


def assert_no_source_selection(document: dict, *, name: str = "") -> dict:
    """A caller may name a COMMIT, never a repository.

    The distinction is the whole point. A SHA is inert data the lane verifies
    against what it actually fetched; a repository name is a redirection, and
    it silently invalidates the numeric-id check downstream — that check would
    then be verifying the id of whatever server the caller chose."""
    block = _on_block(document)
    dispatch = block.get("workflow_dispatch") or {}
    inputs = (dispatch.get("inputs") or {}) if isinstance(dispatch, dict) else {}
    selecting = sorted(k for k in inputs if k in SOURCE_SELECTING_INPUTS)
    if selecting:
        refuse(f"category=workflow_input_selects_source name={name} "
               f"inputs={selecting} — the candidate repository is fixed in "
               "trusted policy; a caller that can name it can redirect the "
               "review")
    for job_name, job in (document.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            with_block = (step or {}).get("with") or {}
            if "repository" in with_block:
                refuse(f"category=checkout_selects_repository name={name} "
                       f"where={job_name}.steps[{index}]")
    return {"source_selection": False}


def assert_no_candidate_execution(document: dict, *, name: str = "") -> dict:
    """No step may run something out of the candidate package.

    The lane fetches the candidate as inert data. A `run:` that invokes
    `scripts/verifier/...`, or pip-installs the checked-out tree, turns that
    data back into code — with whatever credential the job holds."""
    markers = ("scripts/verifier", "pip install -e", "pip install .",
               "setup.py", "python -m verifier", "candidate/")
    offenders = []
    for job_name, job in (document.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            script = str((step or {}).get("run") or "")
            hits = [m for m in markers if m in script]
            if hits:
                offenders.append(f"{job_name}.steps[{index}]:{hits}")
    if offenders:
        refuse(f"category=workflow_executes_candidate_content name={name} "
               f"steps={offenders}")
    return {"candidate_execution": False}


def assert_triggers(document: dict) -> dict:
    block = _on_block(document)
    triggers = sorted(str(key) for key in block)
    for trigger in triggers:
        assert_trigger_permitted(trigger)
    dispatch = block.get("workflow_dispatch") or {}
    inputs = (dispatch.get("inputs") or {}) if isinstance(dispatch, dict) else {}
    assert_no_ref_selection(inputs)
    return {"triggers": triggers, "dispatch_inputs": sorted(inputs)}


def assert_permissions_read_only(document: dict) -> dict:
    """Workflow-level and job-level permissions must grant no write scope."""
    seen = []
    scopes = [("workflow", document.get("permissions"))]
    for name, job in (document.get("jobs") or {}).items():
        scopes.append((f"job:{name}", (job or {}).get("permissions")))
    for where, permissions in scopes:
        if permissions is None:
            if where == "workflow":
                refuse("category=workflow_permissions_unset — an unset "
                       "permissions block inherits the repository default, "
                       "which may include write scopes")
            continue
        if not isinstance(permissions, dict):
            refuse(f"category=workflow_permissions_not_mapping where={where}")
        writes = sorted(f"{k}={v}" for k, v in permissions.items()
                        if v not in ("read", "none"))
        if writes:
            refuse(f"category=workflow_permissions_grant_write where={where} "
                   f"scopes={writes}")
        seen.append(where)
    return {"read_only_scopes": seen}


def assert_checkouts_are_safe(document: dict) -> dict:
    """Every checkout: no persisted credential, no caller-chosen ref."""
    checked = []
    for name, job in (document.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            uses = str((step or {}).get("uses") or "")
            if _CHECKOUT_MARKER not in uses:
                continue
            where = f"{name}.steps[{index}]"
            with_block = (step or {}).get("with") or {}
            if with_block.get("persist-credentials") is not False:
                refuse("category=checkout_persists_credentials "
                       f"where={where} — a checkout that leaves a usable "
                       "token in .git/config hands write access to anything "
                       "that later runs in the workspace")
            if "repository" in with_block:
                refuse(f"category=checkout_selects_repository where={where} — "
                       "a credential-bearing workflow checks out its own "
                       "repository only; the candidate is fetched as inert "
                       "data by candidatefetch.py")
            ref = with_block.get("ref")
            if ref is not None:
                text = str(ref)
                bad = [p for p in CALLER_CONTROLLED_PREFIXES if p in text]
                if bad:
                    refuse(f"category=checkout_ref_caller_controlled "
                           f"where={where} expressions={bad} — if the caller "
                           "chooses the ref, the caller chooses the code that "
                           "runs with the secret")
            checked.append(where)
    if not checked:
        refuse("category=workflow_has_no_checkout_step")
    return {"checkout_steps": checked}


def assert_secret_containment(document: dict) -> dict:
    """Only environment-gated jobs that are gated behind D0 may name a secret."""
    jobs = document.get("jobs") or {}
    if CONTAINMENT_JOB not in jobs:
        refuse(f"category=containment_job_missing job={CONTAINMENT_JOB}")
    containment = jobs[CONTAINMENT_JOB] or {}
    if "environment" in containment:
        refuse(f"category=containment_job_has_environment job="
               f"{CONTAINMENT_JOB} — an `environment:` is what makes an "
               "environment secret reachable; the no-secret job must not have "
               "one")
    if _references_secret(containment):
        refuse(f"category=containment_job_references_secret "
               f"job={CONTAINMENT_JOB}")

    def gated_on(name, seen=()):
        """True when `name` transitively needs the containment job."""
        if name in seen:
            refuse(f"category=workflow_needs_cycle job={name}")
        needs = (jobs.get(name) or {}).get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        if CONTAINMENT_JOB in needs:
            return True
        return any(gated_on(parent, (*seen, name)) for parent in needs)

    credential_jobs = []
    for name, job in jobs.items():
        job = job or {}
        if not _references_secret(job):
            continue
        if not job.get("environment"):
            refuse(f"category=secret_job_not_environment_gated job={name} — a "
                   "secret must come from a protected environment, not from a "
                   "repository- or organization-level fallback")
        if not gated_on(name):
            refuse(f"category=secret_job_not_gated_behind_containment "
                   f"job={name} needs_job={CONTAINMENT_JOB}")
        credential_jobs.append(name)
    return {"credential_bearing_jobs": sorted(credential_jobs),
            "containment_job": CONTAINMENT_JOB}


def assert_no_literal_credential(raw: bytes) -> dict:
    """The file may name a secret; it may not contain one.

    `${{ secrets.NAME }}` is a reference resolved by the runner. An assignment
    whose right-hand side is anything else is a literal, and a literal in an
    unmerged branch is a leak."""
    text = raw.decode("utf-8")
    offenders = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or ":" not in stripped:
            continue
        name, _, value = stripped.partition(":")
        upper = name.strip().upper()
        if not any(marker in upper
                   for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            continue
        value = value.strip()
        if not value or (value.startswith("${{") and value.endswith("}}")):
            continue
        offenders.append(number)
    if offenders:
        refuse(f"category=workflow_assigns_literal_credential "
               f"lines={offenders}")
    return {"literal_credential_assignments": 0}


def assert_phase_secret_policy(document: dict, *, name: str,
                               policy: dict) -> dict:
    """D0 may not name a secret or declare an environment. At all.

    Not "may not use one" — may not NAME one. A workflow that references
    `secrets.X` is one approval away from receiving it; a workflow that has no
    such reference has nothing to widen."""
    references = sorted(set(_secret_references(document)))
    environments = sorted(
        job_name for job_name, job in (document.get("jobs") or {}).items()
        if (job or {}).get("environment"))
    if not policy["secrets_permitted"] and references:
        refuse(f"category=phase_must_not_name_a_secret name={name} "
               f"phase={policy['phase']} count={len(references)}")
    if not policy["environments_permitted"] and environments:
        refuse(f"category=phase_must_not_declare_an_environment name={name} "
               f"phase={policy['phase']} jobs={environments}")
    return {"secret_references": len(references),
            "environment_jobs": environments}


def validate_workflow_file(name: str = D0_FILE, *, root: str = ".") -> dict:
    """Every file-level check for ONE workflow, as one record."""
    loaded = load_workflow(name, root=root)
    document, policy = loaded["document"], loaded["policy"]
    record = {
        "name": name,
        "phase": policy["phase"],
        "deployable_now": policy["deployable_now"],
        "path": os.path.join(WORKFLOW_DIR, name),
        "workflow_sha256": loaded["sha256"],
        **assert_triggers(document),
        **assert_permissions_read_only(document),
        **assert_checkouts_are_safe(document),
        **assert_actions_pinned(document, name=name),
        **assert_no_source_selection(document, name=name),
        **assert_no_candidate_execution(document, name=name),
        **assert_phase_secret_policy(document, name=name, policy=policy),
        **assert_no_literal_credential(loaded["raw"]),
        "honest_scope": "the deployed shape is validated; a validated shape is "
                        "not a run",
    }
    if policy["secrets_permitted"]:
        record.update(assert_secret_containment(document))
    return record


def validate_all_workflows(*, root: str = ".") -> dict:
    """Every trusted-lane workflow, plus the cross-file containment rule."""
    records = {name: validate_workflow_file(name, root=root)
               for name in sorted(TRUSTED_WORKFLOWS)}
    return {
        "workflows": records,
        **assert_no_template_is_live(root=root),
        "action_pin_policy": actionpolicy.pin_record(),
    }
