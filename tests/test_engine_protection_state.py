"""X4 — the engine identity must not claim native branch protection it lacks.

Run 31286479960 produced a byte-reproducible artifact whose own
`engine-identity.json` said `state = PROTECTED_ENGINE_IDENTITY`, and whose
provenance `honest_scope` said the workflow "runs only on a protected ref".
GitHub reported `main` protected=false. The workflow had proved
`refs/heads/main` and nothing else, and those are different facts.

The defect is not that the wrong string was written. It is that the string was
a CONSTANT: no input to the build could ever have made it say anything else, so
there was no state of the world in which the record would have been accurate.
These tests exist to keep the state derived from a platform fact, and to keep
the two lanes from accepting each other's grade of evidence.
"""

from __future__ import annotations

import json

import pytest
from midtermpanel import COUNT_EVIDENCE_CLASS, PANEL_EVIDENCE_CLASS
from midtermpanel import engine as midterm_engine
from midtermpanel.errors import PanelRefusal
from trustedlane import enginebridge, enginebuild
from trustedlane.errors import LaneRefusal

ROLES = {"protected_trusted_lane": "e" * 40, "candidate_verifier": "c" * 40}


def _context(protected, **overrides):
    context = {
        "build_workflow_run_id": 31286479960,
        "build_workflow_run_attempt": 1,
        "build_ref": "refs/heads/main",
        "build_head_sha": "e" * 40,
        "runner_image_digest": "Linux-X64-github-hosted",
        "repository_numeric_id": 1297332828,
    }
    if protected is not ...:
        context["build_ref_protected"] = protected
    context.update(overrides)
    return context


# ---------------------------------------------------------------- 1 + 2 ----
# The two honest outcomes. Both must be reachable; before the correction only
# one string was.

def test_unprotected_ref_emits_midterm_identity_and_names_its_control():
    record = enginebuild.provenance_record(_context("false"), roles=ROLES)

    assert record["build_ref_protected"] is False
    assert record["native_branch_protection"] is False
    assert record["control_class"] == enginebuild.CONTROL_HUMAN_EXACT_HEAD
    assert enginebuild.STATE_FOR_CONTROL[record["control_class"]] == (
        enginebuild.MIDTERM_STATE)


def test_unprotected_provenance_makes_no_protected_ref_claim():
    """The specific sentence that was false, and its neighbours."""
    scope = enginebuild.provenance_record(
        _context("false"), roles=ROLES)["honest_scope"].lower()

    assert "runs only on a protected ref" not in scope
    assert "survived branch protection" not in scope
    assert "protected build" not in scope
    assert "native branch protection was not active" in scope
    assert "not protected-ref or write-separated trusted evidence" in scope


def test_protected_ref_emits_protected_identity():
    record = enginebuild.provenance_record(_context("true"), roles=ROLES)

    assert record["build_ref_protected"] is True
    assert record["native_branch_protection"] is True
    assert record["control_class"] == enginebuild.CONTROL_NATIVE_PROTECTED_REF
    assert enginebuild.STATE_FOR_CONTROL[record["control_class"]] == (
        enginebuild.PROTECTED_STATE)
    assert "native branch protection was active" in record[
        "honest_scope"].lower()


# -------------------------------------------------------------------- 3 ----

def test_missing_protection_input_refuses_rather_than_defaulting():
    """Absent is not False.

    Defaulting to False would be safe today and would also mean a workflow
    that forgot the input silently produced a mid-term identity that looked
    deliberate. The build should stop."""
    with pytest.raises(LaneRefusal) as excinfo:
        enginebuild.provenance_record(_context(...), roles=ROLES)

    assert "build_ref_protection_not_supplied" in str(excinfo.value)


def test_protection_is_not_folded_into_the_truthiness_check():
    """`PROVENANCE_FIELDS` is validated with `if not context.get(f)`.

    Putting the protection field in that tuple would report an honest `False`
    as a missing field and refuse every unprotected build — the correction
    would then look like a platform outage."""
    assert enginebuild.PROTECTION_FIELD not in enginebuild.PROVENANCE_FIELDS
    enginebuild.provenance_record(_context(False), roles=ROLES)


# -------------------------------------------------------------------- 4 ----

@pytest.mark.parametrize("value", ["True", "TRUE", "False", "FALSE", "1", "0",
                                   "yes", "no", "", " true", "true ", None,
                                   1, 0, [], {}])
def test_only_the_platform_renderings_are_accepted(value):
    """`bool("false")` is True. A lenient parser here is the whole defect."""
    with pytest.raises(LaneRefusal):
        enginebuild.assert_ref_protected(value)


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False),
                                            (True, True), (False, False)])
def test_supported_forms_parse(value, expected):
    assert enginebuild.assert_ref_protected(value) is expected


# -------------------------------------------------------------------- 7 ----

