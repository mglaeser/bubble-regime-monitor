"""Validate the proposed protected workflow as a FILE, not as a convention.

`workflowpolicy.py` states the rules over plain values. This module applies them
to the actual YAML that would be deployed, so a future edit that reintroduces
`pull_request_target`, adds a `ref` input, drops `persist-credentials: false`,
or lets the no-secret job reach a secret fails a test rather than a code review.

Two properties are worth naming because they are easy to lose:

* **The workflow lives outside `.github/workflows/`.** A workflow file in that
  directory on an unmerged branch is live. This module asserts the inert
  location, so "we'll move it later" cannot happen by accident.
* **`on` is a YAML 1.1 boolean.** `yaml.safe_load` turns the `on:` key into
  `True`. A validator that looks only for the string key silently validates
  nothing, which is the failure mode where the test passes and the policy is
  unenforced.
"""

from __future__ import annotations

import hashlib
import os

import yaml

from .errors import refuse
from .workflowpolicy import (
    assert_no_ref_selection,
    assert_trigger_permitted,
)

#: Where the inert interface lives, relative to the repository root.
WORKFLOW_PATH = os.path.join("scripts", "trustedlane", "workflow",
                             "trusted-verifier-lane.yml")

#: Directory that makes a workflow live. The interface must not be here yet.
LIVE_WORKFLOW_DIR = os.path.join(".github", "workflows")

#: The job that must gate every credential-bearing job.
CONTAINMENT_JOB = "d0-containment"

#: Expressions that let a caller choose which code runs.
CALLER_CONTROLLED_PREFIXES = ("inputs.", "github.event.",
                              "github.head_ref", "needs.")

# Named for the *reference* form, not the value: ruff S105 flags
# constants whose name contains "secret", and suppressing that check in a
# credential-handling module is the wrong trade.
_CREDENTIAL_REF_MARKER = "secrets."
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


def _references_secret(node) -> bool:
    return any(_CREDENTIAL_REF_MARKER in text for text in _strings(node))


def load_workflow(*, root: str = ".") -> dict:
    """Read and parse the inert workflow, refusing a live location."""
    live = os.path.join(root, LIVE_WORKFLOW_DIR,
                        os.path.basename(WORKFLOW_PATH))
    if os.path.exists(live):
        refuse("category=trusted_lane_workflow_is_live path="
               f"{LIVE_WORKFLOW_DIR} — a workflow in this directory runs on "
               "pushes to an unprotected branch; the D0 interface must stay "
               "inert until it is deployed from a protected default branch")
    path = os.path.join(root, WORKFLOW_PATH)
    if not os.path.exists(path):
        refuse(f"category=trusted_lane_workflow_missing path={WORKFLOW_PATH}")
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        refuse(f"category=trusted_lane_workflow_unparseable "
               f"exception_class={type(exc).__name__}")
    if not isinstance(document, dict):
        refuse("category=trusted_lane_workflow_not_mapping")
    return {"document": document, "raw": raw,
            "sha256": hashlib.sha256(raw).hexdigest()}


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


def validate_workflow_file(*, root: str = ".") -> dict:
    """Every file-level check, as one record."""
    loaded = load_workflow(root=root)
    document = loaded["document"]
    record = {
        "path": WORKFLOW_PATH,
        "workflow_sha256": loaded["sha256"],
        "live_location": False,
        **assert_triggers(document),
        **assert_permissions_read_only(document),
        **assert_checkouts_are_safe(document),
        **assert_secret_containment(document),
        **assert_no_literal_credential(loaded["raw"]),
        "honest_scope": "the deployed shape is validated; a validated shape is "
                        "not a run, and no run of this workflow exists",
    }
    return record
