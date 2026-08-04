"""Every input the count path needs, as a file with a named workflow producer.

## Why this module exists

The previous wiring passed `environ["MIDTERM_SKELETON_PATH"]` straight into
`prepare_review_plan_core(skeleton=...)`. That parameter takes a skeleton — a
dict the engine's planner returns, self-hashed and strict-validated — and what
it received was a string. The same was true of `pin_record` and
`authorizations`. None of the three was materialized by anything: no workflow
step wrote a file at those paths, and no step exported those variables.

So the count job could not have run once. Worse, it would not have failed at the
seam: it would have failed somewhere inside the engine, in the job holding the
credential, with a message about a string where a mapping was expected.

This module is the answer to "where does each input come from, and who wrote
it". Every entry names its producer, and `assert_all_present` reports the whole
missing set at once rather than one refusal per attempt — an operator fixing a
run should learn everything that is wrong in one go.

## What is loaded and what is produced

Not all inputs are files, and pretending otherwise would be its own defect:

  * the REPOSITORY is a directory — the default-branch checkout with the
    candidate's objects fetched in. Objects, not a worktree; that distinction
    is the safety argument and `privilegedworkflow` is what enforces it.
  * the SKELETON is *produced*, by the engine's own planner, from the
    repository. It is deliberately not an input: a skeleton handed in from
    outside is a description of the candidate that nobody in this job derived,
    and the whole point of Stage 1 is that the reviewer derives it.
  * the PIN RECORD, the AUTHORIZATIONS and the CHALLENGE are files.
  * the ENGINE is neither: `engine.load_engine_for_mode` owns it, because its
    identity is checked against the candidate's before any byte is built.

## Authorizations, honestly

The trusted lane gets its literal-authorization set from an authenticated
operator envelope. This lane has no envelope authority — that is exactly what
Exchange 8 blocked on — so there is no honest source for one here.

Absence is therefore represented as absence: `authorizations` is `None`, which
is the engine's own default and which it handles by recording
`confers_real_call_authority=False`. What is NOT done is to synthesise a set
locally and let the manifest record a clearance nobody authorized. The record
this module returns says which of the two happened.
"""

from __future__ import annotations

import json
import os

from .errors import refuse

#: Each input, its environment variable, and the workflow step that must write
#: it. The producer is part of the declaration so that "this input has no
#: producer" is a statement the tests can check against the workflow file rather
#: than a fact somebody has to notice.
COUNT_INPUTS = (
    {"name": "repository_path",
     "variable": "MIDTERM_REPOSITORY_PATH",
     "kind": "directory",
     "producer": "count / assert the candidate is present as OBJECTS",
     "required": True},
    {"name": "pin_record_path",
     "variable": "MIDTERM_PIN_RECORD_PATH",
     "kind": "json_object",
     "producer": "count / materialise the operator PIN record",
     "required": True},
    {"name": "challenge_path",
     "variable": "MIDTERM_CHALLENGE_PATH",
     "kind": "text",
     "producer": "count / materialise the run challenge",
     "required": True},
    {"name": "authorizations_path",
     "variable": "MIDTERM_AUTHORIZATIONS_PATH",
     "kind": "json_list",
     "producer": "count / materialise the literal authorizations",
     "required": False},
)

#: The challenge proves a verdict was written for THIS run. The engine refuses
#: anything under 16 characters, and a short one is guessable — which proves
#: nothing at all. Stated here too so the refusal names the input rather than
#: surfacing from inside the engine as a core failure.
MINIMUM_CHALLENGE_CHARS = 16


def assert_all_present(environ: dict) -> dict:
    """Every required input, checked before anything expensive happens.

    Reports the whole missing set. One-at-a-time refusals turn a misconfigured
    workflow into as many failed runs as there are missing inputs, and each one
    costs a job that may hold a credential."""
    missing = []
    for spec in COUNT_INPUTS:
        if not spec["required"]:
            continue
        value = str(environ.get(spec["variable"]) or "").strip()
        if not value:
            missing.append({"variable": spec["variable"],
                            "producer": spec["producer"]})
    if missing:
        refuse(f"category=count_inputs_not_materialised "
               f"missing={[m['variable'] for m in missing]} "
               f"producers={[m['producer'] for m in missing]} — each of these "
               "is written by a named workflow step; a variable with no "
               "producer is an input the run cannot have")
    return {"required_inputs_present": True,
            "variables": [s["variable"] for s in COUNT_INPUTS if s["required"]]}


