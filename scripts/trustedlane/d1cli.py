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

from . import (
    CANONICAL_REPOSITORY,  # noqa: F401
    candidatefetch,
    d1runtime,
    enginebridge,
    phases,
    signing,
    statuspublish,
    transport,
    trustedverifier,
)
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
    # The operator's revocation list. Required, not optional: "no list" is not
    # "no revocations", and a lane that treats a missing list as empty cannot
    # be told to stop.
    "TRUSTED_REVOCATION_LIST_PATH",
    "TRUSTED_OBSERVED_NOW",
)

#: REMOVED, and the removal is the fix. `TRUSTED_MODEL_ID` let the runner name
#: ONE model, so the trusted lane counted a single-model review while governed
#: policy is a three-model panel. The panel now comes from the engine's own
#: `policy.REQUESTED_MODEL_IDS` and no environment variable can change it.
RETIRED_ENV = ("TRUSTED_MODEL_ID",)


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


def load_engine(*, engine_root: str) -> dict:
    """Load the approved engine, VIA THE BRIDGE, before the lane runs.

    This used to import `verifier.plan` directly — and it imported
    `build_review_skeleton`, which does not exist. It was then wrapped in a
    LAZY builder, because `assert_no_candidate_import` refused whenever
    `verifier` was in `sys.modules`, so an eager load made the lane refuse on
    every invocation.

    Both workarounds are gone. That check asked a name question standing in for
    an origin question; it now asks the origin question, and the engine IS
    `scripts/verifier` inside the approved artifact. So the engine loads here,
    once, and `d1runtime` step 2 verifies that every `verifier` module in the
    process came out of the root it was told to expect."""
    enginebridge.assert_layout(engine_root)
    return enginebridge.load_engine(engine_root)


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
    engine = load_engine(engine_root=values["TRUSTED_ENGINE_ROOT"])
    candidate_plan = load_json_document(values["TRUSTED_CANDIDATE_PLAN_PATH"],
                                        field="candidate_plan")
    revocations = load_json_document(values["TRUSTED_REVOCATION_LIST_PATH"],
                                     field="revocation_list")

    # The trust store is phase-gated for the same reason the credential is: it
    # decides what counts as authorized, and a lane that can read one outside
    # D1/D2 can authorize itself there.
    lane_verifier = trustedverifier.bind(
        engine,
        trust_store=trustedverifier.load_trust_store(phase=phases.D1),
        occurrence={"repository_identity": CANONICAL_REPOSITORY,
                    "repository_numeric_id": values["EVENT_REPOSITORY_ID"],
                    "target_base_sha": values["TARGET_BASE_SHA"],
                    "diff_base_sha": values["TARGET_BASE_SHA"],
                    "head_sha": values["CANDIDATE_HEAD_SHA"]},
        observed_now=values["TRUSTED_OBSERVED_NOW"],
        revocations=revocations)

    return d1runtime.run(
        observations=observations,
        operator_claims=operator_records,
        lane_verifier=lane_verifier,
        engine=engine,
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
        produced_at=values["TRUSTED_OBSERVED_NOW"])
