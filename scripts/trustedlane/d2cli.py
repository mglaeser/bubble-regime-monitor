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

from . import adapter, d2runtime, phases, signing, statuspublish, transport
from .d1cli import load_json_document
from .errors import refuse

REQUIRED_ENV = (
    "TARGET_BASE_SHA",
    "CANDIDATE_HEAD_SHA",
    "TRUSTED_ENGINE_ROOT",
    "TRUSTED_ENGINE_ARTIFACT_PATH",
    "TRUSTED_ENGINE_DIGEST",
    "TRUSTED_ENGINE_SOURCE_SHA256",
    "TRUSTED_PLAN_PATH",
    "TRUSTED_PLAN_SHA256",
    "TRUSTED_RUN_ID",
    "TRUSTED_RUN_ATTEMPT",
    "TRUSTED_RUN_URL",
    "OBSERVED_REF",
    "EVENT_REPOSITORY_ID",
    "TRUSTED_OBSERVATION_PATH",
    "TRUSTED_OPERATOR_RECORDS_PATH",
    "TRUSTED_MODEL_ID",
    "TRUSTED_OBSERVED_NOW",
)


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
    credential = transport.read_credential(phase=phases.D2)
    # The GENERATION bound, not the count lane's. A generation body is orders
    # of magnitude larger than a token count, and reading it through the count
    # lane's 64 KiB cap truncated it into a parse failure.
    opener = transport.open_https(phase=phases.D2,
                                  max_response_bytes=adapter.MAX_RESPONSE_BYTES)
    signing_key = signing.read_signing_key(phase=phases.D2)

    return d2runtime.run(
        observations=load_json_document(values["TRUSTED_OBSERVATION_PATH"],
                                        field="protected_state_observation"),
        operator_records=load_json_document(
            values["TRUSTED_OPERATOR_RECORDS_PATH"], field="operator_records"),
        engine_artifact={"path": values["TRUSTED_ENGINE_ARTIFACT_PATH"],
                         "expected_sha256": values["TRUSTED_ENGINE_DIGEST"],
                         "root": values["TRUSTED_ENGINE_ROOT"],
                         "search_path": None},
        candidate={"repository_numeric_id": values["EVENT_REPOSITORY_ID"],
                   "candidate_head_sha": values["CANDIDATE_HEAD_SHA"],
                   "target_base_sha": values["TARGET_BASE_SHA"]},
        # NOT rebuilt. D2 executes the plan D1 counted, and the digest is what
        # says it is that plan rather than a plan claiming to be.
        plan=load_json_document(values["TRUSTED_PLAN_PATH"],
                                field="trusted_plan"),
        trusted_plan_sha256=values["TRUSTED_PLAN_SHA256"],
        opener=opener,
        credential=credential,
        signing_key=signing_key,
        publisher=statuspublish.publish,
        trusted_run={"id": values["TRUSTED_RUN_ID"],
                     "attempt": values["TRUSTED_RUN_ATTEMPT"],
                     "url": values["TRUSTED_RUN_URL"]},
        observed_now=values["TRUSTED_OBSERVED_NOW"],
        produced_at=values["TRUSTED_OBSERVED_NOW"],
        model=values["TRUSTED_MODEL_ID"],
        engine_source_sha256=values["TRUSTED_ENGINE_SOURCE_SHA256"])
