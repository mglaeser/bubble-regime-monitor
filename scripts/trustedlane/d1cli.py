"""Assemble D1's arguments from the runner environment and run the lane.

EX3-R01. `d1runtime.run` takes every capability and every observation as a
parameter, which is what makes the whole sequence testable against a fake
server. Something has to build those parameters on a real runner, and that
something is here rather than inline in the workflow YAML — a heredoc is not a
place where refusals can be tested, and the last version of this file's job was
a heredoc containing `exit 1`.

**What this does NOT invent.** Three inputs cannot be derived from the runner:

    the protected-state observation   needs repository administration reads
    the status publisher              needs an installation token with Statuses
                                      or Checks write
    the skeleton builder              lives in the candidate verifier package,
                                      which reaches this machine only inside
                                      the verified engine artifact

Each is loaded from an explicit source and refuses if it is absent. None of
them is defaulted, because a default is how one of them quietly stops being
checked — the observation would become "assume protected", the publisher would
become a no-op that leaves the pull request pending forever, and the builder
would fall back to the candidate's own plan, which is the exact thing the
rebuild exists to avoid.

The publisher in particular refuses today, deliberately:
`statuspublish.publish` explains that publishing needs a token this branch must
not hold. That refusal is not a placeholder standing in for missing code — the
request shape, its validation and the pending/terminal ordering are implemented
and tested. What is missing is a credential, and saying so precisely is more
useful than `exit 1`.
"""

from __future__ import annotations

import json
import os

from . import candidatefetch, d1runtime, phases, signing, statuspublish, transport
from .errors import refuse

REQUIRED_ENV = (
    "TARGET_BASE_SHA",
    "CANDIDATE_HEAD_SHA",
    "TRUSTED_ENGINE_ROOT",
    "TRUSTED_ENGINE_ARTIFACT_PATH",
    "TRUSTED_ENGINE_DIGEST",
    "TRUSTED_RUN_ID",
    "TRUSTED_RUN_ATTEMPT",
    "TRUSTED_RUN_URL",
    "OBSERVED_REF",
    "EVENT_REPOSITORY_ID",
    "TRUSTED_OBSERVATION_PATH",
    "TRUSTED_OPERATOR_RECORDS_PATH",
    # The CANDIDATE's declared plan, which is candidate data and is read from
    # its own path. An earlier version read it from inside the engine artifact,
    # which gets the provenance exactly backwards: the engine is the trusted
    # side, and putting the reviewed branch's claims inside it would have made
    # the rebuild comparison compare the engine against itself.
    "TRUSTED_CANDIDATE_PLAN_PATH",
    "TRUSTED_CANDIDATE_CHECKOUT",
    "TRUSTED_MODEL_ID",
    "TRUSTED_OBSERVED_NOW",
)


def read_environment(environ=None) -> dict:
    """Every input named, and a missing one refused rather than defaulted."""
    source = environ if environ is not None else os.environ
    missing = [name for name in REQUIRED_ENV if not source.get(name)]
    if missing:
        refuse(f"category=d1_environment_incomplete missing={missing} — each "
               "of these is an input the lane cannot derive, and a default "
               "for any of them is a check that quietly stopped running")
    values = {name: source[name] for name in REQUIRED_ENV}
    for name in ("TRUSTED_RUN_ID", "TRUSTED_RUN_ATTEMPT",
                 "EVENT_REPOSITORY_ID"):
        try:
            values[name] = int(values[name])
        except ValueError:
            refuse(f"category=d1_environment_not_an_integer field={name}")
    return values


def load_json_document(path: str, *, field: str):
    """Read one operator-supplied document, or refuse naming which one."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        refuse(f"category=d1_input_unreadable field={field} "
               f"exception_class={type(exc).__name__} — the path is not "
               "reported; it can carry a runner temp directory layout")
    except json.JSONDecodeError as exc:
        refuse(f"category=d1_input_not_json field={field} "
               f"exception_class={type(exc).__name__}")


def load_skeleton_builder(*, engine_root: str):
    """Return a LAZY loader for the plan builder in the verified artifact.

    Lazy, and that is the whole point. The eager version imported
    `verifier.plan` here, before `d1runtime.run` was called — and step 4 of that
    run is `enginepolicy.assert_no_candidate_import()`, which refuses when
    `verifier` is in `sys.modules`. So the D1 lane refused on every single
    invocation, always, at the gate that exists to catch the candidate checkout
    leaking in. Found by adversarial review; the lane could never have run.

    The underlying tension is real rather than a slip. `verifier` is candidate
    code in D0 — where any import of it means the artifact under review reached
    the process — and it is TRUSTED code in D1, where it arrives inside the
    operator-approved artifact. The same module name means opposite things
    depending on where it was loaded from, so the isolation check cannot be a
    name check once the artifact is unpacked.

    Deferring the import to first call puts it after step 4, where the check is
    still asking the right question, and `_assert_loaded_from` then answers the
    question that actually matters in D1: not "is `verifier` imported" but "did
    this `verifier` come out of the approved artifact"."""
    import os

    from .enginepolicy import assert_not_candidate_checkout

    assert_not_candidate_checkout(engine_root)
    resolved_root = os.path.realpath(engine_root)
    state = {}

    def builder(**kwargs):
        if "fn" not in state:
            state["fn"] = _import_planner(resolved_root)
        return state["fn"](**kwargs)

    return builder