def test_flipping_protection_after_the_build_breaks_the_seal():
    """The mutation the governance-note approach would have invited."""
    provenance = enginebuild.provenance_record(_context("false"), roles=ROLES)
    sealed = provenance["provenance_sha256"]

    tampered = dict(provenance)
    tampered["build_ref_protected"] = True
    tampered["native_branch_protection"] = True
    tampered["control_class"] = enginebuild.CONTROL_NATIVE_PROTECTED_REF

    assert enginebuild.recomputed_provenance_digest(tampered) != sealed


def test_reseal_check_catches_a_consistently_edited_record():
    """Both stored copies of the digest can be left equal and both wrong.

    Comparing them to each other passes; only recomputing notices. This is why
    the protection fields are inside the digested blob."""
    record = _built_record(protected="false")
    record["provenance"]["native_branch_protection"] = True

    with pytest.raises(LaneRefusal) as excinfo:
        enginebuild.assert_built_record(record)

    assert "does_not_reseal" in str(excinfo.value)


def _built_record(*, protected):
    """A syntactically complete identity record, without a git tree."""
    provenance = enginebuild.provenance_record(_context(protected),
                                               roles=ROLES)
    state = enginebuild.STATE_FOR_CONTROL[provenance["control_class"]]
    return {
        "state": state,
        "build_ref_protected": provenance["build_ref_protected"],
        "native_branch_protection": provenance["native_branch_protection"],
        "control_class": provenance["control_class"],
        "repository_numeric_id": 1297332828,
        "engine_artifact_sha256": "9" * 64,
        "engine_source_sha256": "2" * 64,
        "runtime_lock_sha256": "1" * 64,
        "sbom_sha256": "f" * 64,
        "provenance_sha256": provenance["provenance_sha256"],
        "provenance": provenance,
    }


# -------------------------------------------------------------------- 8 ----

def test_strict_loader_requires_the_protection_fields():
    record = _built_record(protected="false")
    del record["build_ref_protected"]

    with pytest.raises(LaneRefusal) as excinfo:
        enginebuild.assert_built_record(record)

    assert "build_ref_protection_not_supplied" in str(excinfo.value)


def test_strict_loader_refuses_state_that_contradicts_its_control_class():
    record = _built_record(protected="false")
    record["state"] = enginebuild.PROTECTED_STATE

    with pytest.raises(LaneRefusal) as excinfo:
        enginebuild.assert_built_record(record)

    assert "state_control_mismatch" in str(excinfo.value)


def test_strict_loader_refuses_header_that_disagrees_with_its_provenance():
    record = _built_record(protected="false")
    record["native_branch_protection"] = True
    record["build_ref_protected"] = True

    with pytest.raises(LaneRefusal) as excinfo:
        enginebuild.assert_built_record(record)

    assert "control_class_contradicts_protection" in str(excinfo.value)


def test_a_well_formed_midterm_record_still_loads():
    """Red-to-green: the strictness must not refuse the honest record."""
    assert enginebuild.assert_built_record(
        _built_record(protected="false"))["state"] == (
            enginebuild.MIDTERM_STATE)


# -------------------------------------------------------------------- 5 ----

def test_trusted_lane_rejects_the_midterm_identity():
    with pytest.raises(LaneRefusal) as excinfo:
        enginebridge.assert_identity_is_trusted_grade(
            _built_record(protected="false"))

    assert "trusted_lane_refuses_midterm_engine_identity" in str(excinfo.value)


def test_trusted_lane_accepts_a_genuinely_protected_identity():
    accepted = enginebridge.assert_identity_is_trusted_grade(
        _built_record(protected="true"))

    assert accepted["native_branch_protection"] is True
    assert accepted["control_class"] == enginebuild.CONTROL_NATIVE_PROTECTED_REF


def test_trusted_lane_rejects_a_protected_state_over_unprotected_facts():
    """The forged combination, reaching the consumer rather than the builder."""
    record = _built_record(protected="false")
    record["state"] = enginebuild.PROTECTED_STATE

    with pytest.raises(LaneRefusal) as excinfo:
        enginebridge.assert_identity_is_trusted_grade(record)

    assert "not_protected" in str(excinfo.value) or "wrong_control" in str(
        excinfo.value)


# -------------------------------------------------------------------- 6 ----

APPROVED_RELEASE_CONFIG = {
    "approved_engine_source_sha": "c" * 40,
    "approved_engine_protected_sha": "e" * 40,
    "approved_engine_artifact_sha256": "9" * 64,
    "approved_engine_release_tag": "midterm-panel-engine-2026-08-09",
}