def _read_text(path: str, *, what: str) -> str:
    if not os.path.isfile(path):
        refuse(f"category=count_input_file_absent input={what} — the workflow "
               "step that writes it did not run, or wrote somewhere else")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        refuse(f"category=count_input_unreadable input={what} "
               f"exception_class={type(exc).__name__}")


def _read_json(path: str, *, what: str):
    raw = _read_text(path, what=what)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # The exception text can quote the file's own bytes, and one of these
        # files is an operator record. Class and position only.
        refuse(f"category=count_input_not_json input={what} "
               f"line={exc.lineno} column={exc.colno}")


def load_repository_path(environ: dict) -> str:
    """The directory the engine reads the candidate's objects out of."""
    path = str(environ.get("MIDTERM_REPOSITORY_PATH") or "").strip()
    if not path:
        refuse("category=count_input_absent input=repository_path")
    if not os.path.isdir(path):
        refuse("category=repository_path_is_not_a_directory — the engine runs "
               "git plumbing against this path, and a missing one surfaces "
               "from inside the engine as an unhelpful git failure")
    if not os.path.isdir(os.path.join(path, ".git")):
        refuse("category=repository_path_is_not_a_git_repository — the "
               "candidate's commits are read as OBJECTS from here; without a "
               "`.git` there are no objects to read")
    return path


#: The environment variable carrying the review CLASS preflight derived.
#:
#: A class name, not a free-form target, and preflight is what produces it —
#: `preflight.review_class_for` maps a pull-request number to exactly one of
#: four. Required rather than defaulted: the profiles differ by more than an
#: order of magnitude in cost cap, so a default profile is a default budget.
REVIEW_CLASS_ENV = "MIDTERM_REVIEW_CLASS"


def load_pin_values(environ: dict) -> dict:
    """The twelve PIN VALUES and the two transport ceilings for THIS class.

    Every individual PIN's range, type and per-model reasoning-effort support
    is `verifier.pins.validate_pins`' rule, and a second range table here would
    be a second opinion about numbers the operator approved once. So this reads
    the file, selects the profile, and checks only that there is a mapping.

    ## Why profiles rather than one global cap

    The operator approved different ceilings for different kinds of review. A
    single global cap has to be the largest of them to let the largest run
    finish, which means every smaller run is protected by a number chosen for a
    bigger one."""
    document = _read_json(environ["MIDTERM_PIN_RECORD_PATH"], what="pin_record")
    if not isinstance(document, dict):
        refuse("category=pin_record_not_an_object")
    pins = document.get("pins")
    if not isinstance(pins, dict) or not pins:
        refuse("category=pin_record_has_no_pins — the engine reads "
               "`record['pins']`; a record without them would reach "
               "`verifier.pins` as an empty mapping and refuse there, in the "
               "job holding the credential")
    selected, profile = select_profile(document, environ=environ)
    return {"pins": selected, "profile": profile,
            "authority_class": document.get("authority_class"),
            "approved_by": document.get("approved_by"),
            "approved_at": document.get("approved_at")}


