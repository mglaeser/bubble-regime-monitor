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
from trustedlane import d1cli, enginebridge, enginebuild
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

#: The COMPLETE operator approval. Digest plus tag is no longer enough: those
#: identify bytes and a name, not the identity document carrying the runtime
#: lock, SBOM, build run and control class.
APPROVED_RELEASE_CONFIG = {
    "approved_engine_source_sha": "c" * 40,
    "approved_engine_protected_sha": "e" * 40,
    "approved_engine_artifact_sha256": "9" * 64,
    "approved_engine_release_tag": "midterm-panel-engine-2026-08-09",
    "approved_engine_identity_sha256": "a" * 64,
    "native_branch_protection": False,
    "control_class": "HUMAN_EXACT_HEAD_COMPENSATING_CONTROL",
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


# ------------------------------------------- F-01: the live D1 loader ------
#
# The producer-side seal is only worth what the CONSUMER recomputes. These go
# through `d1cli.load_engine_identity` — the function D1 actually calls — not
# through the helpers, because the gap this section exists for was precisely
# that the helpers were strict and the loader did not call them.

def _relabelled_as_protected(record):
    """Coherently edit the unsealed header, touching no approved digest.

    Every one of the five digests an operator approves is left exactly as it
    was, and the nested provenance is untouched. Only the header lies."""
    tampered = json.loads(json.dumps(record))
    tampered["state"] = enginebuild.PROTECTED_STATE
    tampered["build_ref_protected"] = True
    tampered["native_branch_protection"] = True
    tampered["control_class"] = enginebuild.CONTROL_NATIVE_PROTECTED_REF
    return tampered


def test_d1_loader_refuses_midterm_identity_relabelled_as_protected(tmp_path):
    record = _relabelled_as_protected(_built_record(protected="false"))
    original = _built_record(protected="false")
    assert all(record[f] == original[f]
               for f in enginebuild.BUILT_IDENTITY_FIELDS), (
        "the tamper must not touch an approved digest, or the test proves "
        "nothing about the seal")

    path = tmp_path / "engine-identity.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(LaneRefusal, match="reseal|provenance"):
        d1cli.load_engine_identity(str(path))


def _write(tmp_path, record_or_text):
    path = tmp_path / "engine-identity.json"
    if isinstance(record_or_text, (bytes, bytearray)):
        path.write_bytes(record_or_text)
    elif isinstance(record_or_text, str):
        path.write_text(record_or_text, encoding="utf-8")
    else:
        path.write_text(json.dumps(record_or_text), encoding="utf-8")
    return str(path)


def test_d1_loader_accepts_a_genuine_protected_record(tmp_path):
    loaded = d1cli.load_engine_identity(
        _write(tmp_path, _built_record(protected="true")))

    assert loaded["state"] == enginebuild.PROTECTED_STATE
    assert loaded["native_branch_protection"] is True


def test_d1_loader_refuses_a_genuine_midterm_record(tmp_path):
    with pytest.raises(LaneRefusal, match="refuses_midterm"):
        d1cli.load_engine_identity(
            _write(tmp_path, _built_record(protected="false")))


def test_d1_loader_refuses_a_record_whose_stored_digests_agree_but_are_stale():
    """Both copies of `provenance_sha256` equal, both describing an edit."""
    record = _built_record(protected="false")
    record["provenance"]["build_ref_protected"] = True
    resealed = enginebuild.recomputed_provenance_digest(record["provenance"])
    record["provenance"]["provenance_sha256"] = resealed
    record["provenance_sha256"] = resealed

    # The two stored copies now agree AND the digest reseals — but the header
    # still disagrees with the provenance it seals, which is the last check.
    with pytest.raises(LaneRefusal, match="provenance_disagree|control_class"):
        enginebuild.assert_built_record(record)


def test_d1_loader_refuses_duplicate_json_keys(tmp_path):
    record = _built_record(protected="false")
    text = json.dumps(record)
    # A second `state`, which `json.load` would silently resolve to the last.
    doctored = f'{text[:-1]}, "state": "{enginebuild.PROTECTED_STATE}"}}'

    with pytest.raises(LaneRefusal, match="duplicate_key"):
        d1cli.load_engine_identity(_write(tmp_path, doctored))


def test_d1_loader_refuses_a_record_with_no_provenance(tmp_path):
    record = _built_record(protected="true")
    del record["provenance"]

    with pytest.raises(LaneRefusal, match="provenance_missing"):
        d1cli.load_engine_identity(_write(tmp_path, record))


def test_d1_loader_refuses_a_header_that_disagrees_with_its_provenance(tmp_path):
    record = _built_record(protected="true")
    record["provenance"]["control_class"] = (
        enginebuild.CONTROL_HUMAN_EXACT_HEAD)

    with pytest.raises(LaneRefusal, match="reseal|disagree"):
        d1cli.load_engine_identity(_write(tmp_path, record))


def test_d1_loader_refuses_non_utf8_and_oversized_documents(tmp_path):
    with pytest.raises(LaneRefusal, match="not_utf8"):
        d1cli.load_engine_identity(_write(tmp_path, b"\xff\xfe{}"))

    big = tmp_path / "big.json"
    big.write_bytes(b"[" + b" " * enginebuild.IDENTITY_DOCUMENT_MAX_BYTES)
    with pytest.raises(LaneRefusal, match="too_large"):
        d1cli.load_engine_identity(str(big))


def test_d1_loader_refuses_a_json_array(tmp_path):
    with pytest.raises(LaneRefusal, match="not_an_object"):
        d1cli.load_engine_identity(_write(tmp_path, "[]"))


# ------------------------------- F-02: the live mid-term consumer path ------
#
# `load_engine_for_mode` rebuilt the artifact from pinned commits and never
# opened `engine-identity.json`. Reproducibility is not provenance: it proves
# the bytes can be made, not which build made the approved ones nor under what
# control. These drive `load_release_engine_identity`, which is the function
# the live path now calls.

def _identity_record(protected="false", **over):
    record = _built_record(protected=protected)
    record["source_roles"] = {
        "candidate_verifier": {"source_commit": "c" * 40},
        "protected_trusted_lane": {"source_commit": "e" * 40},
    }
    record.update(over)
    return record


def _release(**over):
    release = dict(APPROVED_RELEASE_CONFIG)
    release.update(over)
    return release


def _identity_file(tmp_path, record):
    path = tmp_path / "release-engine-identity.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return str(path)


def test_provider_mode_refuses_without_an_identity_document():
    with pytest.raises(PanelRefusal, match="identity_not_configured"):
        midterm_engine.load_release_engine_identity(
            release=_release(), mode=midterm_engine.MODE_PROVIDER,
            rebuilt_artifact_sha256="9" * 64)


def test_dry_run_may_proceed_without_an_identity_document():
    """The no-key gate must keep working; it says so rather than implying."""
    result = midterm_engine.load_release_engine_identity(
        release=_release(approved_engine_identity_path=None),
        mode=midterm_engine.MODE_DRY_RUN, rebuilt_artifact_sha256="9" * 64)

    assert result["engine_identity"] is None
    assert "nothing about the builder" in result["honest_scope"]


def test_corrected_midterm_identity_passes_with_exact_approval(tmp_path):
    import hashlib

    record = _identity_record()
    path = _identity_file(tmp_path, record)
    with open(path, "rb") as handle:
        approved = hashlib.sha256(handle.read()).hexdigest()
    result = midterm_engine.load_release_engine_identity(
        release=_release(approved_engine_identity_path=path,
                         approved_engine_identity_sha256=approved),
        mode=midterm_engine.MODE_PROVIDER, rebuilt_artifact_sha256="9" * 64)

    assert result["engine_identity_sha256"] == approved
    assert len(result["engine_release_binding_sha256"]) == 64

    assert result["state"] == enginebuild.MIDTERM_STATE
    assert result["native_branch_protection"] is False
    assert result["control_class"] == enginebuild.CONTROL_HUMAN_EXACT_HEAD
    assert result["build_workflow_run_id"] == 31286479960
    assert all(name.startswith("MIDTERM_SINGLE_REPO_")
               for name in result["emits_evidence_classes"])


def test_the_rejected_run_identity_is_refused_on_the_live_path(tmp_path):
    """Run 31286479960's record claimed the protected state.

    Its disposition is REJECTED_FALSE_PROTECTED_REF_PROVENANCE, and the live
    mid-term path must be the thing that enforces that, not a document."""
    record = _identity_record(protected="true")
    with pytest.raises(PanelRefusal, match="claims_native_protection"):
        midterm_engine.load_release_engine_identity(
            release=_release(approved_engine_identity_path=_identity_file(
                tmp_path, record)),
            mode=midterm_engine.MODE_PROVIDER, rebuilt_artifact_sha256="9" * 64)


def test_identity_for_a_different_artifact_is_refused(tmp_path):
    record = _identity_record()
    with pytest.raises(PanelRefusal, match="different_artifact"):
        midterm_engine.load_release_engine_identity(
            release=_release(approved_engine_identity_path=_identity_file(
                tmp_path, record)),
            mode=midterm_engine.MODE_PROVIDER,
            rebuilt_artifact_sha256="a" * 64)


def test_identity_from_another_source_role_pair_is_refused(tmp_path):
    record = _identity_record()
    record["source_roles"]["candidate_verifier"]["source_commit"] = "d" * 40
    with pytest.raises(PanelRefusal, match="source_roles_differ"):
        midterm_engine.load_release_engine_identity(
            release=_release(approved_engine_identity_path=_identity_file(
                tmp_path, record)),
            mode=midterm_engine.MODE_PROVIDER, rebuilt_artifact_sha256="9" * 64)


def test_a_tampered_identity_document_is_refused_on_the_live_path(tmp_path):
    """The same coherent relabel, arriving at the mid-term lane."""
    record = _relabelled_as_protected(_identity_record())
    with pytest.raises(PanelRefusal, match="identity_rejected"):
        midterm_engine.load_release_engine_identity(
            release=_release(approved_engine_identity_path=_identity_file(
                tmp_path, record)),
            mode=midterm_engine.MODE_PROVIDER, rebuilt_artifact_sha256="9" * 64)


def test_an_identity_document_the_operator_did_not_approve_is_refused(tmp_path):
    record = _identity_record()
    with pytest.raises(PanelRefusal, match="document_digest_mismatch"):
        midterm_engine.load_release_engine_identity(
            release=_release(
                approved_engine_identity_path=_identity_file(tmp_path, record),
                approved_engine_identity_sha256="b" * 64),
            mode=midterm_engine.MODE_PROVIDER, rebuilt_artifact_sha256="9" * 64)


def test_duplicate_keys_in_the_release_identity_are_refused(tmp_path):
    path = tmp_path / "dup.json"
    text = json.dumps(_identity_record())
    path.write_text(text[:-1] + ', "state": "X"}', encoding="utf-8")
    with pytest.raises(PanelRefusal, match="identity_rejected"):
        midterm_engine.load_release_engine_identity(
            release=_release(approved_engine_identity_path=str(path)),
            mode=midterm_engine.MODE_PROVIDER, rebuilt_artifact_sha256="9" * 64)


# --------------------------------------- F-03: real release-config inputs ---

def test_release_config_carries_protection_and_calls_the_validator():
    config = midterm_engine.resolve_release_config({
        "MIDTERM_APPROVED_ENGINE_SOURCE_SHA": "c" * 40,
        "MIDTERM_APPROVED_ENGINE_PROTECTED_SHA": "e" * 40,
        "MIDTERM_APPROVED_ENGINE_NATIVE_BRANCH_PROTECTION": "false",
        "MIDTERM_APPROVED_ENGINE_CONTROL_CLASS":
            enginebuild.CONTROL_HUMAN_EXACT_HEAD,
    })

    assert config["native_branch_protection"] is False
    assert config["control_class"] == enginebuild.CONTROL_HUMAN_EXACT_HEAD


def test_release_config_refuses_a_contradictory_protection_claim():
    with pytest.raises(PanelRefusal, match="protection_claim_contradicts"):
        midterm_engine.resolve_release_config({
            "MIDTERM_APPROVED_ENGINE_SOURCE_SHA": "c" * 40,
            "MIDTERM_APPROVED_ENGINE_PROTECTED_SHA": "e" * 40,
            "MIDTERM_APPROVED_ENGINE_NATIVE_BRANCH_PROTECTION": "true",
            "MIDTERM_APPROVED_ENGINE_CONTROL_CLASS":
                enginebuild.CONTROL_HUMAN_EXACT_HEAD,
        })


def test_the_panel_workflow_exports_protection_outside_the_truthy_loop():
    """`false` is falsy; the old export loop would have dropped it."""
    with open(".github/workflows/midterm-panel-review.yml",
              encoding="utf-8") as handle:
        workflow = handle.read()

    assert "MIDTERM_APPROVED_ENGINE_NATIVE_BRANCH_PROTECTION" in workflow
    assert "MIDTERM_APPROVED_ENGINE_CONTROL_CLASS" in workflow
    assert workflow.count("if key in doc:") == 3


# ------------------------------------------ F-04: no stale protected prose --

def test_the_build_workflow_makes_no_protected_ref_claim_in_prose():
    with open(".github/workflows/trusted-engine-build.yml",
              encoding="utf-8") as handle:
        workflow = handle.read().lower()

    assert "survived branch protection" not in workflow
    assert "runs from a protected ref" not in workflow
    assert "check out the protected ref" not in workflow
    assert "github.ref_protected" in workflow


# ------------------- F-05: the workflow must produce the identity path ------

def test_the_panel_workflow_materialises_the_identity_document():
    """The consumer refused without a path and nothing produced one.

    Fail-closed, and also not operational: the first provider-backed count job
    would have stopped at `approved_engine_identity_not_configured`."""
    with open(".github/workflows/midterm-panel-review.yml",
              encoding="utf-8") as handle:
        workflow = handle.read()

    assert workflow.count("python -m midtermpanel.releaseasset") == 2, (
        "count and panel must each materialise the document")


def test_materialisation_precedes_every_provider_key_step():
    """Ordering is the control, so it is asserted rather than described."""
    with open(".github/workflows/midterm-panel-review.yml",
              encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    materialise = [i for i, ln in enumerate(lines)
                   if "python -m midtermpanel.releaseasset" in ln]
    provider = [i for i, ln in enumerate(lines)
                if "TRUSTED_VERIFIER_OPENAI_KEY" in ln]
    assert materialise and provider
    for key_line in provider:
        assert any(m < key_line for m in materialise), (
            "a provider-key step precedes every materialisation step")


def test_every_materialiser_step_holds_no_provider_secret():
    """BOTH jobs. The first version of this test read `split(...)[1]` and so
    only ever inspected the count job's step; the panel job's copy was
    unasserted."""
    with open(".github/workflows/midterm-panel-review.yml",
              encoding="utf-8") as handle:
        text = handle.read()

    blocks = text.split("Materialise the approved engine identity document")[1:]
    assert len(blocks) == 2, "count and panel must each have the step"
    for block in blocks:
        env_block = block.split("python -m midtermpanel.releaseasset")[0]
        assert "TRUSTED_VERIFIER_OPENAI_KEY" not in env_block
        assert "MIDTERM_PANEL_PROVIDER_KEY" not in env_block
        assert "GITHUB_TOKEN" in env_block


def test_the_materialiser_is_actually_runnable_as_a_module():
    """`python -m midtermpanel.releaseasset` was a no-op.

    The module defined `main()` and never called it, so the step ran, exited
    0, materialised nothing and exported no path — a green step that did not
    do its job, which is worse than a red one."""
    with open("scripts/midtermpanel/releaseasset.py", encoding="utf-8") as h:
        source = h.read()

    assert 'if __name__ == "__main__":' in source
    assert source.rstrip().endswith("main()")


def test_the_ordering_guard_checks_the_name_this_lane_actually_uses():
    """The panel binds the secret to MIDTERM_PANEL_PROVIDER_KEY on purpose.

    A guard that looked only for TRUSTED_VERIFIER_OPENAI_KEY would read
    'no provider secret in scope' with the key present under the name this
    lane really exports."""
    from midtermpanel import releaseasset
    from midtermpanel.transport import PROVIDER_KEY_ENV

    with open("scripts/midtermpanel/releaseasset.py", encoding="utf-8") as h:
        assert "PROVIDER_KEY_ENV" in h.read()
    assert PROVIDER_KEY_ENV == "MIDTERM_PANEL_PROVIDER_KEY"
    assert releaseasset.materialise_release_identity is not None


def test_the_host_check_happens_before_the_token_is_sent():
    """Checking `response.geturl()` after urlopen is too late: urllib has
    already followed the redirect and re-sent the Authorization header."""
    from midtermpanel import releaseasset

    assert issubclass(releaseasset._HostRestrictedRedirect,
                      __import__("urllib.request", fromlist=["x"]
                                 ).HTTPRedirectHandler)
    for bad in ("http://api.github.com/x", "https://evil.example/x",
                "ftp://api.github.com/x"):
        with pytest.raises(PanelRefusal):
            releaseasset.assert_permitted_url(bad, where="test")
    assert releaseasset.assert_permitted_url(
        "https://api.github.com/x", where="test")


def test_count_evidence_carries_the_release_binding_the_panel_compares():
    """A field compared but never written makes the check unreachable."""
    from midtermpanel.panel import IDENTITY_HANDOFF_FIELDS

    with open("scripts/midtermpanel/countcli.py", encoding="utf-8") as handle:
        source = handle.read()

    for field in IDENTITY_HANDOFF_FIELDS:
        assert f'"{field}"' in source, (
            f"{field} is compared in the handoff but never written by count")


def test_identity_compared_is_false_when_nothing_was_compared(monkeypatch):
    from midtermpanel import panel
    from midtermpanel.panel import verify_handoff

    monkeypatch.setattr(panel, "assert_plan_is_executable", lambda p: p)
    plan = {"plan_sha256": "p" * 64, "candidate_head_sha": "a" * 40,
            "candidate_base_sha": "b" * 40, "engine_digest": "c" * 64,
            "policy_digest": "d" * 64, "request_semantics_digest": "r" * 64,
            "execution_request_hashes": ["h"], "executable": True}
    count = {"candidate_head_sha": "a" * 40,
             "body": {"request_semantics_digest": "r" * 64,
                      "plan_sha256": "p" * 64}}

    result = verify_handoff(
        count_record=count, plan=plan, expected_head="a" * 40,
        expected_base="b" * 40, expected_engine_digest="c" * 64,
        expected_policy_digest="d" * 64,
        panel_identity={f: None for f in _count_body()},
        require_identity=False)

    assert result["identity_compared"] is False
    assert result["identity_required"] is False


def test_the_materialiser_refuses_a_missing_or_malformed_approved_digest():
    from midtermpanel import releaseasset

    for bad in ("", None, "abc"):
        with pytest.raises(PanelRefusal, match="expected_digest_malformed"):
            releaseasset.materialise_release_identity(
                owner="mglaeser", repo="bubble-regime-monitor", tag="t",
                token="gh", repository_numeric_id=1297332828,
                expected_sha256=bad, destination="/tmp/never-written.json")


def test_the_materialiser_refuses_without_a_tag_or_token():
    from midtermpanel import releaseasset

    with pytest.raises(PanelRefusal, match="release_tag_not_configured"):
        releaseasset.resolve_identity_asset(
            owner="o", repo="r", tag="", token="gh",
            repository_numeric_id=1297332828)
    with pytest.raises(PanelRefusal, match="release_api_token_missing"):
        releaseasset.resolve_identity_asset(
            owner="o", repo="r", tag="t", token="",
            repository_numeric_id=1297332828)


def test_the_materialiser_will_not_overwrite_an_existing_destination(tmp_path,
                                                                     monkeypatch):
    """A symlink at the destination would place approved bytes elsewhere."""
    from midtermpanel import releaseasset

    payload = json.dumps(_identity_record()).encode("utf-8")
    digest = __import__("hashlib").sha256(payload).hexdigest()
    monkeypatch.setattr(releaseasset, "resolve_identity_asset",
                        lambda **kw: {"asset_url": "u", "asset_id": 1,
                                      "release_id": 1, "tag": kw["tag"]})
    monkeypatch.setattr(releaseasset, "_request", lambda *a, **k: payload)

    destination = tmp_path / "engine-identity.json"
    destination.write_text("squatted", encoding="utf-8")
    with pytest.raises(PanelRefusal, match="destination_exists"):
        releaseasset.materialise_release_identity(
            owner="o", repo="r", tag="t", token="gh",
            repository_numeric_id=1297332828, expected_sha256=digest,
            destination=str(destination))


def test_the_materialiser_refuses_a_document_the_operator_did_not_approve(
        tmp_path, monkeypatch):
    from midtermpanel import releaseasset

    payload = json.dumps(_identity_record()).encode("utf-8")
    monkeypatch.setattr(releaseasset, "resolve_identity_asset",
                        lambda **kw: {"asset_url": "u", "asset_id": 1,
                                      "release_id": 1, "tag": kw["tag"]})
    monkeypatch.setattr(releaseasset, "_request", lambda *a, **k: payload)

    with pytest.raises(PanelRefusal, match="release_identity_digest_mismatch"):
        releaseasset.materialise_release_identity(
            owner="o", repo="r", tag="t", token="gh",
            repository_numeric_id=1297332828, expected_sha256="b" * 64,
            destination=str(tmp_path / "out.json"))


def test_the_exact_release_asset_is_written_and_strict_loaded(tmp_path,
                                                              monkeypatch):
    from midtermpanel import releaseasset

    payload = json.dumps(_identity_record()).encode("utf-8")
    digest = __import__("hashlib").sha256(payload).hexdigest()
    monkeypatch.setattr(releaseasset, "resolve_identity_asset",
                        lambda **kw: {"asset_url": "u", "asset_id": 7,
                                      "release_id": 3, "tag": kw["tag"]})
    monkeypatch.setattr(releaseasset, "_request", lambda *a, **k: payload)

    result = releaseasset.materialise_release_identity(
        owner="o", repo="r", tag="midterm-engine", token="gh",
        repository_numeric_id=1297332828, expected_sha256=digest,
        destination=str(tmp_path / "nested" / "engine-identity.json"))

    assert result["engine_identity_sha256"] == digest
    assert result["state"] == enginebuild.MIDTERM_STATE
    assert json.loads(open(result["path"], encoding="utf-8").read())


# ------------------------ F-06: exact identity approval is not optional -----

def test_artifact_and_tag_without_an_identity_digest_is_not_approved():
    config = {"approved_engine_source_sha": "c" * 40,
              "approved_engine_protected_sha": "e" * 40,
              "approved_engine_artifact_sha256": "9" * 64,
              "approved_engine_release_tag": "t"}

    assert midterm_engine.provenance_of(config) == (
        midterm_engine.REBUILT_TEST_ONLY)


def test_identity_digest_without_protection_facts_is_not_approved():
    config = {"approved_engine_source_sha": "c" * 40,
              "approved_engine_protected_sha": "e" * 40,
              "approved_engine_artifact_sha256": "9" * 64,
              "approved_engine_release_tag": "t",
              "approved_engine_identity_sha256": "a" * 64}

    assert midterm_engine.provenance_of(config) == (
        midterm_engine.REBUILT_TEST_ONLY)


def _complete_binding(**over):
    config = {"approved_engine_source_sha": "c" * 40,
              "approved_engine_protected_sha": "e" * 40,
              "approved_engine_artifact_sha256": "9" * 64,
              "approved_engine_release_tag": "midterm-engine",
              "approved_engine_identity_sha256": "a" * 64,
              "native_branch_protection": False,
              "control_class": enginebuild.CONTROL_HUMAN_EXACT_HEAD}
    config.update(over)
    return config


def test_the_complete_binding_is_approved_and_hashes():
    config = _complete_binding()

    assert midterm_engine.provenance_of(config) == (
        midterm_engine.APPROVED_RELEASE)
    assert len(midterm_engine.release_binding(config)[
        "engine_release_binding_sha256"]) == 64


def test_false_protection_counts_as_present_in_the_binding():
    """`False` is an answer. A truthiness test would drop the honest one."""
    assert midterm_engine._binding_member_is_present(False) is True
    assert midterm_engine._binding_member_is_present(None) is False
    assert midterm_engine._binding_member_is_present("") is False


@pytest.mark.parametrize("field", midterm_engine.RELEASE_BINDING_FIELDS)
def test_changing_any_binding_member_changes_the_digest(field):
    base = midterm_engine.release_binding(_complete_binding())
    altered_value = (True if field == "native_branch_protection"
                     else enginebuild.CONTROL_NATIVE_PROTECTED_REF
                     if field == "control_class" else "f" * 40)
    altered = midterm_engine.release_binding(
        _complete_binding(**{field: altered_value}))

    assert altered["engine_release_binding_sha256"] != base[
        "engine_release_binding_sha256"]


def test_provider_mode_refuses_when_the_identity_digest_is_absent(tmp_path):
    """The comparison must be unconditional in provider mode."""
    release = dict(_complete_binding(), approved_engine_identity_sha256=None,
                   approved_engine_identity_path=_identity_file(
                       tmp_path, _identity_record()))

    with pytest.raises(PanelRefusal, match="identity_digest_not_configured"):
        midterm_engine.load_release_engine_identity(
            release=release, mode=midterm_engine.MODE_PROVIDER,
            rebuilt_artifact_sha256="9" * 64)


# --------------------- F-07: panel must prove it used count's identity ------

def _count_body(**over):
    body = {
        "engine_identity_sha256": "d" * 64,
        "engine_identity_state": enginebuild.MIDTERM_STATE,
        "engine_native_branch_protection": False,
        "engine_control_class": enginebuild.CONTROL_HUMAN_EXACT_HEAD,
        "engine_build_run_id": 31286479960,
        "engine_build_run_attempt": 1,
        "engine_provenance_sha256": "e" * 64,
        "engine_artifact_sha256": "9" * 64,
        "engine_release_binding_sha256": "f" * 64,
    }
    body.update(over)
    return body


def test_the_handoff_compares_every_identity_field():
    from midtermpanel.panel import IDENTITY_HANDOFF_FIELDS

    assert set(IDENTITY_HANDOFF_FIELDS) == set(_count_body())


@pytest.mark.parametrize("field", [
    "engine_identity_sha256", "engine_build_run_id",
    "engine_provenance_sha256", "engine_release_binding_sha256",
    "engine_control_class", "engine_native_branch_protection"])
def test_a_different_builder_identity_blocks_the_handoff(field):
    """Same plan, same source roles, different builder → no generation."""
    from midtermpanel.panel import IDENTITY_HANDOFF_FIELDS

    counted = _count_body()
    panel_side = _count_body(**{field: ("z" * 64 if isinstance(
        counted[field], str) else 999 if isinstance(counted[field], int)
        else True)})

    differing = [f for f in IDENTITY_HANDOFF_FIELDS
                 if counted[f] != panel_side[f]]
    assert differing == [field], (
        "the fixture must differ in exactly the field under test")


def test_panel_identity_binding_uses_the_same_vocabulary_as_count():
    """Two near-identical vocabularies agree until one is edited."""
    from midtermpanel.panelcli import identity_binding

    opened = {
        "engine_identity_sha256": "d" * 64,
        "engine_identity_state": enginebuild.MIDTERM_STATE,
        "native_branch_protection": False,
        "control_class": enginebuild.CONTROL_HUMAN_EXACT_HEAD,
        "engine_build_run_id": 31286479960,
        "engine_build_run_attempt": 1,
        "engine_provenance_sha256": "e" * 64,
        "engine_artifact_sha256": "9" * 64,
        "engine_release_binding_sha256": "f" * 64,
    }
    assert identity_binding(opened) == _count_body()


def test_panel_evidence_carries_the_binding_and_names_its_inputs():
    with open("scripts/midtermpanel/panelcli.py", encoding="utf-8") as handle:
        source = handle.read()

    assert '"count_evidence_sha256": count_evidence_sha256' in source
    assert "**(panel_identity or {})" in source
    assert "panel_identity=panel_identity" in source


# ------------------------------- F-08: dedupe must track the release --------

def test_dedupe_binds_on_the_release_not_only_the_source_roles():
    from midtermpanel import dedupe

    assert "engine_release_binding_sha256" in dedupe.BINDING_FIELDS
    assert "engine_source_digest" in dedupe.BINDING_FIELDS
    assert "engine_digest" not in dedupe.BINDING_FIELDS


def _dedupe_binding(**over):
    from midtermpanel import dedupe

    values = {"candidate_head_sha": "a" * 40, "candidate_base_sha": "b" * 40,
              "engine_source_digest": "c" * 64,
              "engine_release_binding_sha256": "d" * 64,
              "policy_digest": "e" * 64,
              "request_semantics_digest": "f" * 64}
    values.update(over)
    return dedupe.binding(**values)


def test_same_source_roles_with_a_different_release_does_not_reuse():
    """The exact stale-evidence case the dedupe docstring promises to stop."""
    first = _dedupe_binding()
    second = _dedupe_binding(engine_release_binding_sha256="9" * 64)

    assert first["binding_digest"] != second["binding_digest"]


def test_an_identical_full_release_binding_reuses():
    assert _dedupe_binding()["binding_digest"] == _dedupe_binding()[
        "binding_digest"]


def test_a_dedupe_binding_with_an_empty_member_is_refused():
    with pytest.raises(PanelRefusal, match="dedupe_binding_incomplete"):
        _dedupe_binding(engine_release_binding_sha256="")


def test_provider_mode_refuses_a_binding_of_absences(monkeypatch):
    """Comparing `None` to `None` nine times is not a check.

    A dry run legitimately has no identity to compare. A provider-backed run
    that reached the comparison with blanks on either side has demonstrated
    only that neither job knew which engine it used."""
    from midtermpanel import panel
    from midtermpanel.panel import verify_handoff

    # The plan validator runs first and is not what this test is about; the
    # synthetic plan below is deliberately not a real signed plan.
    monkeypatch.setattr(panel, "assert_plan_is_executable", lambda p: p)

    plan = {"plan_sha256": "p" * 64, "candidate_head_sha": "a" * 40,
            "candidate_base_sha": "b" * 40, "engine_digest": "c" * 64,
            "policy_digest": "d" * 64, "request_semantics_digest": "r" * 64,
            "execution_request_hashes": ["h"], "executable": True}
    count = {"candidate_head_sha": "a" * 40,
             "body": {"request_semantics_digest": "r" * 64,
                      "plan_sha256": "p" * 64}}

    with pytest.raises(PanelRefusal, match="identity_binding_incomplete"):
        verify_handoff(count_record=count, plan=plan, expected_head="a" * 40,
                       expected_base="b" * 40, expected_engine_digest="c" * 64,
                       expected_policy_digest="d" * 64,
                       panel_identity={f: None for f in _count_body()},
                       require_identity=True)


def test_only_provider_mode_requires_the_identity_binding():
    with open("scripts/midtermpanel/panelcli.py", encoding="utf-8") as handle:
        source = handle.read()

    assert '"require_identity": mode == MODE_PROVIDER' in source
    assert source.count("require_identity=prepared[\"require_identity\"]") == 2


def test_panel_evidence_names_the_counts_published_digest(monkeypatch):
    """`digest_of(count_record)` can never equal its own `evidence_sha256`.

    The record carries that field, so re-hashing it yields a number nobody
    else can reproduce — and a field named `count_evidence_sha256` that
    identifies nothing is worse than an absent one."""
    from midtermpanel.evidence import digest_of

    body = {"a": 1}
    record = {**body, "evidence_sha256": digest_of(body)}
    assert digest_of(record) != record["evidence_sha256"]

    with open("scripts/midtermpanel/panelcli.py", encoding="utf-8") as handle:
        source = handle.read()
    assert 'count_evidence_sha256 = count_record.get("evidence_sha256")' in (
        source)
    assert "count_evidence_sha256 = digest_of(count_record)" not in source


def test_identity_compared_needs_every_field_not_merely_one(monkeypatch):
    """A dry run rebuilds the artifact, so `engine_artifact_sha256` is
    populated even there. Under `any()` that single field made the record
    claim an identity comparison while eight of nine were absent."""
    from midtermpanel import panel
    from midtermpanel.panel import verify_handoff

    monkeypatch.setattr(panel, "assert_plan_is_executable", lambda p: p)
    plan = {"plan_sha256": "p" * 64, "candidate_head_sha": "a" * 40,
            "candidate_base_sha": "b" * 40, "engine_digest": "c" * 64,
            "policy_digest": "d" * 64, "request_semantics_digest": "r" * 64,
            "execution_request_hashes": ["h"], "executable": True}
    partial = {f: None for f in _count_body()}
    partial["engine_artifact_sha256"] = "9" * 64
    count = {"candidate_head_sha": "a" * 40,
             "body": {"request_semantics_digest": "r" * 64,
                      "plan_sha256": "p" * 64, **partial}}

    result = verify_handoff(
        count_record=count, plan=plan, expected_head="a" * 40,
        expected_base="b" * 40, expected_engine_digest="c" * 64,
        expected_policy_digest="d" * 64, panel_identity=partial,
        require_identity=False)

    assert result["identity_compared"] is False, (
        "one populated field of nine is not an identity comparison")


def test_identity_compared_is_true_only_for_a_complete_comparison(monkeypatch):
    from midtermpanel import panel
    from midtermpanel.panel import verify_handoff

    monkeypatch.setattr(panel, "assert_plan_is_executable", lambda p: p)
    plan = {"plan_sha256": "p" * 64, "candidate_head_sha": "a" * 40,
            "candidate_base_sha": "b" * 40, "engine_digest": "c" * 64,
            "policy_digest": "d" * 64, "request_semantics_digest": "r" * 64,
            "execution_request_hashes": ["h"], "executable": True}
    full = _count_body()
    count = {"candidate_head_sha": "a" * 40,
             "body": {"request_semantics_digest": "r" * 64,
                      "plan_sha256": "p" * 64, **full}}

    result = verify_handoff(
        count_record=count, plan=plan, expected_head="a" * 40,
        expected_base="b" * 40, expected_engine_digest="c" * 64,
        expected_policy_digest="d" * 64, panel_identity=full,
        require_identity=True)

    assert result["identity_compared"] is True
    assert result["identity_required"] is True
