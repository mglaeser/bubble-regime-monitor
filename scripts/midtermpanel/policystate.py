"""Operational state, and the lock that keeps the trusted lane shut while it runs.

## Why a state machine rather than a string in a JSON file

The first version of the governance record simply said

    ci_operational_state = MIDTERM_SINGLE_REPO_PANEL_ACTIVE

while the workflow was not runnable, no entry point existed and no provider call
had ever been made. That is the same defect class the whole programme has been
correcting: a document asserting a capability that no run has demonstrated. The
fix is not a more careful author. It is to make the claim reachable only by
passing through the states that earn it.

Three states, in order:

    IMPLEMENTATION_IN_PROGRESS   code exists, workflow not merged, nothing ran
    STAGED_NO_PROVIDER_SECRET    merged and dry-run green, NO key installed
    ACTIVE                       key installed AND a real count and panel
                                 completed on an exact candidate head

`IMPLEMENTATION_IN_PROGRESS -> ACTIVE` is refused. Not discouraged — refused.
Skipping the middle state is precisely how a system arrives at "active" without
ever having proved it can run without spending money, and it is a one-word edit
in a JSON file, so it needs to be a failing test rather than a review comment.

## The mode lock

The mid-term panel uses a REPOSITORY-scoped secret named
`TRUSTED_VERIFIER_OPENAI_KEY`. The trusted lane's operator prerequisite 6
(`no_repository_or_org_fallback`) exists to forbid exactly that, because a
repository secret is readable from any ref and silently defeats an environment's
deployment-branch policy — the environment gate still appears to be in force
while the credential is reachable from a branch it was supposed to exclude.

So the two architectures are mutually exclusive, and the exclusion has to be
mechanical. `assert_trusted_lane_is_locked` refuses whenever the mid-term
workflow is live and persistent-secret mode is declared while
`phases.IMPLEMENTED_PHASE` is anything other than `D0`.

Note what this gate can and cannot see. It cannot query whether a secret exists —
no code here can, and code that claimed to would be lying. It reads two things it
CAN check: whether the live workflow file is present, and what the policy
document declares. Retiring the mode therefore requires removing the workflow,
deleting the secret, recording an operator confirmation that no fallback remains,
and transitioning the policy — and only the first and last of those are visible
here. The docstring says so rather than letting the gate imply more than it
knows.
"""

from __future__ import annotations

import json
import os

from . import WORKFLOW_FILENAME
from .errors import refuse

IMPLEMENTATION_IN_PROGRESS = "MIDTERM_SINGLE_REPO_PANEL_IMPLEMENTATION_IN_PROGRESS"
STAGED_NO_PROVIDER_SECRET = (
    "MIDTERM_SINGLE_REPO_PANEL_STAGED_NO_PROVIDER_SECRET"  # noqa: S105 - a STATE NAME; pragma: allowlist secret
)
ACTIVE = "MIDTERM_SINGLE_REPO_PANEL_ACTIVE"

STATES = (IMPLEMENTATION_IN_PROGRESS, STAGED_NO_PROVIDER_SECRET, ACTIVE)

#: state -> the states it may legally become.
#:
#: Written as an explicit adjacency map rather than an ordered list plus an
#: index comparison. An index comparison says "forward only" and would happily
#: allow a jump of two; this says which single step is permitted from where.
PERMITTED_TRANSITIONS = {
    IMPLEMENTATION_IN_PROGRESS: (STAGED_NO_PROVIDER_SECRET,),
    STAGED_NO_PROVIDER_SECRET: (ACTIVE, IMPLEMENTATION_IN_PROGRESS),
    ACTIVE: (STAGED_NO_PROVIDER_SECRET,),
}

#: What each state asserts has already happened. Used in the refusal message so
#: a reader is told which evidence is missing, not merely that they are wrong.
STATE_MEANS = {
    IMPLEMENTATION_IN_PROGRESS:
        "code exists; the workflow is not merged and has never run",
    STAGED_NO_PROVIDER_SECRET:
        "merged, and a hosted dry-run completed with zero provider calls and "
        "no repository secret installed",
    ACTIVE:
        "the repository secret is installed AND a real count and a real panel "
        "have both completed on an exact candidate head",
}

#: The declared mode. While this is the policy's mode, trusted D1/D2 is locked.
PERSISTENT_REPOSITORY_SECRET_MODE = (
    "MIDTERM_PERSISTENT_REPOSITORY_SECRET_MODE"  # noqa: S105 - a MODE NAME; pragma: allowlist secret
)


def assert_known_state(state: str) -> str:
    if state not in STATES:
        refuse(f"category=midterm_state_unknown state={state!r} "
               f"permitted={list(STATES)}")
    return state


def assert_transition(current: str, target: str) -> dict:
    """Refuse any move the ladder does not permit.

    The refusal names what the target state CLAIMS, because the useful message
    is not "that transition is illegal" but "you are asserting a real panel
    completed and the previous state says nothing has ever run"."""
    assert_known_state(current)
    assert_known_state(target)
    if current == target:
        return {"from": current, "to": target, "changed": False}
    permitted = PERMITTED_TRANSITIONS[current]
    if target not in permitted:
        refuse(f"category=midterm_state_transition_forbidden from={current} "
               f"to={target} permitted={list(permitted)} — {target} means "
               f"'{STATE_MEANS[target]}', and {current} means "
               f"'{STATE_MEANS[current]}'. The intermediate state is where that "
               "evidence is produced; skipping it is how a system becomes "
               "'active' without ever proving it can run")
    return {"from": current, "to": target, "changed": True}


