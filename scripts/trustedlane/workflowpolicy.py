"""Workflow shapes that must never carry a secret.

`pull_request_target` runs the BASE workflow definition with write permissions
and secrets available, against a head the pull request controls. Any step that
checks out or executes head content — a build script, a test runner, an
editable install, a hook — hands the secret to the candidate. A
`workflow_dispatch` whose `ref` input can select a candidate branch is the same
hazard with an extra step: the workflow definition comes from the candidate too.

These are refused by inspection rather than by convention, so a future
workflow edit fails a test instead of a review."""

from __future__ import annotations

from .errors import refuse

#: Triggers that expose secrets to candidate-controlled content.
FORBIDDEN_TRIGGERS = ("pull_request_target", "issue_comment",
                      "workflow_run")

#: Triggers a credential-bearing trusted run may use.
PERMITTED_TRIGGERS = ("push", "schedule", "workflow_dispatch")

#: Inputs that can redirect execution to a candidate ref.
REF_SELECTING_INPUTS = ("ref", "sha", "branch", "head", "head_ref",
                        "candidate_ref")


def assert_trigger_permitted(trigger: str) -> str:
    if trigger in FORBIDDEN_TRIGGERS:
        refuse(f"category=forbidden_workflow_trigger trigger={trigger} — this "
               "trigger runs with secrets available against content the "
               "candidate controls")
    if trigger not in PERMITTED_TRIGGERS:
        refuse(f"category=unknown_workflow_trigger trigger={trigger!r}")
    return trigger


def assert_no_ref_selection(inputs) -> dict:
    """A credential-bearing workflow takes no ref from its caller.

    If the caller chooses the ref, the caller chooses the code."""
    names = sorted(inputs or ())
    selecting = [n for n in names if n in REF_SELECTING_INPUTS]
    if selecting:
        refuse(f"category=workflow_input_selects_ref inputs={selecting} — a "
               "credential-bearing workflow that takes a ref from its caller "
               "lets the caller choose which code runs with the secret")
    return {"inputs": names, "ref_selection": False}


def assert_checkout_is_inert(step: dict) -> dict:
    """A candidate checkout may be fetched, never executed.

    `run` alongside a candidate checkout in the same job is the shape that
    turns inert data into code."""
    if not isinstance(step, dict):
        refuse("category=checkout_step_not_object")
    if step.get("executes_checked_out_code"):
        refuse("category=candidate_checkout_executed — candidate commits are "
               "inert data: fetched, hashed, diffed, never run")
    if step.get("persist_credentials") is not False:
        refuse("category=checkout_persists_credentials — a candidate checkout "
               "must not leave a usable token in its .git/config")
    return {"inert": True, "persist_credentials": False}


def assert_secret_not_available(job: dict) -> dict:
    """D0 asserts the job has NO secret at all, not that it uses none."""
    if not isinstance(job, dict):
        refuse("category=job_not_object")
    declared = job.get("secrets") or {}
    env = job.get("env") or {}
    if declared:
        refuse(f"category=d0_job_declares_secrets count={len(declared)} — the "
               "no-secret bootstrap declares none; a job that can read one is "
               "not the bootstrap")
    suspicious = sorted(k for k in env
                        if "KEY" in k.upper() or "TOKEN" in k.upper()
                        or "SECRET" in k.upper())
    if suspicious:
        refuse(f"category=d0_job_env_looks_like_a_credential "
               f"names={suspicious}")
    return {"secrets_declared": 0, "env_names": sorted(env)}
