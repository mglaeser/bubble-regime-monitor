"""Assemble D2's arguments from the runner environment and run the lane.

EX3-R01, the generation half. Same structure as `d1cli` and deliberately a
separate module, for the same reason `d2runtime` is separate from `d1runtime`:
the inputs differ in the ways that matter, and one assembler with a phase
argument would put "which secret, which environment, which approval" inside a
branch.

The differences are not cosmetic. D2 reads a DIFFERENT secret, from a DIFFERENT
protected environment, under a DIFFERENT operator prerequisite, and it does not
rebuild a plan at all — it executes the plan D1 counted, identified by digest.
Sharing an assembler would mean one of those four could be wrong while the code
still looked symmetric.
"""

from __future__ import annotations

import os

from . import (
    CANONICAL_REPOSITORY,  # noqa: F401
    adapter,
    candidatefetch,
    d2runtime,
    enginebridge,
    phases,
    protectedstate,
    signing,
    statuspublish,
    transport,
    trustedverifier,
)
from .d1cli import (
    load_engine_identity,
    load_json_document,
    observe_bootstrap,
)
from .errors import refuse

REQUIRED_ENV = (
    "TARGET_BASE_SHA",
    "CANDIDATE_HEAD_SHA",
    "TRUSTED_ENGINE_ROOT",
    "TRUSTED_ENGINE_ARTIFACT_PATH",
    "TRUSTED_ENGINE_DIGEST",
    # The five approved digests, as the protected build published them.
    "TRUSTED_ENGINE_IDENTITY_PATH",
    # Which deployment of the trusted lane is running. Prerequisite 15 approves
    # a branch, a head and a policy digest; EX5-R21 compares all three.
    "TRUSTED_BOOTSTRAP_HEAD_SHA",
    "TRUSTED_WORKFLOW_DIR",
    "TRUSTED_PLAN_PATH",
    "TRUSTED_COUNT_EVIDENCE_PATH",
    "TRUSTED_CANDIDATE_CHECKOUT",
    "TRUSTED_RUN_ID",
    "TRUSTED_RUN_ATTEMPT",
    "TRUSTED_RUN_URL",
    "OBSERVED_REF",
    "EVENT_REPOSITORY_ID",
    "TRUSTED_OBSERVATION_PATH",
    "TRUSTED_OPERATOR_RECORDS_PATH",
    "TRUSTED_REVOCATION_LIST_PATH",
    "TRUSTED_OBSERVED_NOW",
)

#: REMOVED. `TRUSTED_MODEL_ID` let the runner name ONE model, so a check
#: published as `trusted-cross-vendor-review` asked a single model and the
#: runner chose which. The panel is `verifier.policy.REQUESTED_MODEL_IDS` and
#: no environment variable can change it.
#: `TRUSTED_PLAN_SHA256` is retired with `TRUSTED_MODEL_ID`. A digest handed to
#: D2 alongside a plan is not a binding: the digest identifies a plan, and
#: whoever supplies the plan chooses which plan gets identified. D2 now reads
#: the digest out of the document D1 signed.
#: `TRUSTED_ENGINE_SOURCE_SHA256` is retired for a third reason: the engine
#: identity record already carries `engine_source_sha256`, and the runtime
#: binding gate compares THAT one against the operator's approval. A separate
#: environment variable holding the same digest is a second copy nothing
#: reconciles — the runner could set one value while the approved record named
#: another, and the adapter identity would report the runner's.
RETIRED_ENV = ("TRUSTED_MODEL_ID", "TRUSTED_PLAN_SHA256",
               "TRUSTED_ENGINE_SOURCE_SHA256")


def read_environment(environ=None) -> dict:
    source = environ if environ is not None else os.environ
    missing = [name for name in REQUIRED_ENV if not source.get(name)]
    if missing:
        refuse(f"category=d2_environment_incomplete missing={missing} — each "
               "of these is an input the lane cannot derive, and a default "
               "for any of them is a check that quietly stopped running")
    values = {name: source[name] for name in REQUIRED_ENV}
    for name in ("TRUSTED_RUN_ID", "TRUSTED_RUN_ATTEMPT",
                 "EVENT_REPOSITORY_ID"):
        try:
            values[name] = int(values[name])
        except ValueError:
            refuse(f"category=d2_environment_not_an_integer field={name}")
    return values


