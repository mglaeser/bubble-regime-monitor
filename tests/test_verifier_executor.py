"""MC4 PASS C — the mock Stage-3 executor and its integrity gates.

Every provider interaction goes through an injected mock transport, so "no
real model call happened" is structural. The gates are the legacy panel's,
restored and driven adversarially: challenge echo, required approver, distinct
corroborators, DISTINCT REASONING, usage drift, and a synthesis that cannot
clear a refutation.

The end-to-end tests run against the real PR-25 range rather than a stub plan.
That is not thoroughness for its own sake: after C4-F05/F06/F07 the executor
strict-loads and rebuilds the report, hash-matches every request against the
counted one, and preflights every execution payload — none of which a
hand-written plan_record can exercise, and all of which are the point.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_verifier_finalize import (  # noqa: E402
    PR25_BASE,
    PR25_HEAD,
    _have,
    good_pins,
    proposed_authorizations,
)
from verifier import (  # noqa: E402
    counting,
    evidence,
    executor,
    finalize,
    plan,
    providerreq,
    reviewpolicy,
    verdicts,
)
from verifier.errors import (  # noqa: E402
    CHUNK_COUNT_EXHAUSTED,
    EXECUTABLE_PLAN_INVALID,
    INSUFFICIENT_CORROBORATION,
    PROVIDER_RESPONSE_INVALID,
    REQUIRED_APPROVER_MISSING,
    REQUIRED_APPROVER_REFUTED,
    SECRET_PREFLIGHT_FAILED,
    SYNTHESIS_REFUTED,
    TOKEN_COUNT_DRIFT,
    BlockingError,
)

MODELS = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]


# ------------------------------------------------------------- fixtures -----


@pytest.fixture(scope="module")
def clone(tmp_path_factory):
    if not (_have(PR25_BASE) and _have(PR25_HEAD)):
        pytest.skip("PR25 objects absent")
    dst = tmp_path_factory.mktemp("exec-clone")
    subprocess.run(["git", "clone", "-q", "--no-local", str(ROOT), str(dst)],
                   check=True, capture_output=True)
    return dst


@pytest.fixture(scope="module")
def skeleton(clone):
    return plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=clone)


@pytest.fixture(scope="module")
def plan_record(skeleton, clone):
    return finalize.finalize(
        skeleton, cwd=clone, operator_pins=good_pins(),
        transport=counting.MockCountTransport(),
        authorizations=proposed_authorizations(skeleton, clone))


def _run(plan_record, skeleton, clone, *, transport=None, **over):
    return executor.execute_mock(
        plan_record, transport=transport or executor.MockGenerationTransport(),
        skeleton=skeleton, cwd=clone,
        operator_pins=over.pop("operator_pins", good_pins()),
        authorizations=over.pop(
            "authorizations", proposed_authorizations(skeleton, clone)))


def _policy(min_others=1):
    return reviewpolicy.policy_record(
        MODELS, required_approver="gpt-5.6-sol",
        minimum_other_approvers=min_others, max_output_tokens=8_000)


def _minimal_request_body() -> bytes:
    schema = providerreq.verdict_schema(["a" * 64], challenge="CH-X")
    return json.dumps({"model": MODELS[0],
                       "text": {"format": schema}}).encode()


def _assembly_for(plan_record, skeleton, clone, batch, model_id):
    payloads = executor.rebuild_payloads(skeleton, plan_record, cwd=clone)
    paths = executor._path_identities(skeleton, plan_record["final_units"])
    return executor.reconstruct_batch_requests(
        plan_record, batch, payloads_by_unit=payloads,
        path_bytes_b64_by_unit=paths)[model_id]


# --------------------------------------------------------- happy path -------


class TestHappyPath:
    def test_every_unit_is_approved_and_nothing_real_was_called(
            self, plan_record, skeleton, clone):
        transport = executor.MockGenerationTransport()
        report = _run(plan_record, skeleton, clone, transport=transport)
        assert report["synthesis"]["overall_approved"] is True
        assert transport.source == counting.SOURCE_MOCK
        assert report["evidence_class"] == evidence.MOCK_TEST_EVIDENCE
        assert report["executable_authority"] is False

    def test_the_generation_timeout_reaches_the_transport(
            self, plan_record, skeleton, clone):
        transport = executor.MockGenerationTransport()
        _run(plan_record, skeleton, clone, transport=transport)
        assert transport.last_timeout == good_pins()[
            "VERIFIER_GENERATION_TIMEOUT_SECONDS"]

    def test_the_challenge_comes_from_the_plan_not_the_caller(
            self, plan_record, skeleton, clone):
        # C4-F13: there is no challenge parameter to override.
        import inspect
        assert "challenge" not in inspect.signature(
            executor.execute_mock).parameters
        report = _run(plan_record, skeleton, clone)
        assert report["execution_challenge"] == plan_record[
            "execution_challenge"]


# ------------------------------------------------------- preconditions ------


class TestExecutionPreconditions:
    def test_an_arbitrary_transport_is_refused_before_any_call(
            self, plan_record, skeleton, clone):
        # C4-F08: candidate-side execution may not transmit at all.
        class Eager:
            source = counting.SOURCE_PROVIDER

            def __init__(self):
                self.calls = 0

            def post(self, path, body, *, timeout=None):
                self.calls += 1
                return 200, b"{}"

        transport = Eager()
        with pytest.raises(BlockingError) as e:
            _run(plan_record, skeleton, clone, transport=transport)
        assert "requires_mock_transport" in str(e.value)
        assert transport.calls == 0

    def test_a_transport_without_the_class_declaration_is_refused(self):
        class Undeclared:
            source = counting.SOURCE_MOCK

            def post(self, path, body, *, timeout=None):
                return 200, b"{}"

        with pytest.raises(BlockingError) as e:
            executor.assert_mock_generation_transport(Undeclared())
        assert "generation_transport_class_undeclared" in str(e.value)

    def test_a_transport_with_a_network_method_is_refused(self):
        class Networked(executor.MockGenerationTransport):
            def connect(self):
                pass

        with pytest.raises(BlockingError) as e:
            executor.assert_mock_generation_transport(Networked())
        assert "network_method" in str(e.value)

    def test_a_transport_that_cannot_be_timed_out_is_refused(self):
        class NoTimeout:
            source = counting.SOURCE_MOCK
            generation_transport_class = "MOCK_NOT_PROVIDER"

            def post(self, path, body):
                return 200, b"{}"

        with pytest.raises(BlockingError) as e:
            executor.assert_mock_generation_transport(NoTimeout())
        assert "timeout_protocol" in str(e.value)

    def test_a_tampered_report_is_refused_before_any_call(
            self, plan_record, skeleton, clone):
        # C4-F05: the executor strict-loads and REBUILDS first. Editing a
        # count and recomputing the digest does not survive reconstruction.
        tampered = copy.deepcopy(plan_record)
        tampered["batches"][0]["input_tokens_by_model"][MODELS[0]] += 1
        tampered["mock_finalization_report_sha256"] = (
            finalize.mock_report_digest(tampered))
        transport = executor.MockGenerationTransport()
        with pytest.raises(BlockingError):
            _run(tampered, skeleton, clone, transport=transport)
        assert transport.calls == 0

    def test_a_request_that_is_not_the_counted_request_blocks(
            self, plan_record, skeleton, clone):
        # C4-F06: hash equality, not token arithmetic. Corrupt the stored
        # request hash so the rebuilt request cannot match it.
        tampered = copy.deepcopy(plan_record)
        batch = tampered["batches"][0]
        batch["request_hashes_by_model"][MODELS[0]][
            "request_semantics_sha256"] = "0" * 64
        with pytest.raises(BlockingError) as e:
            executor.assert_request_matches_plan(
                _assembly_for(tampered, skeleton, clone, batch, MODELS[0]),
                batch, MODELS[0])
        assert e.value.code == EXECUTABLE_PLAN_INVALID
        assert "request_hash_mismatch" in str(e.value)

    def test_a_batch_without_request_hashes_blocks(self):
        with pytest.raises(BlockingError) as e:
            executor.assert_request_matches_plan(
                None, {"batch_id": "b0"}, MODELS[0])
        assert "batch_has_no_request_hashes" in str(e.value)


# -------------------------------------------------- execution preflight -----


class TestGlobalExecutionPreflight:
    def test_every_execution_payload_is_scanned_before_any_call(
            self, plan_record, skeleton, clone):
        report = _run(plan_record, skeleton, clone)
        manifest = report["execution_preflight_manifest"]
        expected = len(plan_record["batches"]) * len(MODELS)
        assert manifest["scanned_execution_payload_count"] == expected
        assert manifest["generation_attempts_at_seal"] == 0
        for entry in manifest["entries"]:
            assert len(entry["execution_payload_scan"]["payload_sha256"]) == 64
            assert len(
                entry["execution_payload_scan"]["origin_map_sha256"]) == 64

    def test_a_secret_reaching_execution_costs_zero_generation_calls(
            self, plan_record, skeleton, clone):
        # C4-F07: drop the authorizations at EXECUTION time. Every payload is
        # scanned as one sealed set, so the first batch is never sent.
        transport = executor.MockGenerationTransport()
        with pytest.raises(BlockingError) as e:
            executor.execute_mock(
                plan_record, transport=transport, skeleton=skeleton,
                cwd=clone, operator_pins=good_pins(), authorizations=None)
        assert e.value.code == SECRET_PREFLIGHT_FAILED
        assert transport.calls == 0


# ------------------------------------------------------- the panel gates ----


class TestRequiredApprover:
    def test_a_sol_refutation_of_any_confidence_vetoes(self, plan_record,
                                                       skeleton, clone):
        units = set(plan_record["batches"][0]["unit_sha256_in_order"])
        transport = executor.MockGenerationTransport(
            refute={"gpt-5.6-sol": units})
        with pytest.raises(BlockingError) as e:
            _run(plan_record, skeleton, clone, transport=transport)
        assert e.value.code == REQUIRED_APPROVER_REFUTED

    def test_a_missing_approver_blocks(self):
        by_model = {
            "gpt-5.3-codex": {"a" * 64: {"refuted": False}},
            "gpt-4.1-mini": {"a" * 64: {"refuted": False}},
        }
        with pytest.raises(BlockingError) as e:
            executor._decide_unit("a" * 64, by_model, approver="gpt-5.6-sol",
                                  model_ids=MODELS, min_others=1)
        assert e.value.code == REQUIRED_APPROVER_MISSING


class TestCorroboration:
    def test_too_few_distinct_others_blocks(self, plan_record, skeleton,
                                            clone):
        units = set(plan_record["batches"][0]["unit_sha256_in_order"])
        transport = executor.MockGenerationTransport(
            refute={"gpt-5.3-codex": units, "gpt-4.1-mini": units})
        with pytest.raises(BlockingError) as e:
            _run(plan_record, skeleton, clone, transport=transport)
        assert e.value.code == INSUFFICIENT_CORROBORATION

    def test_one_distinct_other_approver_is_enough(self, plan_record,
                                                   skeleton, clone):
        units = set(plan_record["batches"][0]["unit_sha256_in_order"])
        transport = executor.MockGenerationTransport(
            refute={"gpt-4.1-mini": units})
        report = _run(plan_record, skeleton, clone, transport=transport)
        first = report["batch_results"][0]
        for decision in first["unit_decisions"].values():
            assert decision["distinct_other_approvers"] == 1
            assert "gpt-4.1-mini" in decision["refuted_by"]


class TestAntiCannedReasoning:
    """C4-F12: the legacy distinct-reason gate, restored."""

    def test_identical_approval_reasons_from_two_models_block(
            self, plan_record, skeleton, clone):
        transport = executor.MockGenerationTransport(identical_reasons=True)
        with pytest.raises(BlockingError) as e:
            _run(plan_record, skeleton, clone, transport=transport)
        assert "canned_identical" in str(e.value)

    def test_distinct_reasons_pass(self, plan_record, skeleton, clone):
        report = _run(plan_record, skeleton, clone)
        decision = next(iter(
            report["batch_results"][0]["unit_decisions"].values()))
        assert decision["distinct_reasoning"]["distinct_reasoning_count"] >= 1

    def test_the_challenge_alone_is_not_a_reason(self):
        challenge = "CH-1"
        by_model = {
            "gpt-5.6-sol": {"u": {"refuted": False, "reason": challenge,
                                  "proof_of_check": f"{challenge} looked"}},
            "gpt-4.1-mini": {"u": {"refuted": False,
                                   "reason": f"{challenge} something else",
                                   "proof_of_check": f"{challenge} read it"}},
        }
        with pytest.raises(BlockingError) as e:
            verdicts.assert_distinct_reasoning(
                by_model, unit_hash="u", approver="gpt-5.6-sol",
                challenge=challenge, corroborators=["gpt-4.1-mini"])
        assert "only_the_challenge" in str(e.value)

    def test_a_refutation_may_repeat_another_models_words(self):
        # Two models describing the same real defect identically is
        # agreement, not collusion. The gate applies to approvals.
        challenge = "CH-1"
        same = f"{challenge} the null check was removed"
        by_model = {
            "gpt-5.6-sol": {"u": {"refuted": False,
                                  "reason": f"{challenge} ok",
                                  "proof_of_check": f"{challenge} read"}},
            "gpt-4.1-mini": {"u": {"refuted": True, "reason": same,
                                   "proof_of_check": same}},
        }
        verdicts.assert_distinct_reasoning(
            by_model, unit_hash="u", approver="gpt-5.6-sol",
            challenge=challenge, corroborators=["gpt-4.1-mini"])


# ------------------------------------------------ response envelope ---------


class TestResponseEnvelope:
    def _mutated(self, mutate):
        base = executor.MockGenerationTransport()

        class Mutated(executor.MockGenerationTransport):
            def post(self, path, body, *, timeout=None):
                status, out = base.post(path, body, timeout=timeout)
                return mutate(status, out)
        return Mutated()

    def _expect_block(self, plan_record, skeleton, clone, mutate):
        with pytest.raises(BlockingError) as e:
            _run(plan_record, skeleton, clone,
                 transport=self._mutated(mutate))
        return e.value

    def test_a_wrong_challenge_echo_blocks(self, plan_record, skeleton,
                                           clone):
        def mutate(status, out):
            parsed = json.loads(out)
            parsed["output_parsed"]["challenge"] = "WRONG"
            return status, json.dumps(parsed).encode()
        error = self._expect_block(plan_record, skeleton, clone, mutate)
        assert error.code == PROVIDER_RESPONSE_INVALID

    def test_a_model_identity_mismatch_blocks(self, plan_record, skeleton,
                                              clone):
        def mutate(status, out):
            parsed = json.loads(out)
            parsed["model"] = "gpt-does-not-exist"
            return status, json.dumps(parsed).encode()
        assert "model_mismatch" in str(
            self._expect_block(plan_record, skeleton, clone, mutate))

    def test_a_missing_usage_object_blocks(self, plan_record, skeleton,
                                           clone):
        # The old check allowed a missing key as long as none were unknown.
        def mutate(status, out):
            parsed = json.loads(out)
            parsed.pop("usage")
            return status, json.dumps(parsed).encode()
        assert "envelope_key_set" in str(
            self._expect_block(plan_record, skeleton, clone, mutate))

    def test_a_bool_token_count_blocks(self, plan_record, skeleton, clone):
        # True is an int in Python; an isinstance check alone lets it through.
        def mutate(status, out):
            parsed = json.loads(out)
            parsed["usage"]["output_tokens"] = True
            return status, json.dumps(parsed).encode()
        assert "plain_nonneg_int" in str(
            self._expect_block(plan_record, skeleton, clone, mutate))

    def test_a_negative_token_count_blocks(self, plan_record, skeleton,
                                           clone):
        def mutate(status, out):
            parsed = json.loads(out)
            parsed["usage"]["input_tokens"] = -1
            return status, json.dumps(parsed).encode()
        assert "plain_nonneg_int" in str(
            self._expect_block(plan_record, skeleton, clone, mutate))

    def test_a_wrong_object_tag_blocks(self, plan_record, skeleton, clone):
        def mutate(status, out):
            parsed = json.loads(out)
            parsed["object"] = "response.chunk"
            return status, json.dumps(parsed).encode()
        assert "object_tag_unexpected" in str(
            self._expect_block(plan_record, skeleton, clone, mutate))

    def test_a_duplicate_json_key_blocks(self, plan_record, skeleton, clone):
        def mutate(status, out):
            raw = out.decode()
            raw = raw.replace('"model"', '"model": "x", "model"', 1)
            return status, raw.encode()
        self._expect_block(plan_record, skeleton, clone, mutate)

    def test_an_oversized_body_blocks(self):
        body = b'{"object":"response"}' + b" " * (
            executor.MAX_GENERATION_RESPONSE_BYTES + 1)
        with pytest.raises(BlockingError) as e:
            executor.validate_response_envelope(200, body,
                                                model_id=MODELS[0])
        assert "oversized" in str(e.value)

    def test_a_missing_unit_verdict_blocks(self, plan_record, skeleton,
                                           clone):
        def mutate(status, out):
            parsed = json.loads(out)
            units = list(parsed["output_parsed"]["verdicts_by_unit"])
            parsed["output_parsed"]["verdicts_by_unit"].pop(units[0])
            return status, json.dumps(parsed).encode()
        self._expect_block(plan_record, skeleton, clone, mutate)


# ----------------------------------------------------- retries and ledger ---


class TestGenerationLedger:
    def test_retries_are_counted_separately_from_logical_requests(self):
        # C4-F09: `retries = attempts - logical_requests` was zero by
        # construction, because both were incremented in the same call.
        ledger = executor.GenerationLedger(
            good_pins(VERIFIER_GENERATION_MAX_RETRIES=2,
                      VERIFIER_MAX_GENERATION_CALLS=64))

        class Flaky:
            source = counting.SOURCE_MOCK
            generation_transport_class = "MOCK_NOT_PROVIDER"

            def __init__(self):
                self.calls = 0

            def post(self, path, body, *, timeout=None):
                self.calls += 1
                if self.calls < 3:
                    return 503, b""
                return executor.MockGenerationTransport().post(
                    path, body, timeout=timeout)

        executor._post_with_retries(Flaky(), _minimal_request_body(),
                                    model_id=MODELS[0], batch_id="b0",
                                    ledger=ledger, timeout=30)
        record = ledger.record()
        assert record["logical_generation_requests"] == 1
        assert record["generation_attempts"] == 3
        assert record["retries"] == 2
        assert record["failed_attempt_count"] == 2

    def test_a_deterministic_failure_is_not_retried(self):
        ledger = executor.GenerationLedger(good_pins())

        class Broken:
            source = counting.SOURCE_MOCK
            generation_transport_class = "MOCK_NOT_PROVIDER"

            def __init__(self):
                self.calls = 0

            def post(self, path, body, *, timeout=None):
                self.calls += 1
                return 400, b"{}"

        transport = Broken()
        with pytest.raises(BlockingError):
            executor._post_with_retries(transport, _minimal_request_body(),
                                        model_id=MODELS[0], batch_id="b0",
                                        ledger=ledger, timeout=30)
        assert transport.calls == 1
        assert ledger.record()["retries"] == 0

    def test_the_retry_budget_is_reserved_before_the_first_attempt(self):
        pins = good_pins(VERIFIER_MAX_GENERATION_CALLS=2,
                         VERIFIER_GENERATION_MAX_RETRIES=2)
        ledger = executor.GenerationLedger(pins)
        with pytest.raises(BlockingError) as e:
            ledger.open_request(MODELS[0], "b0")
        assert e.value.code == CHUNK_COUNT_EXHAUSTED
        assert ledger.attempts == 0

    def test_every_attempt_is_settled(self, plan_record, skeleton, clone):
        report = _run(plan_record, skeleton, clone)
        ledger = report["generation_ledger"]
        assert ledger["generation_attempts"] == len(
            plan_record["batches"]) * len(MODELS)
        assert all(a["result_category"] == "ok" for a in ledger["attempts"])
        assert "not provider usage" in ledger["honest_scope"]


# ------------------------------------------------------- usage drift --------


class TestUsageDrift:
    def test_usage_far_from_the_count_blocks(self, plan_record, skeleton,
                                             clone):
        transport = executor.MockGenerationTransport(usage_input_tokens=1)
        with pytest.raises(BlockingError) as e:
            _run(plan_record, skeleton, clone, transport=transport)
        assert e.value.code == TOKEN_COUNT_DRIFT


# ------------------------------------------------------- verdict evidence ---


class TestFullVerdictEvidence:
    def test_the_full_validated_verdict_is_retained(self, plan_record,
                                                    skeleton, clone):
        # C4-F11: confidence, reason, proof and categories were validated and
        # then discarded, so the evidence could show THAT Sol approved and
        # never WHY.
        report = _run(plan_record, skeleton, clone)
        records = report["batch_results"][0]["per_model_verdict_evidence"]
        assert {r["model_id"] for r in records} == set(MODELS)
        for record in records:
            assert record["publication_class"] == "private"
            assert record["challenge"] == plan_record["execution_challenge"]
            assert len(record["request_semantics_sha256"]) == 64
            for verdict in record["verdicts_by_unit"].values():
                assert verdict["confidence"] in reviewpolicy.CONFIDENCE_VALUES
                assert verdict["reason"]
                assert verdict["proof_of_check"]
                assert verdict["checked_categories"]
                assert len(verdict["reason_sha256"]) == 64

    def test_provider_output_is_scanned_for_secrets(self, plan_record,
                                                    skeleton, clone):
        report = _run(plan_record, skeleton, clone)
        assert report["output_privacy_scan"]["scanned_field_count"] > 0

    def test_a_secret_echoed_in_a_reason_blocks(self):
        leaked = "sk-proj-" + "abcdef1234567890abcdef"  # pragma: allowlist secret
        record = {
            "model_id": MODELS[0],
            "verdicts_by_unit": {
                "a" * 64: {"reason": f"I saw {leaked} here",
                           "proof_of_check": "read it"},
            },
        }
        with pytest.raises(BlockingError) as e:
            executor.assert_output_carries_no_secret(
                [record], path_identities=frozenset())
        assert "secret_in_provider_output" in str(e.value)

    def test_a_path_identity_echoed_in_a_proof_blocks(self):
        record = {
            "model_id": MODELS[0],
            "verdicts_by_unit": {
                "a" * 64: {"reason": "fine", "proof_of_check": "saw cA=="},
            },
        }
        with pytest.raises(BlockingError) as e:
            executor.assert_output_carries_no_secret(
                [record], path_identities=frozenset({"cA=="}))
        assert "path_identity_in_provider_output" in str(e.value)


# ---------------------------------------------------------- synthesis -------


class TestSynthesis:
    def test_a_refutation_is_final_across_synthesis(self, plan_record,
                                                    skeleton, clone):
        units = set(plan_record["batches"][0]["unit_sha256_in_order"])
        transport = executor.MockGenerationTransport(
            refute={"gpt-4.1-mini": units})
        report = _run(plan_record, skeleton, clone, transport=transport)
        assert report["synthesis"]["overall_approved"] is False
        assert set(report["synthesis"]["refuted_unit_sha256"]) >= units

    def test_synthesis_cannot_move_a_refuted_unit_to_approved(self):
        contradictory = [
            {"unit_decisions": {"a" * 64: {"approved": False}}},
            {"unit_decisions": {"a" * 64: {"approved": True}}},
        ]
        with pytest.raises(BlockingError) as e:
            executor.synthesize(contradictory)
        assert e.value.code == SYNTHESIS_REFUTED


# ------------------------------------------------------- strict loader ------


class TestStrictExecutionLoader:
    def test_a_faithful_report_reconstructs(self, plan_record, skeleton,
                                            clone):
        report = _run(plan_record, skeleton, clone)
        result = executor.validate_mock_execution_strict(
            report, mock_finalization_report=plan_record, skeleton=skeleton,
            cwd=clone, operator_pins=good_pins(),
            authorizations=proposed_authorizations(skeleton, clone))
        assert result["reconstructed"] is True

    def test_an_edited_decision_does_not_survive_reconstruction(
            self, plan_record, skeleton, clone):
        # C4-F16: recomputing the outer digest is not validation.
        report = _run(plan_record, skeleton, clone)
        tampered = copy.deepcopy(report)
        tampered["synthesis"]["overall_approved"] = False
        tampered["mock_execution_report_sha256"] = (
            executor.mock_execution_digest(tampered))
        with pytest.raises(BlockingError) as e:
            executor.validate_mock_execution_strict(
                tampered, mock_finalization_report=plan_record,
                skeleton=skeleton, cwd=clone, operator_pins=good_pins(),
                authorizations=proposed_authorizations(skeleton, clone))
        assert "not_reproducible" in str(e.value)

    def test_a_report_claiming_authority_is_refused(self, plan_record,
                                                    skeleton, clone):
        report = _run(plan_record, skeleton, clone)
        tampered = copy.deepcopy(report)
        tampered["executable_authority"] = True
        tampered["mock_execution_report_sha256"] = (
            executor.mock_execution_digest(tampered))
        with pytest.raises(BlockingError) as e:
            executor.validate_mock_execution_strict(
                tampered, mock_finalization_report=plan_record,
                skeleton=skeleton, cwd=clone, operator_pins=good_pins())
        assert "claims_authority" in str(e.value)


class TestVerdictsModule:
    def test_a_proof_that_is_only_the_challenge_blocks(self):
        challenge = "CH-EXEC-001"
        unit = "a" * 64
        parsed = {"challenge": challenge, "verdicts_by_unit": {
            unit: {"refuted": False, "confidence": "high",
                   "reason": challenge + " a substantive enough reason",
                   "proof_of_check": challenge + "          ",
                   "checked_categories": ["logic"]}}}
        with pytest.raises(BlockingError) as e:
            verdicts.validate_verdicts(parsed, unit_hashes=[unit],
                                       challenge=challenge,
                                       review_policy=_policy())
        assert "only_the_challenge" in str(e.value)