def test_midterm_accepts_its_own_state_with_exact_operator_approval():
    accepted = midterm_engine.assert_identity_is_midterm_grade(
        _built_record(protected="false"), release=APPROVED_RELEASE_CONFIG)

    assert accepted["native_branch_protection"] is False
    assert accepted["control_class"] == (
        enginebuild.CONTROL_HUMAN_EXACT_HEAD)
    assert accepted["emits_evidence_classes"] == [COUNT_EVIDENCE_CLASS,
                                                  PANEL_EVIDENCE_CLASS]


def test_midterm_emits_only_single_repo_evidence_classes():
    accepted = midterm_engine.assert_identity_is_midterm_grade(
        _built_record(protected="false"), release=APPROVED_RELEASE_CONFIG)

    assert all(name.startswith("MIDTERM_SINGLE_REPO_")
               for name in accepted["emits_evidence_classes"])
    assert not any(name.startswith("TRUSTED_")
                   for name in accepted["emits_evidence_classes"])


def test_midterm_refuses_without_exact_operator_approval():
    """The right kind of record is not the same as an approved one."""
    unapproved = dict(APPROVED_RELEASE_CONFIG,
                      approved_engine_release_tag=None)

    with pytest.raises(PanelRefusal) as excinfo:
        midterm_engine.assert_identity_is_midterm_grade(
            _built_record(protected="false"), release=unapproved)

    assert "without_exact_approval" in str(excinfo.value)


def test_midterm_refuses_an_identity_for_a_different_artifact():
    other = dict(APPROVED_RELEASE_CONFIG,
                 approved_engine_artifact_sha256="a" * 64)

    with pytest.raises(PanelRefusal) as excinfo:
        midterm_engine.assert_identity_is_midterm_grade(
            _built_record(protected="false"), release=other)

    assert "different_artifact" in str(excinfo.value)


def test_midterm_refuses_a_protected_claim_from_this_unprotected_repository():
    with pytest.raises(PanelRefusal) as excinfo:
        midterm_engine.assert_identity_is_midterm_grade(
            _built_record(protected="true"), release=APPROVED_RELEASE_CONFIG)

    assert "claims_native_protection" in str(excinfo.value)


# -------------------------------------------------------------------- 9 ----

def test_release_configuration_cannot_call_an_unprotected_build_protected():
    with pytest.raises(PanelRefusal) as excinfo:
        midterm_engine.assert_release_protection_claim({
            "native_branch_protection": True,
            "control_class": enginebuild.CONTROL_HUMAN_EXACT_HEAD})

    assert "protection_claim_contradicts" in str(excinfo.value)


def test_release_configuration_must_state_protection_at_all():
    with pytest.raises(PanelRefusal) as excinfo:
        midterm_engine.assert_release_protection_claim(
            {"control_class": enginebuild.CONTROL_HUMAN_EXACT_HEAD})

    assert "omits_protection" in str(excinfo.value)


def test_the_committed_release_configuration_is_self_consistent():
    with open("governance/midterm-panel-engine-release.json",
              encoding="utf-8") as handle:
        config = json.load(handle)

    claim = midterm_engine.assert_release_protection_claim(config)

    assert claim["native_branch_protection"] is False
    assert claim["control_class"] == enginebuild.CONTROL_HUMAN_EXACT_HEAD


# ------------------------------------------------------------------- 10 ----

def test_current_github_free_state_produces_the_midterm_identity():
    """What this repository actually is, today.

    `main` has no native protection. A build from it must produce the mid-term
    identity — and the artifact from run 31286479960, which claimed the
    protected one, must not be reproducible by the corrected code."""
    record = _built_record(protected="false")

    assert record["state"] == enginebuild.MIDTERM_STATE
    assert record["state"] != enginebuild.PROTECTED_STATE
    assert record["control_class"] == enginebuild.CONTROL_HUMAN_EXACT_HEAD
    enginebridge_refused = False
    try:
        enginebridge.assert_identity_is_trusted_grade(record)
    except LaneRefusal:
        enginebridge_refused = True
    assert enginebridge_refused, (
        "the trusted lane must refuse what this repository can currently build")


def test_the_workflow_passes_the_platform_protection_fact():
    """The build cannot record a fact the workflow never gives it."""
    with open(".github/workflows/trusted-engine-build.yml",
              encoding="utf-8") as handle:
        workflow = handle.read()

    assert "BUILD_REF_PROTECTED: ${{ github.ref_protected }}" in workflow
    assert '"build_ref_protected": os.environ["BUILD_REF_PROTECTED"]' in (
        workflow)


def test_no_protected_state_constant_remains_hardcoded_in_the_builder():
    """The shape of the original defect, not just its value.

    A single `BUILT_STATE` constant meant no input could change the claim. If
    one reappears, this fails regardless of which string it holds."""
    with open("scripts/trustedlane/enginebuild.py", encoding="utf-8") as handle:
        source = handle.read()

    assert "\nBUILT_STATE = " not in source
    assert "STATE_FOR_CONTROL" in source
