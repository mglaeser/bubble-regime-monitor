"""MC4 PASS D — the trusted lane contract, and what candidate code cannot do.

Every test here is a NEGATIVE test or a shape test. That is the whole content
of Pass D from inside the reviewed branch: the lane can be specified, its
records can be given schemas, its preconditions can be enumerated — and none
of it can be satisfied here, because satisfying it would be this branch
authorizing itself.

If a test in this file ever passes by producing trusted evidence, the trust
boundary has been lost.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verifier import evidence, trustedlane  # noqa: E402
from verifier.errors import BlockingError  # noqa: E402

ANCHOR = {
    "anchor_kind": "TRUSTED_WORKFLOW_RUN",
    "anchor_reference": "mglaeser/bubble-regime-monitor/actions/runs/999",
    "anchor_digest": "a" * 64,
    "workflow_run_id": 999,
    "workflow_run_attempt": 1,
    "protected_ref": "refs/heads/main",
    "signature": "s" * 64,
}


def _shaped_trusted_record(**over):
    record = {field: "x" for field in trustedlane.TRUSTED_EVIDENCE_FIELDS}
    record["evidence_class"] = evidence.TRUSTED_COUNT_EVIDENCE
    record["external_anchor"] = dict(ANCHOR)
    record.update(over)
    return record


class TestTheLaneIsNotImplementedHere:
    def test_the_contract_says_so_in_its_own_record(self):
        contract = trustedlane.contract_record()
        assert contract["implemented_here"] is False
        assert contract["generation_calls_during_finalization"] == 0
        assert len(contract["trusted_lane_contract_sha256"]) == 64

    def test_the_module_imports_nothing_that_can_reach_a_network(self):
        # Checked over the IMPORT GRAPH, not the text: a substring scan flags
        # the word "requests" inside `assemble_all_requests` and would have
        # to be loosened until it stopped meaning anything.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(trustedlane))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("urllib", "http", "requests", "socket", "ssl",
                          "httpx", "subprocess", "os"):
            assert forbidden not in imported, forbidden

    def test_the_module_reads_no_credential_from_the_environment(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(trustedlane))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "OPENAI_API_KEY" not in node.value
                assert "Bearer " not in node.value

    def test_the_phase_list_puts_the_challenge_before_counting(self):
        # The challenge must be bound into the request that is COUNTED, or
        # the counted request and the executed request are different
        # questions with the same price.
        phases = [p["phase"] for p in trustedlane.TRUSTED_PHASES]
        assert phases.index("mint_challenge") < phases.index("count")
        assert phases.index("global_preflight") < phases.index("count")
        assert phases.index("count") < phases.index("execute")

    def test_finalization_makes_zero_generation_calls(self):
        assert trustedlane.GENERATION_CALLS_DURING_FINALIZATION == 0


class TestPrerequisitesDefaultToUnauthorized:
    def test_nothing_is_satisfied_by_default(self):
        status = trustedlane.prerequisite_status()
        assert status["all_satisfied"] is False
        assert status["real_calls_authorized"] is False
        assert len(status["outstanding"]) == len(
            trustedlane.OPERATOR_PREREQUISITES)

    def test_an_unknown_prerequisite_key_is_refused(self):
        with pytest.raises(BlockingError) as e:
            trustedlane.prerequisite_status({"invent_your_own_authority"})
        assert "unknown_prerequisite_key" in str(e.value)

    def test_claiming_every_prerequisite_still_authorizes_nothing(self):
        # The record is candidate-writable. Satisfying it here would be this
        # branch granting itself permission.
        every = {p.key for p in trustedlane.OPERATOR_PREREQUISITES}
        status = trustedlane.prerequisite_status(every)
        assert status["all_satisfied"] is True
        assert status["real_calls_authorized"] is False
        with pytest.raises(BlockingError) as e:
            trustedlane.assert_real_calls_authorized(status)
        assert "never_authorized_from_candidate_code" in str(e.value)

    def test_the_operator_prerequisites_cover_the_mandated_list(self):
        keys = {p.key for p in trustedlane.OPERATOR_PREREQUISITES}
        for required in ("delete_failed_run", "verify_run_404",
                         "rotate_probe_key", "review_key_usage",
                         "install_environment_key",
                         "no_repository_or_org_fallback",
                         "protected_trusted_environment",
                         "authorize_twelve_pins",
                         "authorize_capability_policy",
                         "approve_literal_authorizations",
                         "approve_count_spending",
                         "approve_review_request_policy",
                         "approve_artifact_retention",
                         "approve_engine_identity",
                         "approve_bootstrap_branch"):
            assert required in keys, required


class TestTrustedRecordShapeIsNecessaryNotSufficient:
    def test_a_complete_shape_validates_as_SHAPE_ONLY(self):
        result = trustedlane.validate_trusted_evidence_shape(
            _shaped_trusted_record())
        assert result["shape_valid"] is True
        assert result["authenticated"] is False
        assert result["verification_status"] == "SHAPE_ONLY_NOT_AUTHENTICATED"

    def test_a_shaped_record_still_confers_no_executable_authority(self):
        # The point of the whole boundary: a perfect shape is not authority.
        record = _shaped_trusted_record()
        record["executable_authority"] = True
        assert evidence.is_executable_authority(record) is False

    @pytest.mark.parametrize("field", trustedlane.TRUSTED_EVIDENCE_FIELDS)
    def test_every_required_field_is_actually_required(self, field):
        record = _shaped_trusted_record()
        record.pop(field)
        with pytest.raises(BlockingError):
            trustedlane.validate_trusted_evidence_shape(record)

    @pytest.mark.parametrize("field", trustedlane.TRUSTED_ANCHOR_FIELDS)
    def test_every_anchor_field_is_actually_required(self, field):
        record = _shaped_trusted_record()
        record["external_anchor"].pop(field)
        with pytest.raises(BlockingError):
            trustedlane.validate_trusted_evidence_shape(record)

    def test_a_candidate_evidence_class_is_refused_by_the_trusted_schema(self):
        record = _shaped_trusted_record(
            evidence_class=evidence.MOCK_TEST_EVIDENCE)
        with pytest.raises(BlockingError) as e:
            trustedlane.validate_trusted_evidence_shape(record)
        assert "class_not_trusted" in str(e.value)


class TestNormalizationContract:
    def test_the_adapter_digest_is_unverified_candidate_side(self):
        record = trustedlane.normalization_record()
        assert record["adapter_digest"] is None
        assert record["adapter_digest_verified"] is False

    def test_a_supplied_digest_is_still_unverified(self):
        # A candidate can WRITE a digest. It cannot make it true.
        record = trustedlane.normalization_record("d" * 64)
        assert record["adapter_digest"] == "d" * 64
        assert record["adapter_digest_verified"] is False

    def test_the_contract_drops_transport_ids_and_provider_error_text(self):
        drops = trustedlane.NORMALIZATION_CONTRACT["drops"]
        assert any("transport ids" in d for d in drops)
        assert any("error bodies" in d for d in drops)

    def test_the_contract_targets_the_envelope_the_executor_validates(self):
        from verifier import executor
        assert trustedlane.NORMALIZATION_CONTRACT[
            "produces_envelope_version"] == executor.RESPONSE_ENVELOPE_VERSION


class TestCodeCutoffClosure:
    def test_the_closure_record_is_open_and_empty(self):
        record = trustedlane.closure_record_template()
        assert record["closure_state"] == "OPEN_PENDING_TRUSTED_LANE"
        for field in trustedlane.CLOSURE_FIELDS:
            assert record[field] is None, field

    def test_the_closure_binds_the_facts_a_branch_digest_cannot(self):
        for field in ("repository_numeric_id", "final_candidate_head_sha",
                      "verifier_code_cutoff_sha", "evidence_only_delta_sha256",
                      "trusted_engine_digest", "workflow_run_id",
                      "protected_ref", "signature"):
            assert field in trustedlane.CLOSURE_FIELDS, field