def select_profile(document: dict, *, environ: dict) -> tuple:
    """The exact profile for this review class, by exact name.

    The class must BE a profile key. No substring matching, no fallback to the
    largest: both would make "which budget applies" a property of spelling."""
    from .preflight import REVIEW_CLASSES
    name = str(environ.get(REVIEW_CLASS_ENV) or "").strip()
    profiles = document.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        refuse("category=pin_record_has_no_profiles")
    if not name:
        refuse(f"category=review_class_not_named variable={REVIEW_CLASS_ENV} "
               f"permitted={sorted(profiles)} — the profiles differ by more "
               "than an order of magnitude in cost cap; there is no safe "
               "default")
    if name not in REVIEW_CLASSES:
        refuse(f"category=review_class_unknown class={name!r} "
               f"permitted={sorted(REVIEW_CLASSES)} — the class is derived by "
               "preflight from the pull-request number; a name that is not one "
               "of the four did not come from there")
    if name not in profiles:
        refuse(f"category=review_class_has_no_profile class={name!r} "
               f"profiles={sorted(profiles)} — the governance document must "
               "carry a profile for every class the code can produce, or a "
               "real pull request refuses at spend time")

    overridden = document.get("profile_overridden_pins") or []
    ceilings = document.get("profile_transport_ceilings") or []
    selected = dict(document["pins"])
    for key in overridden:
        if key not in profiles[name]:
            refuse(f"category=profile_missing_an_overridden_pin "
                   f"profile={name!r} pin={key} — the document declares this "
                   "pin profile-specific and this profile does not set it, so "
                   "the run would silently spend under another profile's cap")
        selected[key] = profiles[name][key]

    transport = {}
    for key in ceilings:
        value = profiles[name].get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            refuse(f"category=profile_transport_ceiling_missing_or_invalid "
                   f"profile={name!r} ceiling={key} observed={value!r} — these "
                   "used to be free-standing workflow literals, and one of "
                   "them contradicted the approved generation cap. They live "
                   "in the profile now precisely so they cannot disagree with "
                   "it silently")
        transport[key] = value

    _assert_transport_ceilings_agree_with_pins(selected, transport, name=name)

    from .evidence import digest_of
    return selected, {
        "review_class": name,
        "overridden_pins": sorted(overridden),
        "max_count_calls": selected["VERIFIER_MAX_COUNT_CALLS"],
        "max_generation_calls": selected["VERIFIER_MAX_GENERATION_CALLS"],
        "cost_cap_micro_usd": selected["VERIFIER_COST_CAP_MICRO_USD"],
        "authorized_total_input_tokens":
            transport["authorized_total_input_tokens"],
        "generation_attempt_cap": transport["generation_attempt_cap"],
        "approved_by": profiles[name].get("approved_by"),
        # Bound into the evidence, so "which budget did this run spend under"
        # is answerable from the record rather than from the file as it stands
        # today.
        "profile_digest": digest_of({"review_class": name, "pins": selected,
                                     "transport": transport}),
    }


def _assert_transport_ceilings_agree_with_pins(pins: dict, transport: dict, *,
                                               name: str) -> None:
    """The transport may not be allowed fewer attempts than the approved calls.

    This is the check that would have caught `MIDTERM_GENERATION_ATTEMPT_CAP=60`
    against an approved `VERIFIER_MAX_GENERATION_CALLS=80`: the plan would have
    been priced for 80 and the panel would have stopped at 60, under a limit
    nobody approved, reported as an exhausted budget.

    A profile that forbids generating entirely is the one permitted asymmetry,
    and it is permitted in the safe direction: zero approved calls with a
    positive attempt cap can still never generate, because the engine refuses
    to plan a call it has no budget for."""
    approved_calls = pins["VERIFIER_MAX_GENERATION_CALLS"]
    cap = transport["generation_attempt_cap"]
    if approved_calls and cap < approved_calls:
        refuse(f"category=transport_cap_below_approved_generation_calls "
               f"profile={name!r} attempt_cap={cap} "
               f"approved_calls={approved_calls} — the plan would be priced "
               "for the approved number and the panel would stop at the lower "
               "one, under a limit no operator wrote")