def _import_planner(resolved_root: str):
    """Import the planner and prove it came out of the approved artifact."""
    import sys

    if resolved_root not in sys.path:
        sys.path.insert(0, resolved_root)
    try:
        from verifier.plan import build_review_skeleton  # noqa: PLC0415
    except ImportError as exc:
        refuse(f"category=engine_artifact_has_no_planner "
               f"exception_class={type(exc).__name__} — the approved artifact "
               "does not contain the verifier planner, and falling back to the "
               "candidate's own plan would let the reviewed branch choose what "
               "gets reviewed")
    _assert_loaded_from(resolved_root)
    return build_review_skeleton


def _assert_loaded_from(resolved_root: str) -> dict:
    """`verifier` must resolve INSIDE the approved artifact, not beside it.

    A name check cannot answer this — `import verifier` succeeds identically
    whether the package came from the operator-approved tarball or from the
    candidate clone sitting on the same disk. Comparing the loaded module's
    real path against the artifact root is the only thing that distinguishes
    them, and it is the check that has to hold once the artifact is unpacked."""
    import os
    import sys

    module = sys.modules.get("verifier")
    origin = getattr(module, "__file__", None)
    if origin is None:
        # A namespace package has no __file__; its search locations do. An
        # empty directory is a valid namespace root, which is exactly how the
        # candidate tree got imported once before.
        locations = list(getattr(getattr(module, "__path__", None), "_path", [])
                         or getattr(module, "__path__", []) or [])
        if not locations:
            refuse("category=planner_origin_unknown — a module with no file "
                   "and no search path cannot be traced to an artifact")
        outside = [p for p in locations
                   if not os.path.realpath(p).startswith(resolved_root + os.sep)]
        if outside:
            refuse(f"category=planner_loaded_outside_the_artifact "
                   f"count={len(outside)} — `verifier` resolved to a namespace "
                   "root that is not inside the approved engine; the candidate "
                   "clone is on the same disk and imports identically")
        return {"planner_inside_artifact": True, "namespace_package": True}
    if not os.path.realpath(origin).startswith(resolved_root + os.sep):
        refuse("category=planner_loaded_outside_the_artifact — `verifier` was "
               "imported from somewhere other than the approved engine root; "
               "the path is not reported because it can carry the runner "
               "layout")
    return {"planner_inside_artifact": True, "namespace_package": False}


def main(environ=None) -> dict:
    """The whole D1 activation path. Refuses before spending, every time."""
    # Two deliberate acts, not one: renaming the template deploys the workflow,
    # and raising IMPLEMENTED_PHASE in a protected commit deploys the phase.
    # This refuses until both have happened.
    phases.assert_phase_permitted(phases.D1)

    values = read_environment(environ)
    credential = transport.read_credential(phase=phases.D1)
    opener = transport.open_https(phase=phases.D1)
    signing_key = signing.read_signing_key(phase=phases.D1)

    observations = load_json_document(values["TRUSTED_OBSERVATION_PATH"],
                                      field="protected_state_observation")
    operator_records = load_json_document(
        values["TRUSTED_OPERATOR_RECORDS_PATH"], field="operator_records")
    builder = load_skeleton_builder(engine_root=values["TRUSTED_ENGINE_ROOT"])
    candidate_plan = load_json_document(values["TRUSTED_CANDIDATE_PLAN_PATH"],
                                        field="candidate_plan")

    return d1runtime.run(
        observations=observations,
        operator_records=operator_records,
        engine_artifact={"path": values["TRUSTED_ENGINE_ARTIFACT_PATH"],
                         "expected_sha256": values["TRUSTED_ENGINE_DIGEST"],
                         "root": values["TRUSTED_ENGINE_ROOT"],
                         "search_path": None},
        candidate={"repository_numeric_id": values["EVENT_REPOSITORY_ID"],
                   "candidate_head_sha": values["CANDIDATE_HEAD_SHA"],
                   "target_base_sha": values["TARGET_BASE_SHA"],
                   "checkout_destination": values["TRUSTED_CANDIDATE_CHECKOUT"]},
        plan=candidate_plan,
        # The real inert fetch: `--no-checkout`, from the remote fixed in
        # trusted policy, with no caller-supplied repository.
        fetch=candidatefetch.fetch_candidate,
        opener=opener,
        credential=credential,
        signing_key=signing_key,
        # Refuses today, and says exactly why: publishing needs an installation
        # token held by the approved integration and never by a reviewed
        # branch. The request shape and ordering are implemented and tested.
        publisher=statuspublish.publish,
        trusted_run={"id": values["TRUSTED_RUN_ID"],
                     "attempt": values["TRUSTED_RUN_ATTEMPT"],
                     "url": values["TRUSTED_RUN_URL"]},
        observed_now=values["TRUSTED_OBSERVED_NOW"],
        produced_at=values["TRUSTED_OBSERVED_NOW"],
        # `builder` imports the planner on FIRST CALL, which happens at step 7
        # — after step 4's candidate-isolation check has run.
        skeleton_rebuild=builder,
        model=values["TRUSTED_MODEL_ID"])