def main(environ=None) -> dict:
    """The whole D2 activation path. Refuses before generating, every time."""
    phases.assert_phase_permitted(phases.D2)

    values = read_environment(environ)
    # EX4-R11. The credential used to be read HERE, above the protected-state
    # observation — so a run on an unprotected ref pulled the provider key into
    # the process and only THEN asked whether a key was allowed to be there.
    # `d2runtime.run` asserts readiness as its first step, which was true and did not
    # help: by the time step 1 refused, the secret was already in the
    # environment of a process the refusal does not unwind.
    #
    # Readiness is now established before any capability is obtained. `d2runtime.run`
    # still asserts it — that is not redundant, because `d2runtime.run` takes the
    # capabilities as parameters and must not depend on its caller having
    # checked.
    observations = load_json_document(values["TRUSTED_OBSERVATION_PATH"],
                                      field="protected_state_observation")
    protectedstate.assert_ready_for_credential(**observations)

    credential = transport.read_credential(phase=phases.D2)
    # The GENERATION bound, not the count lane's. A generation body is orders
    # of magnitude larger than a token count, and reading it through the count
    # lane's 64 KiB cap truncated it into a parse failure.
    opener = transport.open_https(phase=phases.D2,
                                  max_response_bytes=adapter.MAX_RESPONSE_BYTES)
    signing_key = signing.read_signing_key(phase=phases.D2)

    engine = enginebridge.load_engine(values["TRUSTED_ENGINE_ROOT"])
    lane_verifier = trustedverifier.bind(
        engine,
        trust_store=trustedverifier.load_trust_store(phase=phases.D2),
        occurrence={"repository_identity": CANONICAL_REPOSITORY,
                    "repository_numeric_id": values["EVENT_REPOSITORY_ID"],
                    "target_base_sha": values["TARGET_BASE_SHA"],
                    "diff_base_sha": values["TARGET_BASE_SHA"],
                    "head_sha": values["CANDIDATE_HEAD_SHA"]},
        observed_now=values["TRUSTED_OBSERVED_NOW"],
        revocations=load_json_document(values["TRUSTED_REVOCATION_LIST_PATH"],
                                       field="revocation_list"))

    engine_identity = load_engine_identity(
        values["TRUSTED_ENGINE_IDENTITY_PATH"])
    return d2runtime.run(
        observations=observations,
        operator_claims=load_json_document(
            values["TRUSTED_OPERATOR_RECORDS_PATH"], field="operator_records"),
        lane_verifier=lane_verifier,
        engine=engine,
        engine_artifact={"path": values["TRUSTED_ENGINE_ARTIFACT_PATH"],
                         "expected_sha256": values["TRUSTED_ENGINE_DIGEST"],
                         "root": values["TRUSTED_ENGINE_ROOT"],
                         "search_path": None},
        engine_identity=engine_identity,
        bootstrap=observe_bootstrap(values),
        candidate={"repository_numeric_id": values["EVENT_REPOSITORY_ID"],
                   "candidate_head_sha": values["CANDIDATE_HEAD_SHA"],
                   "target_base_sha": values["TARGET_BASE_SHA"],
                   "checkout_destination": values["TRUSTED_CANDIDATE_CHECKOUT"]},
        # D1's two SIGNED documents. D2 verifies both signatures before it
        # reads either — it used to take a plan and a digest and check they
        # matched, which is not a binding: the digest identifies a plan, and
        # whoever hands D2 the plan chooses which plan gets identified.
        count_evidence=load_json_document(values["TRUSTED_COUNT_EVIDENCE_PATH"],
                                          field="count_evidence"),
        signed_plan=load_json_document(values["TRUSTED_PLAN_PATH"],
                                       field="signed_executable_plan"),
        # The real inert fetch. D2 needs the repository because the plan stores
        # request HASHES, not bodies, so the prompt bytes are earned again from
        # the commits.
        fetch=candidatefetch.fetch_candidate,
        opener=opener,
        credential=credential,
        signing_key=signing_key,
        publisher=statuspublish.publish,
        trusted_run={"id": values["TRUSTED_RUN_ID"],
                     "attempt": values["TRUSTED_RUN_ATTEMPT"],
                     "url": values["TRUSTED_RUN_URL"]},
        observed_now=values["TRUSTED_OBSERVED_NOW"],
        produced_at=values["TRUSTED_OBSERVED_NOW"],
        # No `model=`. The panel is governed policy, from the engine.
        # From the approved identity record, which is the digest the runtime
        # binding gate compares against the operator's approval. A separate
        # environment variable for the same fact is a second copy nothing
        # reconciles.
        engine_source_sha256=engine_identity["engine_source_sha256"])