def build_pin_record(*, engine: dict, pins: dict) -> dict:
    """The PIN record, built by the ENGINE's own honest constructor.

    `verifier.pins.test_pin_record` validates the values and labels the result
    `TEST_FIXTURE_UNAUTHORIZED` with `executable_authority=False` — which is
    exactly true of PIN values this repository authored and no operator
    approved.

    Deliberately not `operator_pin_claim`: that one carries a repository
    identity, an operator identity and an external anchor, and filling those in
    from values nobody signed would be this lane asserting an approval it does
    not have. The engine's own docstring makes the distinction — validation
    proves the values are internally coherent and proves nothing about who
    chose them — and the honest label is the one that says so."""
    from trustedlane import enginebridge
    model_ids = list(enginebridge.model_panel(engine))
    with enginebridge.engine_refusals(engine, where="build_pin_record"):
        return engine["modules"]["verifier.pins"].test_pin_record(
            pins, model_ids)


def load_challenge(environ: dict) -> str:
    """The unpredictable token that binds a verdict to this run.

    Read from a file rather than an environment variable so it does not sit in
    the process environment of every step, where a `set -x` or a crash dump
    would print it. A predictable challenge is one the models could re-derive,
    and a re-derivable challenge proves nothing about when a verdict was
    written."""
    challenge = _read_text(environ["MIDTERM_CHALLENGE_PATH"],
                           what="challenge").strip()
    if len(challenge) < MINIMUM_CHALLENGE_CHARS:
        refuse(f"category=challenge_too_short length={len(challenge)} "
               f"minimum={MINIMUM_CHALLENGE_CHARS} — the challenge is what "
               "proves a verdict was written for this run; a short one is "
               "guessable, and a guessable challenge proves nothing")
    return challenge


def load_authorizations(environ: dict, *, engine: dict, skeleton: dict):
    """The literal-clearance set, or an honest `None`.

    Constructed through the ENGINE's own `authority.LiteralAuthorizationSet`
    with the skeleton's own repository state, so the scoping rules are the
    engine's. What this function decides is only whether there is a set at all.

    When the variable is absent the answer is `None`, which the engine handles
    by recording `confers_real_call_authority=False`. A locally synthesised set
    would let the preflight manifest record a clearance nobody authorized, and
    that is the one thing worse than having none."""
    path = str(environ.get("MIDTERM_AUTHORIZATIONS_PATH") or "").strip()
    if not path:
        return None, {
            "literal_authorizations": None,
            "authority_class": None,
            "confers_real_call_authority": False,
            "honest_scope": (
                "this lane has no operator envelope authority, so no literal "
                "clearance was authorized for this run. Absence is recorded as "
                "absence; nothing was synthesised locally"),
        }
    records = _read_json(path, what="authorizations")
    if not isinstance(records, list):
        refuse("category=authorizations_not_a_list — the engine's loader "
               "requires a list of claims, each carrying a literal's SHA-256 "
               "and its scope and never the value")
    authority = engine["modules"]["verifier.authority"]
    state = skeleton["repository_state"]
    loaded = authority.LiteralAuthorizationSet(
        records,
        repository_identity=skeleton.get("repository_identity", "unknown"),
        target_base_sha=state["target_base_sha"],
        diff_base_sha=state["diff_base_sha"], head_sha=state["head_sha"])
    return loaded, {
        "literal_authorizations": loaded.digest(),
        "authority_class": loaded.authority_class,
        "confers_real_call_authority": bool(loaded.confers_real_call_authority),
        "claim_count": len(records),
        "honest_scope": ("claims loaded from a file this repository wrote. "
                         "They are scoped by the engine's own rules; they are "
                         "not an authenticated operator envelope"),
    }


def load_count_inputs(environ: dict) -> dict:
    """Everything the count path needs that is not the engine. One call."""
    assert_all_present(environ)
    loaded = load_pin_values(environ)
    return {"repository_path": load_repository_path(environ),
            "pin_values": loaded["pins"],
            "pin_profile": loaded["profile"],
            "transport_ceilings": {
                "authorized_total_input_tokens":
                    loaded["profile"]["authorized_total_input_tokens"],
                "generation_attempt_cap":
                    loaded["profile"]["generation_attempt_cap"]},
            "pin_authority": {k: loaded[k] for k in
                              ("authority_class", "approved_by", "approved_at")},
            "challenge": load_challenge(environ)}
