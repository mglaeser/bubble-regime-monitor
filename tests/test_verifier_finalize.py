"""Stage-2 finalizer: PINs, requests, preflight, counts, batching, cost.

Every provider interaction goes through an INJECTED transport, so "no real
call happened" is structural. A mock count is never allowed to make a plan
executable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import (  # noqa: E402
    batching,
    capabilities,
    counting,
    finalize,
    plan,
    preflight,
    providerreq,
)
from verifier import (
    pins as pinsmod,
)
from verifier.errors import (  # noqa: E402
    COST_CAP_EXCEEDED,
    MODEL_CONTEXT_EXCEEDED_UNSPLITTABLE,
    SECRET_PREFLIGHT_FAILED,
    TOKEN_COUNT_RESPONSE_INVALID,
    TOKEN_COUNT_RETRY_EXHAUSTED,
    UNKNOWN_MODEL_CAPABILITY,
    UNSET_POLICY_PIN,
    BlockingError,
)

ROOT = Path(__file__).resolve().parents[1]
PR25_BASE = "75a093de45f73169072837c7c062fab421caaf8b"  # pragma: allowlist secret
PR25_HEAD = "b08844a0755710035d62830faa84902d9d85d3fe"  # pragma: allowlist secret
MODELS = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]


def good_pins(**over):
    values = {
        "VERIFIER_MAX_OUTPUT_TOKENS": 8_000,
        "VERIFIER_CONTEXT_MARGIN_TOKENS": 4_000,
        "VERIFIER_COST_CAP_MICRO_USD": 50_000_000,          # 50 USD in micros
        "VERIFIER_REASONING_EFFORT_BY_MODEL": {
            "gpt-5.3-codex": "medium",
            "gpt-5.6-sol": "medium",
            "gpt-4.1-mini": None,
        },
        "VERIFIER_MAX_REVIEW_UNITS": 5_000,
        "VERIFIER_MAX_GENERATION_CALLS": 5_000,
        "VERIFIER_MAX_COUNT_CALLS": 100_000,
        "VERIFIER_COUNT_TIMEOUT_SECONDS": 30,
        "VERIFIER_COUNT_MAX_RETRIES": 2,
        "VERIFIER_GENERATION_TIMEOUT_SECONDS": 120,
        "VERIFIER_GENERATION_MAX_RETRIES": 1,
        "VERIFIER_TOKEN_DRIFT_TOLERANCE": 64,
    }
    values.update(over)
    return values


def _have(sha):
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                          cwd=ROOT, capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def clone(tmp_path_factory):
    if not (_have(PR25_BASE) and _have(PR25_HEAD)):
        pytest.skip("PR25 objects absent")
    dst = tmp_path_factory.mktemp("clone")
    subprocess.run(["git", "clone", "-q", "--no-local", str(ROOT), str(dst)],
                   check=True, capture_output=True)
    return dst


@pytest.fixture(scope="module")
def skeleton(clone):
    return plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=clone)


class TestCapabilityPolicy:
    def test_official_facts_are_pinned(self):
        codex = capabilities.capability("gpt-5.3-codex")
        assert codex.context_window_tokens == 400_000
        assert codex.max_output_tokens_supported == 128_000
        assert codex.input_micro_usd_per_million == 1_750_000
        sol = capabilities.capability("gpt-5.6-sol")
        assert sol.context_window_tokens == 1_050_000
        assert sol.long_context_threshold_input_tokens == 272_000
        assert sol.above_threshold_input_multiplier_bp == 20_000
        mini = capabilities.capability("gpt-4.1-mini")
        assert mini.context_window_tokens == 1_047_576
        assert mini.max_output_tokens_supported == 32_768
        assert mini.supports_reasoning is False
        assert mini.reasoning_efforts == ()

    def test_unknown_model_blocks_with_no_fallback(self):
        with pytest.raises(BlockingError) as e:
            capabilities.capability("gpt-does-not-exist")
        assert e.value.code == UNKNOWN_MODEL_CAPABILITY

    def test_policy_record_is_provisional_and_hashed(self):
        record = capabilities.policy_record(MODELS)
        assert record["status"] == "PROVISIONAL_IN_REPOSITORY"
        assert capabilities.policy_digest(record) == record[
            "capability_policy_sha256"]


class TestOperatorPins:
    def test_complete_record_validates(self):
        pinsmod.validate_pins(good_pins(), MODELS)

    @pytest.mark.parametrize("mutate", [
        lambda p: p.pop("VERIFIER_COST_CAP_MICRO_USD"),
        lambda p: p.update(VERIFIER_MAX_OUTPUT_TOKENS=200_000),   # > support
        lambda p: p.update(VERIFIER_COST_CAP_MICRO_USD=1.5),            # float money
        lambda p: p.update(VERIFIER_COUNT_MAX_RETRIES=True),      # bool as int
        lambda p: p["VERIFIER_REASONING_EFFORT_BY_MODEL"].update(
            {"gpt-4.1-mini": "high"}),                            # no reasoning
        lambda p: p["VERIFIER_REASONING_EFFORT_BY_MODEL"].update(
            {"gpt-5.3-codex": "max"}),               # not in codex's set
        lambda p: p.update(EXTRA_PIN=1),
    ])
    def test_invalid_pin_blocks_with_zero_calls(self, mutate):
        values = good_pins()
        mutate(values)
        with pytest.raises(BlockingError) as e:
            pinsmod.validate_pins(values, MODELS)
        assert e.value.code == UNSET_POLICY_PIN

    def test_pin_record_is_hashed(self):
        record = pinsmod.test_pin_record(good_pins(), MODELS)
        assert pinsmod.pin_digest(record) == record["pin_record_sha256"]


class TestProviderRequest:
    def _request(self, model_id="gpt-5.3-codex", effort="medium"):
        unit = {"unit_sha256": "a" * 64, "git_status": "M",
                "atom_ids": ["b" * 64]}
        return providerreq.build_request(model_id, [unit], ["x = 1"],
                                         reasoning_effort=effort,
                                         max_output_tokens=8_000)

    def test_three_hashes_are_distinct_and_stable(self):
        request = self._request()
        hashes = request.hashes()
        assert len({*hashes.values()}) == 3
        assert request.hashes() == hashes

    def test_count_body_omits_max_output_execution_body_carries_it(self):
        request = self._request()
        assert "max_output_tokens" not in request.count_payload()
        assert request.execution_payload()["max_output_tokens"] == 8_000
        assert (request.count_request_sha256()
                != request.execution_request_sha256())

    def test_no_tools_and_truncation_disabled(self):
        body = self._request().execution_payload()
        assert body["truncation"] == "disabled"
        for forbidden in ("tools", "tool_choice", "functions", "background"):
            assert forbidden not in body

    def test_non_reasoning_model_omits_the_field(self):
        body = self._request("gpt-4.1-mini", None).execution_payload()
        assert "reasoning" not in body
        with pytest.raises(ValueError):
            self._request("gpt-4.1-mini", "high")
        with pytest.raises(ValueError):
            self._request("gpt-5.3-codex", None)

    def test_schema_requires_one_verdict_per_unit(self):
        schema = providerreq.verdict_schema(["u1", "u2"])
        items = schema["schema"]["properties"]["verdicts"]
        assert items["minItems"] == items["maxItems"] == 2
        assert items["items"]["properties"]["unit_sha256"]["enum"] == ["u1",
                                                                      "u2"]
        assert schema["strict"] is True


class TestSecretPreflight:
    def test_clean_text_passes(self):
        record = preflight.preflight_request("def f():\n    return 1\n",
                                             label="t")
        assert record["finding_count"] == 0

    @pytest.mark.parametrize("planted", [
        "sk-proj-abcdef1234567890abcdef",          # pragma: allowlist secret
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",    # pragma: allowlist secret
        "AKIAIOSFODNN7EXAMPLE",                    # pragma: allowlist secret
        "-----BEGIN RSA PRIVATE KEY-----",   # pragma: allowlist secret
        "postgres://user:hunter2xyz@db.internal/app",  # pragma: allowlist secret
        "Authorization: Bearer abcdefghijklmnop",  # pragma: allowlist secret
    ])
    def test_planted_secret_blocks_before_any_call(self, planted):
        with pytest.raises(BlockingError) as e:
            preflight.preflight_request(f"prefix {planted} suffix", label="t")
        assert e.value.code == SECRET_PREFLIGHT_FAILED
        assert planted not in str(e.value)          # never echoed

    def test_hex_digests_are_not_flagged(self):
        preflight.preflight_request("a" * 40 + " " + "b" * 64, label="t")


class TestCountClient:
    def test_mock_counts_are_labelled_not_provider(self):
        request = TestProviderRequest()._request()
        result = counting.count_input_tokens(
            request, transport=counting.MockCountTransport(), max_retries=0)
        assert result.source == counting.SOURCE_MOCK
        assert result.input_tokens > 0

    @pytest.mark.parametrize("body", [
        b'{"object":"wrong","input_tokens":5}',
        b'{"object":"response.input_tokens","input_tokens":true}',
        b'{"object":"response.input_tokens","input_tokens":-1}',
        b'{"object":"response.input_tokens"}',
        b'not json',
        b'[]',
    ])
    def test_malformed_response_blocks(self, body):
        class T:
            source = counting.SOURCE_PROVIDER

            def post(self, path, payload):
                return 200, body
        with pytest.raises(BlockingError) as e:
            counting.count_input_tokens(TestProviderRequest()._request(),
                                        transport=T(), max_retries=0)
        assert e.value.code in (TOKEN_COUNT_RESPONSE_INVALID,
                                "TOKEN_COUNT_ENDPOINT_UNAVAILABLE")

    def test_retry_exhaustion_blocks(self):
        class T:
            source = counting.SOURCE_PROVIDER

            def post(self, path, payload):
                return 503, b""
        with pytest.raises(BlockingError) as e:
            counting.count_input_tokens(TestProviderRequest()._request(),
                                        transport=T(), max_retries=2)
        assert e.value.code == TOKEN_COUNT_RETRY_EXHAUSTED

    def test_local_failure_sentinel_cannot_be_forged_by_json(self):
        class T:
            source = counting.SOURCE_PROVIDER

            def post(self, path, payload):
                # a provider CANNOT produce a LocalFailure instance
                return 200, b'{"object":"response.input_tokens",' \
                            b'"input_tokens":{"category":"timeout"}}'
        with pytest.raises(BlockingError):
            counting.count_input_tokens(TestProviderRequest()._request(),
                                        transport=T(), max_retries=0)

    def test_deterministic_failure_is_not_retried(self):
        class T:
            source = counting.SOURCE_PROVIDER
            calls = 0

            def post(self, path, payload):
                T.calls += 1
                return 400, b""
        with pytest.raises(BlockingError):
            counting.count_input_tokens(TestProviderRequest()._request(),
                                        transport=T(), max_retries=3)
        assert T.calls == 1

    def test_no_transport_ids_are_recorded(self):
        record = counting.evidence_record(
            [], counting.resolve_models(MODELS,
                                        transport=counting.MockCountTransport()))
        assert record["transport_ids_persisted"] is False
        import json
        assert "request_id" not in json.dumps(record)


# The PR #25 range contains tests/test_probe_error_redaction.py, whose whole
# purpose is to plant FAKE credential literals and prove they never leak.
# Preflight correctly flags them, so finalizing that range needs an explicit
# operator-REVIEWED allowlist — exactly the mechanism §33 requires. These are
# the reviewed fixture strings, listed by exact value.
PR25_REVIEWED_ALLOWLIST = frozenset({
    "sk-proj-abcdef123456",                          # pragma: allowlist secret
    "sk-proj-DEADBEEFdeadbeef",                      # pragma: allowlist secret
    "sk-proj-THIS-IS-THE-REAL-KEY",                  # pragma: allowlist secret
    "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH",  # pragma: allowlist secret
    "sk-proj-not-even-an-int",                       # pragma: allowlist secret
})


class TestFinalizeEndToEnd:
    def _finalize(self, skeleton, clone, **over):
        return finalize.finalize(
            skeleton, cwd=clone, operator_pins=good_pins(**over),
            transport=counting.MockCountTransport(),
            secret_allowlist=PR25_REVIEWED_ALLOWLIST)

    def test_unreviewed_fixture_secrets_block_finalization(self, skeleton,
                                                           clone):
        # WITHOUT the reviewed allowlist the same range must refuse to count.
        transport = counting.MockCountTransport()
        with pytest.raises(BlockingError) as e:
            finalize.finalize(skeleton, cwd=clone,
                              operator_pins=good_pins(), transport=transport)
        assert e.value.code == SECRET_PREFLIGHT_FAILED
        assert transport.calls == 0

    def test_mock_counts_never_produce_an_executable_plan(self, skeleton,
                                                          clone):
        result = self._finalize(skeleton, clone)
        assert result["executable"] is False
        assert result["generation_calls_performed"] == 0
        assert result["count_calls_performed"] > 0
        assert any(p["code"] == "COUNTS_ARE_NOT_PROVIDER_EVIDENCE"
                   for p in result["pending_requirements"])
        assert result["count_evidence"]["counts"][0]["source"] == (
            counting.SOURCE_MOCK)

    def test_every_unit_lands_in_exactly_one_batch(self, skeleton, clone):
        result = self._finalize(skeleton, clone)
        batching.prove_batch_partition(result["final_units"],
                                       result["batches"])
        slots = [s for b in result["batches"]
                 for s in b["required_verdict_slots"]]
        assert sorted(slots) == sorted(u["unit_sha256"]
                                       for u in result["final_units"])

    def test_plan_digest_binds_the_record(self, skeleton, clone):
        result = self._finalize(skeleton, clone)
        assert finalize.plan_digest(result) == result["executable_plan_sha256"]
        tampered = dict(result)
        tampered["generation_calls_performed"] = 1
        assert finalize.plan_digest(tampered) != result["executable_plan_sha256"]

    def test_cost_plan_is_integer_micros(self, skeleton, clone):
        cost = self._finalize(skeleton, clone)["cost_plan"]
        assert isinstance(cost["worst_case_total_micro_usd"], int)
        assert cost["money_unit"] == "integer micro-USD"
        assert cost["count_call_billing_state"] == (
            "UNKNOWN_PENDING_OPERATOR_VERIFICATION")

    def test_cost_cap_blocks_generation(self, skeleton, clone):
        with pytest.raises(BlockingError) as e:
            self._finalize(skeleton, clone, VERIFIER_COST_CAP_MICRO_USD=1)
        assert e.value.code == COST_CAP_EXCEEDED

    def test_missing_pin_blocks_before_any_count(self, skeleton, clone):
        values = good_pins()
        del values["VERIFIER_MAX_OUTPUT_TOKENS"]
        transport = counting.MockCountTransport()
        with pytest.raises(BlockingError):
            finalize.finalize(skeleton, cwd=clone, operator_pins=values,
                              transport=transport)
        assert transport.calls == 0

    def test_stale_skeleton_blocks_before_any_count(self, skeleton, clone):
        import copy
        stale = copy.deepcopy(skeleton)
        stale["coverage"]["unit_count"] += 1
        transport = counting.MockCountTransport()
        with pytest.raises(BlockingError):
            finalize.finalize(stale, cwd=clone, operator_pins=good_pins(),
                              transport=transport)
        assert transport.calls == 0

    def test_context_gate_splits_or_blocks(self, skeleton, clone):
        # A tiny context forces recursive splitting down to single atoms and
        # then an explicit unsplittable block — never a truncation.
        class Tiny(counting.MockCountTransport):
            def post(self, path, body):
                self.calls += 1
                import json
                return 200, json.dumps(
                    {"object": counting.EXPECTED_OBJECT,
                     "input_tokens": 500_000}).encode()
        with pytest.raises(BlockingError) as e:
            finalize.finalize(skeleton, cwd=clone,
                              operator_pins=good_pins(), transport=Tiny(),
                              secret_allowlist=PR25_REVIEWED_ALLOWLIST)
        assert e.value.code == MODEL_CONTEXT_EXCEEDED_UNSPLITTABLE

    def test_public_summary_carries_no_unit_paths(self, skeleton, clone):
        result = self._finalize(skeleton, clone)
        pub = finalize.public_plan_summary(result)
        import json
        blob = json.dumps(pub)
        assert "path_bytes_b64" not in blob
        assert pub["generation_calls_performed"] == 0
        assert pub["executable"] is False
