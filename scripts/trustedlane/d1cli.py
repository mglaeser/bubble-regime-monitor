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
    bootstrapstate,
    candidatefetch,
    d1runtime,
    enginebridge,
    phases,
    protectedstate,
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
    # The five approved digests, as the protected build published them. Four
    # describe how the artifact was made and cannot be recomputed from it; the
    # fifth is checked against the archive this run opens.
    "TRUSTED_ENGINE_IDENTITY_PATH",
    # Which deployment of the trusted lane is running. Prerequisite 15 approves
    # a branch, a head and a policy digest, and EX5-R21 compares all three
    # against the deployment that actually dispatched.
    "TRUSTED_BOOTSTRAP_HEAD_SHA",
    "TRUSTED_WORKFLOW_DIR",
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
    # R04/R13 + R06. Where the two SIGNED documents are written so the retain
    # steps have files to upload and D2 has documents to download. Required,
    # not optional: a run that produced a plan and wrote it nowhere is a run
    # whose count can never be executed, and the failure would look like D2
    # being misconfigured.
    "TRUSTED_PLAN_OUTPUT_PATH",
    "TRUSTED_COUNT_EVIDENCE_OUTPUT_PATH",
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


def load_engine_identity(path: str) -> dict:
    """The five digests the protected engine build published.

    Read from a file the BUILD produced, not derived here. Four of the five —
    source, lock, SBOM, provenance — describe how the artifact was made, and a
    process that computed them from the artifact it is holding would be
    certifying its own inputs. The fifth, the artifact digest, IS verified
    here, by `enginebridge.assert_identity_is_this_engine` against the archive
    this run opened.

    Shape-checked at the boundary so a truncated or half-written record refuses
    with a name rather than a `KeyError` three layers down."""
    record = load_json_document(path, field="engine_identity")
    if not isinstance(record, dict):
        refuse("category=engine_identity_not_an_object")
    missing = [f for f in enginebridge.ENGINE_IDENTITY_FIELDS
               if not isinstance(record.get(f), str) or len(record[f]) != 64]
    if missing:
        refuse(f"category=engine_identity_incomplete fields={missing} — the "
               "operator approves five digests, so a run that can state fewer "
               "is asking them to approve something it cannot check")
    return {f: record[f] for f in enginebridge.ENGINE_IDENTITY_FIELDS}


def observe_bootstrap(values: dict) -> dict:
    """Which deployment of the trusted lane this run is running FROM.

    Prerequisite 15 approves a branch, a head and a policy. EX5-R21 compares
    them against the deployment actually running, so the deployment has to be
    stated — and two of the three come from the platform, which is recorded
    in the observation rather than smoothed over.

    `OBSERVED_REF` is reused deliberately. It is `github.ref`, the same
    platform fact `protectedstate` reads, and minting a second name for one
    value is how two copies of it end up disagreeing."""
    return bootstrapstate.observe(
        ref=values["OBSERVED_REF"],
        head_sha=values["TRUSTED_BOOTSTRAP_HEAD_SHA"],
        workflow_dir=values["TRUSTED_WORKFLOW_DIR"],
        implemented_phase=phases.IMPLEMENTED_PHASE)


def write_signed_documents(result: dict, values: dict) -> dict:
    """Persist D1's two signed documents, as ENVELOPE plus PAYLOAD pairs.

    Both halves, because the envelope carries `payload_sha256` and not the
    payload: a stored envelope alone proves a signature over bytes nobody
    kept, and a stored payload alone is a document with no signature. D2's
    `_load_signed_plan` verifies the signature and then
    `assert_payload_matches`, which needs exactly this pair.

    The plan file is PRIVATE and stays private: it carries the exact prompt
    bytes of every request. The workflow uploads it as a retained artifact with
    a short retention, never as a release asset and never anywhere a pull
    request can see."""
    written = {}
    for key, path_var in (
            ("executable_plan", "TRUSTED_PLAN_OUTPUT_PATH"),
            ("count_evidence", "TRUSTED_COUNT_EVIDENCE_OUTPUT_PATH")):
        document = result.get(key)
        if not isinstance(document, dict) or "envelope" not in document or (
                "payload" not in document):
            refuse(f"category=signed_document_missing document={key} — the run "
                   "reported success and produced no document to retain")
        with open(values[path_var], "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
        written[key] = values[path_var]
    return written


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
    # EX4-R11. The credential used to be read HERE, above the protected-state
    # observation — so a run on an unprotected ref pulled the provider key into
    # the process and only THEN asked whether a key was allowed to be there.
    # `run` asserts readiness as its first step, which was true and did not
    # help: by the time step 1 refused, the secret was already in the
    # environment of a process the refusal does not unwind.
    #
    # Readiness is now established before any capability is obtained. `run`
    # still asserts it — that is not redundant, because `run` takes the
    # capabilities as parameters and must not depend on its caller having
    # checked.
    observations = load_json_document(values["TRUSTED_OBSERVATION_PATH"],
                                      field="protected_state_observation")
    protectedstate.assert_ready_for_credential(**observations)

    credential = transport.read_credential(phase=phases.D1)
    opener = transport.open_https(phase=phases.D1)
    signing_key = signing.read_signing_key(phase=phases.D1)

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

    result = d1runtime.run(
        observations=observations,
        operator_claims=operator_records,
        lane_verifier=lane_verifier,
        engine=engine,
        engine_artifact={"path": values["TRUSTED_ENGINE_ARTIFACT_PATH"],
                         "expected_sha256": values["TRUSTED_ENGINE_DIGEST"],
                         "root": values["TRUSTED_ENGINE_ROOT"],
                         "search_path": None},
        engine_identity=load_engine_identity(
            values["TRUSTED_ENGINE_IDENTITY_PATH"]),
        bootstrap=observe_bootstrap(values),
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
    # After the run, never before: a refusal must leave no file behind that a
    # retain step would upload under a name meaning "D1 counted this".
    write_signed_documents(result, values)
    return result
