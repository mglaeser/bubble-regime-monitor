"""Operational state, and the lock that keeps the trusted lane shut while it runs.

## Why a state machine rather than a string in a JSON file

The first version of the governance record simply said

    ci_operational_state = MIDTERM_SINGLE_REPO_PANEL_ACTIVE

while the workflow was not runnable, no entry point existed and no provider call
had ever been made. That is the same defect class the whole programme has been
correcting: a document asserting a capability that no run has demonstrated. The
fix is not a more careful author. It is to make the claim reachable only by
passing through the states that earn it.

Five states, in order:

    IMPLEMENTATION_IN_PROGRESS      code exists, workflow not merged, nothing ran
    STAGED_NO_PROVIDER_SECRET       merged and dry-run green, NO key installed
    STAGED_PROVIDER_KEY_PRESENT_NO_CALLS
                                    key installed, engine release approved, a
                                    hosted no-provider validation green, and no
                                    provider or generation call yet made
    PROVIDER_ATTEMPTS_OBSERVED_NOT_ACTIVE
                                    a provider attempt happened and was durably
                                    accounted for; NO review completed
    ACTIVE                          a real count and panel completed on an exact
                                    candidate head

`IMPLEMENTATION_IN_PROGRESS -> ACTIVE` is refused. Not discouraged — refused.
Skipping a state is precisely how a system arrives at "active" without ever
having proved it can run without spending money, and it is a one-word edit in a
JSON file, so it needs to be a failing test rather than a review comment.

## Why the key-present rung was added

`STAGED_NO_PROVIDER_SECRET -> ACTIVE` used to be one step, and it spanned the
two events that matter most: the credential arriving, and the first call being
made. While the key sat installed and unused there was no state that said so —
the document either understated the situation (`no provider secret`, false once
the key existed) or overstated it (`ACTIVE`, false until a panel had run). Both
readings are wrong in the way this whole ladder exists to prevent, and the
window between them is exactly when a stray provider call would be an incident
rather than a cost.

So the rung is named for what is true in it: the key is present and no call has
been made. Reaching it requires the approved engine release to be bound and a
hosted validation to have completed with zero provider calls; leaving it for
`ACTIVE` requires a real panel. `STAGED_NO_PROVIDER_SECRET -> ACTIVE` is now
refused, because a lane that has never held a key cannot have spent one.

## The walk, not just the step

`assert_transition` checks ONE step, which is all it can see when a caller asks
"may I move from here to there". A committed document is a different question:
it asserts a state, and nothing used to check that the state was ever REACHED
legally. `assert_recorded_history_is_a_legal_walk` reads the ordered history the
policy document records and refuses a walk that starts anywhere but the first
state, that takes a step the ladder forbids, or whose last entry is not the
state the document declares.

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
#: The key exists and has never been spent. Named for both halves, because a
#: state called "staged" that did not mention the credential is how the window
#: between "installed" and "used" stayed invisible.
STAGED_PROVIDER_KEY_PRESENT_NO_CALLS = (
    "MIDTERM_SINGLE_REPO_PANEL_STAGED_PROVIDER_KEY_PRESENT_NO_CALLS"  # noqa: S105 - a STATE NAME; pragma: allowlist secret
)
#: A provider attempt has happened and no review has completed.
#:
#: Added because the rung below it became false while nobody could say so.
#: Panel run 31718284187 made one real count attempt and the provider answered
#: `400 invalid_json_schema` at `text.format.schema`; sixteen operator
#: diagnostic count-only calls followed. `..._NO_CALLS` was then a false
#: statement, and `ACTIVE` — "a real count and a real panel have both
#: completed" — was equally false, because neither did.
#:
#: Both readings being wrong at once is exactly the gap the key-present rung
#: was added to close one level down, reappearing one level up: the ladder had
#: no name for "it tried, it failed, nothing was reviewed".
PROVIDER_ATTEMPTS_OBSERVED_NOT_ACTIVE = (
    "MIDTERM_SINGLE_REPO_PANEL_PROVIDER_ATTEMPTS_OBSERVED_NOT_ACTIVE"
)
ACTIVE = "MIDTERM_SINGLE_REPO_PANEL_ACTIVE"

#: In ladder order. `STATES` is used for membership, never for ordering — the
#: adjacency map below is the only thing that decides what may follow what.
STATES = (IMPLEMENTATION_IN_PROGRESS, STAGED_NO_PROVIDER_SECRET,
          STAGED_PROVIDER_KEY_PRESENT_NO_CALLS,
          PROVIDER_ATTEMPTS_OBSERVED_NOT_ACTIVE, ACTIVE)

#: The state a lane starts in, and the only legal beginning of a recorded walk.
INITIAL_STATE = IMPLEMENTATION_IN_PROGRESS

#: state -> the states it may legally become.
#:
#: Written as an explicit adjacency map rather than an ordered list plus an
#: index comparison. An index comparison says "forward only" and would happily
#: allow a jump of two; this says which single step is permitted from where.
PERMITTED_TRANSITIONS = {
    IMPLEMENTATION_IN_PROGRESS: (STAGED_NO_PROVIDER_SECRET,),
    STAGED_NO_PROVIDER_SECRET: (STAGED_PROVIDER_KEY_PRESENT_NO_CALLS,
                                IMPLEMENTATION_IN_PROGRESS),
    # Backwards to the no-key state is how the key is RETIRED: delete the
    # secret, then say so. Forwards is now the attempts rung, NOT `ACTIVE`:
    # a lane cannot pass from "no calls" to "a panel completed" in one step,
    # because the first provider call and the first completed review are two
    # different events and the run that proved it made the first one made
    # neither of the second.
    STAGED_PROVIDER_KEY_PRESENT_NO_CALLS: (
        PROVIDER_ATTEMPTS_OBSERVED_NOT_ACTIVE, STAGED_NO_PROVIDER_SECRET),
    # NOT back to `..._NO_CALLS`. That claim is monotonic: once a provider
    # attempt has been observed, no later edit can truthfully say none was
    # made. Retiring the credential is still reachable — that is the step to
    # `STAGED_NO_PROVIDER_SECRET`, which is a statement about the secret
    # rather than a denial of the attempts.
    PROVIDER_ATTEMPTS_OBSERVED_NOT_ACTIVE: (ACTIVE, STAGED_NO_PROVIDER_SECRET),
    # Down from `ACTIVE` lands on the attempts rung for the same reason: a lane
    # that has run a panel cannot return to a state asserting it never called a
    # provider.
    ACTIVE: (PROVIDER_ATTEMPTS_OBSERVED_NOT_ACTIVE,),
}

#: What each state asserts has already happened. Used in the refusal message so
#: a reader is told which evidence is missing, not merely that they are wrong.
STATE_MEANS = {
    IMPLEMENTATION_IN_PROGRESS:
        "code exists; the workflow is not merged and has never run",
    STAGED_NO_PROVIDER_SECRET:
        "merged, and a hosted dry-run completed with zero provider calls and "
        "no usable repository provider secret installed",
    STAGED_PROVIDER_KEY_PRESENT_NO_CALLS:
        "a usable repository provider secret is installed, an approved engine "
        "release is bound by all seven binding fields, a hosted no-provider "
        "validation of that exact release completed, and no provider or "
        "generation call has been made",
    PROVIDER_ATTEMPTS_OBSERVED_NOT_ACTIVE:
        "a usable repository provider secret is installed, an approved engine "
        "release is bound, at least one provider attempt has been observed and "
        "durably accounted for, and NO count-and-panel review has completed — "
        "so no verdict, no panel evidence and no approval exist",
    ACTIVE:
        "a real count and a real panel have both completed on an exact "
        "candidate head, with the provider actually called",
}

#: The operator's explicit authorisation for the FIRST run that may spend.
#:
#: Separate from the state on purpose, and the reason is a genuine
#: chicken-and-egg. `ACTIVE` means "a real count and a real panel have
#: completed", so it cannot be declared before one has — but with the engine
#: release bound and the key installed, nothing else stood between a merge and
#: an automatic provider-backed panel on the next pull request. The first run
#: that spends money would then have happened as a side effect of merging a
#: governance file, which is the opposite of deliberate.
#:
#: So the authorisation is one boolean an operator flips in its own commit,
#: reviewed like any other change to `main`. It is not a state, it does not
#: move up or down a ladder, and it is read only while the lane sits at the
#: key-present rung; once `ACTIVE` has been earned, the panel runs normally and
#: this field has nothing left to gate.
FIRST_PROVIDER_RUN_FIELD = "first_provider_backed_run_authorised"

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


def assert_recorded_history_is_a_legal_walk(*, root: str = ".") -> dict:
    """The declared state must have been REACHED, not merely written down.

    `assert_transition` answers "may I take this one step". Nothing answered
    "was the state in this file ever arrived at legally", and the answer used
    to be unknowable, because the document recorded a state and no path. A
    single-value field is editable to any member of `STATES` in one keystroke,
    and every ladder check would still pass because no check was looking at a
    ladder — it was looking at a word.

    So the document records the ordered walk, and this reads it as one:

      * it must begin at `INITIAL_STATE`; a history that starts halfway up has
        assumed the rungs below it
      * every consecutive pair must satisfy `assert_transition`, which is the
        same function a live transition uses, not a second copy of the rules
      * the last entry must be the state the document declares, so the walk
        and the claim cannot drift apart

    Repeats are permitted — a lane may re-record the state it is already in —
    and are reported rather than silently dropped, because a history that
    repeats a state twice is fine and a history that oscillates is a fact a
    reader should see."""
    policy = load_policy(root=root)
    architecture = policy.get("architecture") or {}
    history = architecture.get("ci_operational_state_history")
    if not isinstance(history, list) or not history:
        refuse("category=midterm_policy_has_no_state_history — the governance "
               "record must declare `architecture.ci_operational_state_history` "
               "as the ordered walk that reached the declared state. A state "
               "with no path is a word, and every ladder check would pass")
    for entry in history:
        if not isinstance(entry, str):
            refuse(f"category=midterm_state_history_entry_not_a_string "
                   f"entry_type={type(entry).__name__}")
        assert_known_state(entry)
    if history[0] != INITIAL_STATE:
        refuse(f"category=midterm_state_history_does_not_start_at_the_beginning "
               f"first={history[0]} expected={INITIAL_STATE} — a walk that "
               "starts halfway up the ladder has assumed the rungs below it")
    # By index rather than `zip(history, history[1:])`. The two arguments are
    # deliberately different lengths, so `strict=True` would be wrong and
    # `strict=False` would be silencing a warning about the one case that
    # matters — a walk truncated between the two reads.
    steps = [assert_transition(history[i], history[i + 1])
             for i in range(len(history) - 1)]
    declared = current_state(root=root)
    if history[-1] != declared:
        refuse(f"category=midterm_state_history_does_not_reach_the_declared_state"
               f" last={history[-1]} declared={declared} — the recorded walk "
               "and the declared state describe two different lanes")
    return {"state": declared, "history": list(history),
            "steps": steps,
            "transitions_taken": sum(1 for s in steps if s["changed"]),
            "honest_scope": ("the walk is internally legal and ends where the "
                             "document says it is. Whether each step's "
                             "evidence exists is not a question a JSON file "
                             "can answer")}


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
    if state != IMPLEMENTATION_IN_PROGRESS and not live:
        refuse(f"category=midterm_state_claims_more_than_the_tree_shows "
               f"state={state} workflow_live=False — {state} means "
               f"'{STATE_MEANS[state]}', and the workflow that would have done "
               "it is not in the live directory")
    # The key-present rung asserts a bound engine release as well as a merged
    # workflow, and that IS visible from the tree: `provenance_of` refuses a
    # partial binding. Checked in the same one direction — a complete binding
    # cannot confirm the state, it can only fail to disprove it.
    if state in (STAGED_PROVIDER_KEY_PRESENT_NO_CALLS,
                 PROVIDER_ATTEMPTS_OBSERVED_NOT_ACTIVE, ACTIVE):
        from .engine import APPROVED_RELEASE, provenance_of
        release = _committed_release(root=root)
        if provenance_of(release) != APPROVED_RELEASE:
            refuse(f"category=midterm_state_claims_an_engine_release_it_lacks "
                   f"state={state} — {state} means '{STATE_MEANS[state]}', and "
                   "`governance/midterm-panel-engine-release.json` does not "
                   "name all seven binding fields, so no run under this state "
                   "could obtain an engine at all")
    return {"state": state, "workflow_live": live,
            "honest_scope": ("absence can disprove a state; presence cannot "
                             "confirm one — no file proves a provider call "
                             "happened")}


def assert_provider_backed_run_is_authorised(*, root: str = ".") -> dict:
    """May this lane make a provider call at all, right now?

    Asked of the committed policy, in the credential-bearing job, before the
    engine is built — because until this activation the answer was decided by
    an ACCIDENT. `assert_provenance_permits` refused a provider-backed run
    while the engine release was unbound, and that refusal was doing double
    duty: it meant "no approved engine" and it happened to also mean "no
    spending yet". Binding the release answered the first question and silently
    answered the second one too, so the first run that cost money would have
    been triggered by whoever next opened a pull request.

    Three states may spend at all, and they are asked different questions:

        STAGED_PROVIDER_KEY_PRESENT_NO_CALLS   an operator must have said yes,
                                               explicitly, in its own commit
        PROVIDER_ATTEMPTS_OBSERVED_NOT_ACTIVE  the SAME question, still asked.
                                               An attempt that failed spent the
                                               authorisation on nothing; the
                                               operator's decision is what
                                               permits the retry, and a run
                                               that reached a provider and
                                               produced no review has earned no
                                               standing that a completed panel
                                               would have earned
        ACTIVE                                 the lane has already run; the
                                               authorisation is spent

    A `True` here is not a claim about the world and cannot be wrong the way a
    state can. It is a decision, so it is read strictly: absent is refused, and
    so is the string `"true"`, for the reason `assert_ref_protected` refuses
    it — `bool("false")` is `True`, and a lenient parser would turn a hedge
    into a yes."""
    state = current_state(root=root)
    if state in (IMPLEMENTATION_IN_PROGRESS, STAGED_NO_PROVIDER_SECRET):
        refuse(f"category=provider_backed_run_forbidden_in_this_state "
               f"state={state} — {state} means '{STATE_MEANS[state]}'. A lane "
               "that has not recorded holding a usable provider key must not "
               "be the lane that spends one")
    if state == ACTIVE:
        return {"state": state, "authorised": True,
                "authorisation": "the lane is ACTIVE; a real panel has run",
                "honest_scope": ("says a provider call is permitted, not that "
                                 "one will succeed or is a good idea")}
    architecture = (load_policy(root=root).get("architecture") or {})
    decision = architecture.get(FIRST_PROVIDER_RUN_FIELD)
    if decision is not True:
        refuse(f"category=first_provider_backed_run_not_authorised "
               f"state={state} field={FIRST_PROVIDER_RUN_FIELD} "
               f"value={decision!r} — the engine release is bound and the key "
               "is installed, so nothing else stands between a merge and an "
               "automatic panel that spends. The first such run is an operator "
               "decision: set "
               f"`architecture.{FIRST_PROVIDER_RUN_FIELD}` to the JSON literal "
               "`true` in governance/midterm-panel-policy.json, on main, in its "
               "own reviewed commit. A string is refused, not coerced")
    return {"state": state, "authorised": True,
            "authorisation": f"architecture.{FIRST_PROVIDER_RUN_FIELD} is true",
            "honest_scope": ("an operator authorised the first spending run in "
                             "a committed, reviewable decision. It does not "
                             "record that anyone reviewed the amount")}


def _committed_release(*, root: str = ".") -> dict:
    """The engine-release document, read the same strict way as the policy."""
    path = os.path.join(root, "governance", "midterm-panel-engine-release.json")
    if not os.path.exists(path):
        refuse(f"category=midterm_engine_release_document_missing path={path}")
    try:
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        refuse(f"category=midterm_engine_release_document_unreadable "
               f"exception_class={type(exc).__name__}")