def load_policy(*, root: str = ".") -> dict:
    """Read the governance record, refusing anything unparseable.

    `object_pairs_hook` refuses duplicate keys. A JSON document with the state
    declared twice parses fine under the default loader and silently keeps the
    last one — which makes the effective policy depend on file order rather than
    on review."""
    path = os.path.join(root, "governance", "midterm-panel-policy.json")
    if not os.path.exists(path):
        refuse(f"category=midterm_policy_missing path={path}")

    def _no_duplicates(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                refuse(f"category=midterm_policy_duplicate_key key={key!r} — a "
                       "document that declares the same key twice has an "
                       "effective value that depends on parse order")
            seen[key] = value
        return seen

    try:
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"),
                              object_pairs_hook=_no_duplicates)
    except (OSError, UnicodeDecodeError) as exc:
        refuse(f"category=midterm_policy_unreadable "
               f"exception_class={type(exc).__name__}")
    except json.JSONDecodeError as exc:
        refuse(f"category=midterm_policy_not_json line={exc.lineno}")


def current_state(*, root: str = ".") -> str:
    policy = load_policy(root=root)
    state = (policy.get("architecture") or {}).get("ci_operational_state")
    if not isinstance(state, str):
        refuse("category=midterm_policy_has_no_state — the governance record "
               "must declare `architecture.ci_operational_state`")
    return assert_known_state(state)


def workflow_is_live(*, root: str = ".") -> bool:
    """Is the privileged panel workflow present in the live directory?

    By path, because that is what GitHub schedules. A file elsewhere in the
    repository is source; a file here is a thing that runs."""
    return os.path.exists(
        os.path.join(root, ".github", "workflows", WORKFLOW_FILENAME))


def persistent_secret_mode_declared(*, root: str = ".") -> bool:
    policy = load_policy(root=root)
    return bool((policy.get("secret") or {}).get(
        "persistent_repository_secret_mode"))


def assert_trusted_lane_is_locked(*, root: str = ".") -> dict:
    """While the mid-term panel is live, trusted D1/D2 must stay at D0.

    The two architectures cannot coexist: the mid-term panel installs a
    REPOSITORY-scoped secret under the name the trusted lane expects as an
    ENVIRONMENT secret, which is what prerequisite 6 forbids. A repository
    secret is readable from any ref, so an environment's deployment-branch
    policy would appear to be in force while the credential was reachable from a
    branch it was meant to exclude.

    HONEST SCOPE. This gate reads two facts it can actually check — the live
    workflow file, and the declared policy mode. It cannot query whether a
    secret exists, and it does not pretend to. Retiring the mode requires
    removing the workflow, deleting the secret, an operator record confirming no
    fallback, and a policy transition; only the first and the last are visible
    from here."""
    from trustedlane import phases

    live = workflow_is_live(root=root)
    declared = persistent_secret_mode_declared(root=root)
    locked = live and declared
    if locked and phases.IMPLEMENTED_PHASE != phases.D0:
        refuse(f"category=trusted_lane_activated_under_persistent_secret_mode "
               f"implemented_phase={phases.IMPLEMENTED_PHASE} "
               f"mode={PERSISTENT_REPOSITORY_SECRET_MODE} — the mid-term panel "
               "is live with a repository-scoped secret named after the trusted "
               "lane's environment secret. That is exactly what operator "
               "prerequisite 6 forbids: a repository secret is readable from any "
               "ref, so the environment gate would appear to hold while the "
               "credential was reachable from a branch it excluded. Retire the "
               "mid-term mode before raising the phase")
    return {
        "workflow_live": live,
        "persistent_secret_mode": declared,
        "trusted_lane_locked": locked,
        "implemented_phase": phases.IMPLEMENTED_PHASE,
        "honest_scope": ("reads the live workflow file and the declared policy "
                         "mode; it cannot observe whether a secret exists"),
    }


def assert_state_is_consistent_with_reality(*, root: str = ".") -> dict:
    """The declared state must not claim more than the tree shows.

    One direction only, and deliberately so. Code can see that a workflow file
    is absent, which makes `ACTIVE` impossible; it cannot see that a provider
    call succeeded, so it cannot confirm `ACTIVE` either. A check that pretended
    to confirm would be the overclaim it was written to prevent."""
    state = current_state(root=root)
    live = workflow_is_live(root=root)
    if state in (STAGED_NO_PROVIDER_SECRET, ACTIVE) and not live:
        refuse(f"category=midterm_state_claims_more_than_the_tree_shows "
               f"state={state} workflow_live=False — {state} means "
               f"'{STATE_MEANS[state]}', and the workflow that would have done "
               "it is not in the live directory")
    return {"state": state, "workflow_live": live,
            "honest_scope": ("absence can disprove a state; presence cannot "
                             "confirm one — no file proves a provider call "
                             "happened")}
