"""MC4 PASS C — the mock Stage-3 executor and its integrity gates.

Every provider interaction goes through an injected mock transport, so "no
real model call happened" is structural. The gates are the legacy panel's,
restored and driven adversarially: challenge echo, required approver, distinct
corroborators, usage drift, and a synthesis that cannot clear a refutation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verifier import (  # noqa: E402
    counting,
    evidence,
    executor,
    reviewpolicy,
    verdicts,
)
from verifier.errors import (  # noqa: E402
    INSUFFICIENT_CORROBORATION,
    PROVIDER_RESPONSE_INVALID,
    REQUIRED_APPROVER_MISSING,
    REQUIRED_APPROVER_REFUTED,
    SYNTHESIS_REFUTED,
    TOKEN_COUNT_DRIFT,
    BlockingError,
)

MODELS = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
CHALLENGE = "CH-EXEC-001"
UNITS = ["a" * 64, "b" * 64]


def _pins(**over):
    from test_verifier_finalize import good_pins
    return good_pins(**over)


def _policy(min_others=1):
    return reviewpolicy.policy_record(
        MODELS, required_approver="gpt-5.6-sol",
        minimum_other_approvers=min_others, max_output_tokens=8_000)


def _batch():
    # Only the fields the executor reads.
    return {"batch_id": "batch-0000", "unit_sha256_in_order": list(UNITS),
            "input_tokens_by_model": {m: None for m in MODELS}}


def _run(transport, *, policy=None, batch=None, counted=None, pins=None):
    return executor.execute_batch(
        batch or _batch(), policy or _policy(), transport=transport,
        pin_values=pins or _pins(), challenge=CHALLENGE,
        counted_by_model=counted or {m: None for m in MODELS})


class TestHappyPath:
    def test_all_green_approves_every_unit(self):
        result = _run(executor.MockGenerationTransport())
        for unit_hash in UNITS:
            assert result["unit_decisions"][unit_hash]["approved"] is True

    def test_zero_real_calls_but_the_mock_was_used(self):
        transport = executor.MockGenerationTransport()
        _run(transport)
        assert transport.calls == len(MODELS)
        assert transport.source == counting.SOURCE_MOCK

    def test_the_generation_timeout_reaches_the_transport(self):
        transport = executor.MockGenerationTransport()
        _run(transport, pins=_pins(VERIFIER_GENERATION_TIMEOUT_SECONDS=99))
        assert transport.last_timeout == 99


class TestRequiredApprover:
    def test_a_sol_refutation_of_any_confidence_vetoes(self):
        transport = executor.MockGenerationTransport(
            refute={"gpt-5.6-sol": {UNITS[0]}})
        with pytest.raises(BlockingError) as e:
            _run(transport)
        assert e.value.code == REQUIRED_APPROVER_REFUTED

    def test_a_missing_approver_blocks(self):
        # A policy whose approver is not in the panel cannot even be built,
        # so drive the decision function directly with the approver absent.
        by_model = {
            "gpt-5.3-codex": {UNITS[0]: {"refuted": False}},
            "gpt-4.1-mini": {UNITS[0]: {"refuted": False}},
        }
        with pytest.raises(BlockingError) as e:
            executor._decide_unit(UNITS[0], by_model, approver="gpt-5.6-sol",
                                  model_ids=MODELS, min_others=1)
        assert e.value.code == REQUIRED_APPROVER_MISSING


class TestCorroboration:
    def test_one_distinct_other_approver_is_enough(self):
        # Sol approves, codex approves, mini refutes: still one distinct
        # other approver (codex).
        transport = executor.MockGenerationTransport(
            refute={"gpt-4.1-mini": set(UNITS)})
        result = _run(transport, policy=_policy(min_others=1))
        # units are refuted (mini refuted) but the required approver approved
        # and one other approved, so the ROLE gate passes; the unit records
        # that mini refuted it.
        for unit_hash in UNITS:
            decision = result["unit_decisions"][unit_hash]
            assert decision["distinct_other_approvers"] == 1
            assert "gpt-4.1-mini" in decision["refuted_by"]

    def test_too_few_distinct_others_blocks(self):
        # Both non-approver models refute -> zero distinct other approvers.
        transport = executor.MockGenerationTransport(
            refute={"gpt-5.3-codex": set(UNITS),
                    "gpt-4.1-mini": set(UNITS)})
        with pytest.raises(BlockingError) as e:
            _run(transport, policy=_policy(min_others=1))
        assert e.value.code == INSUFFICIENT_CORROBORATION


class TestResponseValidation:
    def _transport(self, mutate):
        base = executor.MockGenerationTransport()

        class Mutated(executor.MockGenerationTransport):
            def post(self, path, body, *, timeout=None):
                status, out = base.post(path, body, timeout=timeout)
                return mutate(status, out)
        return Mutated()

    def test_a_wrong_challenge_echo_blocks(self):
        def mutate(status, out):
            parsed = json.loads(out)
            parsed["output_parsed"]["challenge"] = "WRONG"
            return status, json.dumps(parsed).encode()
        with pytest.raises(BlockingError) as e:
            _run(self._transport(mutate))
        assert e.value.code == PROVIDER_RESPONSE_INVALID

    def test_a_model_identity_mismatch_blocks(self):
        def mutate(status, out):
            parsed = json.loads(out)
            parsed["model"] = "gpt-does-not-exist"
            return status, json.dumps(parsed).encode()
        with pytest.raises(BlockingError) as e:
            _run(self._transport(mutate))
        assert "model_mismatch" in str(e.value)

    def test_a_missing_unit_verdict_blocks(self):
        def mutate(status, out):
            parsed = json.loads(out)
            parsed["output_parsed"]["verdicts_by_unit"].pop(UNITS[1])
            return status, json.dumps(parsed).encode()
        with pytest.raises(BlockingError):
            _run(self._transport(mutate))

    def test_a_duplicate_json_key_blocks(self):
        def mutate(status, out):
            # Inject a duplicate top-level key into the raw bytes.
            raw = out.decode()
            assert '"model"' in raw
            raw = raw.replace('"model"', '"model": "x", "model"', 1)
            return status, raw.encode()
        with pytest.raises(BlockingError):
            _run(self._transport(mutate))


class TestUsageDrift:
    def test_usage_far_from_the_count_blocks(self):
        transport = executor.MockGenerationTransport(usage_input_tokens=1)
        with pytest.raises(BlockingError) as e:
            _run(transport, counted={m: 100_000 for m in MODELS})
        assert e.value.code == TOKEN_COUNT_DRIFT

    def test_usage_within_tolerance_passes(self):
        transport = executor.MockGenerationTransport(usage_input_tokens=1000)
        _run(transport, counted={m: 1000 for m in MODELS},
             pins=_pins(VERIFIER_TOKEN_DRIFT_TOLERANCE=64))


class TestSynthesis:
    def test_a_refutation_is_final_across_synthesis(self):
        # One unit refuted by mini; synthesis must keep it refuted.
        transport = executor.MockGenerationTransport(
            refute={"gpt-4.1-mini": {UNITS[0]}})
        result = _run(transport)
        synthesis = executor.synthesize([result])
        assert UNITS[0] in synthesis["refuted_unit_sha256"]
        assert synthesis["overall_approved"] is False

    def test_synthesis_cannot_move_a_refuted_unit_to_approved(self):
        # Hand-craft contradictory batch results: the same unit both refuted
        # and approved. Synthesis must catch the override attempt.
        contradictory = [
            {"unit_decisions": {UNITS[0]: {"approved": False}}},
            {"unit_decisions": {UNITS[0]: {"approved": True}}},
        ]
        with pytest.raises(BlockingError) as e:
            executor.synthesize(contradictory)
        assert e.value.code == SYNTHESIS_REFUTED


class TestExecuteMockEndToEnd:
    def test_a_mock_execution_report_is_non_executable(self):
        plan_record = {
            "review_skeleton_sha256": "f" * 64,
            "executable_plan_sha256": "e" * 64,
            "review_request_policy": _policy(),
            "operator_pin_record": {"pins": _pins()},
            "batches": [_batch()],
        }
        report = executor.execute_mock(
            plan_record, transport=executor.MockGenerationTransport(),
            challenge=CHALLENGE)
        assert report["artifact"] == "mock-execution-report"
        assert report["evidence_class"] == evidence.MOCK_TEST_EVIDENCE
        assert report["executable_authority"] is False
        assert report["synthesis"]["overall_approved"] is True
        assert report["generation_attempts_performed"] == len(MODELS)


class TestVerdictsModule:
    def test_a_proof_that_is_only_the_challenge_blocks(self):
        policy = _policy()
        parsed = {"challenge": CHALLENGE, "verdicts_by_unit": {
            UNITS[0]: {"refuted": False, "confidence": "high",
                       "reason": CHALLENGE + " a substantive enough reason",
                       # long enough to pass the length bound, but nothing
                       # after the challenge is substantive.
                       "proof_of_check": CHALLENGE + "          ",
                       "checked_categories": ["logic"]}}}
        with pytest.raises(BlockingError) as e:
            verdicts.validate_verdicts(parsed, unit_hashes=[UNITS[0]],
                                       challenge=CHALLENGE,
                                       review_policy=policy)
        assert "only_the_challenge" in str(e.value)
