"""MC4 A2-FINAL — the second round of reopened findings, closed.

These cover the items the A2 external review raised on top of the first five:
candidate-side verified-class downgrade, immutable generations, mock-only
transport, the governed required approver, the tightened verdict schema, the
strict count response, and the shipped CLI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_verifier_finalize import _request, good_pins  # noqa: E402
from verifier import (  # noqa: E402
    authority,
    counting,
    finalize,
    preflight,
    providerreq,
    reviewpolicy,
    verdicts,
)
from verifier import pins as pinsmod  # noqa: E402
from verifier.errors import (  # noqa: E402
    PROVIDER_RESPONSE_INVALID,
    TOKEN_COUNT_RESPONSE_INVALID,
    UNSET_POLICY_PIN,
    BlockingError,
)

RANGE = dict(repository_identity="r", target_base_sha="b" * 40,
             diff_base_sha="c" * 40, head_sha="d" * 40)
MODELS = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]


# ---------------------- A2-F01: candidate never derives trust ---------------


class TestCandidateNeverDerivesTrust:
    def _verified_literal(self):
        # A record that LABELS itself verified, built by hand.
        record = {
            "schema_version": 2,
            "authority_class": authority.VERIFIED_LITERAL_AUTHORIZATION,
            "repository_identity": RANGE["repository_identity"],
            "target_base_sha": RANGE["target_base_sha"],
            "diff_base_sha": RANGE["diff_base_sha"],
            "head_sha": RANGE["head_sha"],
            "path_bytes_b64": "cA==", "atom_id": "a" * 64,
            "occurrence_index": 0, "literal_sha256": "e" * 64,
            "literal_length": 20,
            "literal_categories": ["openai_key"],
            "literal_category_set_sha256": preflight.category_set_sha256(
                ["openai_key"]),
            "scanner_policy_sha256": preflight.scanner_policy_sha256(),
            "reason": "hand-built", "reviewer_identity": "attacker",
            "authorized_at": "t", "authorization_source": "s",
            "revoked": False,
        }
        record["authorization_sha256"] = authority.literal_authorization_digest(
            record)
        return record

    def test_a_verified_labelled_set_reports_only_unverified(self):
        # The set accepts the record as inert input but never agrees with its
        # label.
        aset = authority.LiteralAuthorizationSet([self._verified_literal()],
                                                 **RANGE)
        assert aset.authority_class == authority.UNVERIFIED_EXTERNAL_CLAIM
        assert aset.confers_real_call_authority is False

    def test_a_hand_labelled_verified_record_is_still_refused_for_a_real_call(
            self):
        # MC4-R05: the real-call gate checks the class AND exact scope. A
        # hand-written label passes neither the trust boundary nor, if it
        # somehow did, an exact-scope requirement it does not satisfy.
        record = self._verified_literal()
        record["occurrence_index"] = None
        record["authorization_sha256"] = (
            authority.literal_authorization_digest(record))
        with pytest.raises(BlockingError) as e:
            authority.assert_usable_for_real_call(record)
        assert "real_call_scope_not_exact" in str(e.value)

    def test_a_verified_labelled_pin_record_is_refused(self):
        record = {
            "schema_version": 2,
            "pins": good_pins(),
            "money_unit": "integer micro-USD",
            "authority_class": authority.VERIFIED_OPERATOR_PIN_AUTHORIZATION,
            "executable_authority": True,
        }
        record["pin_record_sha256"] = pinsmod.pin_digest(record)
        with pytest.raises(BlockingError) as e:
            pinsmod.validate_pin_authority(record, MODELS)
        assert e.value.code == UNSET_POLICY_PIN
        assert "candidate_side" in str(e.value)

    def test_a_custom_verifier_object_still_cannot_make_evidence_executable(
            self):
        # A candidate can define a verifier that says yes. Running it does not
        # make it a trust boundary — the class is UNVERIFIED regardless.
        class YesVerifier(authority.TrustedVerifier):
            def verify_count_evidence(self, record):
                return {**record, "evidence_class": "TRUSTED_COUNT_EVIDENCE"}

        from verifier import evidence
        mock = evidence.candidate_evidence_record(
            evidence.MOCK_TEST_EVIDENCE, counts=[], logical_request_count=0,
            provider_attempt_count=0, endpoint=counting.COUNT_PATH,
            billing_state="UNKNOWN")
        # The mock's own class is not trusted, so is_executable_authority is
        # False even before the verifier is consulted.
        assert evidence.is_executable_authority(
            mock, verifier=YesVerifier()) is False


# ---------------------- A2-F03: generations are immutable -------------------


class TestImmutableGenerations:
    def test_the_request_list_is_not_a_public_mutable_attribute(self):
        gen = finalize.RequestGeneration("solo-0")
        assert not hasattr(gen, "requests")

    def test_sealing_freezes_the_set(self):
        gen = finalize.RequestGeneration("solo-0")
        gen.add(_request(), label="u",
                units=[{"unit_sha256": "a" * 64, "atom_ids": ["b" * 64]}])
        sha = gen.seal()
        assert isinstance(sha, str) and len(sha) == 64
        # A second seal is idempotent and returns the same digest.
        assert gen.seal() == sha

    def test_the_snapshot_digest_detects_tampering(self):
        gen = finalize.RequestGeneration("solo-0")
        gen.add(_request(), label="u",
                units=[{"unit_sha256": "a" * 64, "atom_ids": ["b" * 64]}])
        gen.seal()
        # Swap the frozen snapshot out from under it.
        gen._sealed_snapshot = ()
        with pytest.raises(BlockingError) as e:
            gen.sealed_snapshot()
        assert "generation_digest_changed" in str(e.value)


# ---------------------- A2-F04: candidate mock-only transport ----------------


class _ProviderClaimingTransport:
    source = counting.SOURCE_PROVIDER

    def post(self, path, body, *, timeout=None):
        return 200, b'{"object":"response.input_tokens","input_tokens":1}'


class _SocketTransport:
    source = counting.SOURCE_MOCK

    def post(self, path, body, *, timeout=None):
        return 200, b'{"object":"response.input_tokens","input_tokens":1}'

    def connect(self, *a, **k):        # a real network method
        raise AssertionError("should never be reached")


class TestMockOnlyTransport:
    def _skeleton_finalize(self, transport):
        # Reach the guard without a real skeleton: the guard runs first.
        finalize._assert_mock_transport(transport)

    def test_a_provider_claiming_transport_is_refused(self):
        with pytest.raises(BlockingError) as e:
            self._skeleton_finalize(_ProviderClaimingTransport())
        assert "requires_mock_transport" in str(e.value)

    def test_a_transport_with_a_network_method_is_refused(self):
        with pytest.raises(BlockingError) as e:
            self._skeleton_finalize(_SocketTransport())
        assert "network_method" in str(e.value)

    def test_the_mock_transport_is_accepted(self):
        finalize._assert_mock_transport(counting.MockCountTransport())


# ---------------------- A2-F08: the governed required approver ---------------


class TestRequiredApprover:
    def test_the_governed_approver_is_sol(self):
        assert reviewpolicy.GOVERNED_REQUIRED_APPROVER == "gpt-5.6-sol"

    def test_the_policy_default_is_sol_not_the_first_model(self):
        record = reviewpolicy.policy_record(
            MODELS, required_approver=reviewpolicy.GOVERNED_REQUIRED_APPROVER,
            minimum_other_approvers=1, max_output_tokens=8_000)
        assert record["required_approver"] == "gpt-5.6-sol"
        assert record["required_approver"] != MODELS[0]

    def test_corroborator_count_is_unambiguous(self):
        record = reviewpolicy.policy_record(
            MODELS, required_approver="gpt-5.6-sol",
            minimum_other_approvers=1, max_output_tokens=8_000)
        assert record["minimum_other_approvers"] == 1
        assert record["minimum_total_approvals"] == 2

    def test_too_many_other_approvers_is_refused(self):
        with pytest.raises(BlockingError):
            reviewpolicy.policy_record(
                MODELS, required_approver="gpt-5.6-sol",
                minimum_other_approvers=3, max_output_tokens=8_000)


# ---------------------- A2-F09: the verdict schema is bounded ----------------


class TestVerdictSchema:
    def _policy(self):
        return reviewpolicy.policy_record(
            MODELS, required_approver="gpt-5.6-sol",
            minimum_other_approvers=1, max_output_tokens=8_000)

    def _good(self, challenge="CH"):
        return {"challenge": challenge, "verdicts_by_unit": {
            "a" * 64: {"refuted": False, "confidence": "high",
                       "reason": challenge + " the change is a safe rename "
                                             "with no behaviour change",
                       "proof_of_check": challenge + " inspected both sides",
                       "checked_categories": ["logic", "state"]}}}

    def test_schema_bounds_are_present(self):
        schema = providerreq.verdict_schema(["a" * 64], challenge="CH")
        verdict = schema["schema"]["properties"]["verdicts_by_unit"][
            "properties"]["a" * 64]["properties"]
        assert verdict["reason"]["minLength"] == reviewpolicy.REASON_MIN_CHARS
        assert verdict["proof_of_check"]["minLength"] == (
            reviewpolicy.PROOF_MIN_CHARS)
        # `uniqueItems` used to be asserted here. It is gone from the wire
        # schema because the provider rejects it — a live operator differential
        # returned 400 invalid_json_schema for the exact payload and 200 with
        # only that keyword removed, on all three governed models. Uniqueness
        # is enforced after parsing instead; see
        # tests/test_verifier_schema_uniqueitems.py.
        assert "uniqueItems" not in verdict["checked_categories"]
        assert verdict["checked_categories"]["minItems"] == 1

    def test_a_good_response_validates(self):
        verdicts.validate_verdicts(
            self._good(), unit_hashes=["a" * 64], challenge="CH",
            review_policy=self._policy())

    def test_an_empty_reason_blocks(self):
        bad = self._good()
        bad["verdicts_by_unit"]["a" * 64]["reason"] = "CH short"
        with pytest.raises(BlockingError):
            verdicts.validate_verdicts(bad, unit_hashes=["a" * 64],
                                       challenge="CH",
                                       review_policy=self._policy())

    def test_a_proof_that_does_not_echo_the_challenge_blocks(self):
        bad = self._good()
        bad["verdicts_by_unit"]["a" * 64]["proof_of_check"] = (
            "WRONG did the check anyway")
        with pytest.raises(BlockingError) as e:
            verdicts.validate_verdicts(bad, unit_hashes=["a" * 64],
                                       challenge="CH",
                                       review_policy=self._policy())
        assert "challenge" in str(e.value)

    def test_a_missing_unit_blocks(self):
        with pytest.raises(BlockingError):
            verdicts.validate_verdicts(
                self._good(), unit_hashes=["a" * 64, "z" * 64],
                challenge="CH", review_policy=self._policy())

    def test_a_duplicate_json_key_blocks(self):
        body = (b'{"challenge":"CH","challenge":"CH",'
                b'"verdicts_by_unit":{}}')
        with pytest.raises(BlockingError) as e:
            verdicts.parse_strict(body)
        assert e.value.code == PROVIDER_RESPONSE_INVALID


# ---------------------- A2-F26: strict count response -----------------------


class TestStrictCountResponse:
    def _run(self, body):
        class T:
            source = counting.SOURCE_MOCK

            def post(self, path, b, *, timeout=None):
                return 200, body
        return counting.count_input_tokens(_request(), transport=T(),
                                           max_retries=0, timeout_seconds=30)

    def test_an_unknown_field_blocks(self):
        with pytest.raises(BlockingError) as e:
            self._run(b'{"object":"response.input_tokens",'
                      b'"input_tokens":1,"extra":9}')
        assert e.value.code == TOKEN_COUNT_RESPONSE_INVALID

    def test_a_duplicate_key_blocks(self):
        with pytest.raises(BlockingError):
            self._run(b'{"object":"response.input_tokens",'
                      b'"input_tokens":1,"input_tokens":2}')

    def test_an_implausible_count_blocks(self):
        with pytest.raises(BlockingError) as e:
            self._run(b'{"object":"response.input_tokens",'
                      b'"input_tokens":9999999999}')
        assert "implausible" in str(e.value)


# ---------------------- A2-F07: the shipped CLI is not stale -----------------


class TestShippedCLI:
    def _have(self, sha):
        return subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=ROOT,
            capture_output=True).returncode == 0

    def test_finalize_mock_dispatch_runs_end_to_end(self, tmp_path):
        base = "75a093de45f73169072837c7c062fab421caaf8b"  # pragma: allowlist secret
        head = "b08844a0755710035d62830faa84902d9d85d3fe"  # pragma: allowlist secret
        if not (self._have(base) and self._have(head)):
            pytest.skip("PR25 objects absent")
        # A hermetic clone (the working tree carries diff.noprefix), but the
        # SHIPPED script is the working one — so the dispatch under test is
        # the current code, not the clone's committed copy.
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", "--no-local", str(ROOT),
                        str(clone)], check=True, capture_output=True)
        script = str(ROOT / "scripts" / "independent_verify.py")
        env = _cli_env()

        # Build the skeleton in-process (the API path) so the CLI test focuses
        # on the --finalize-mock dispatch.
        from verifier import artifact, plan
        skeleton = plan.build_skeleton(base, head, cwd=clone)
        skel = tmp_path / "skel.json"
        artifact.write_atomic(skeleton, str(skel))

        out = tmp_path / "report.json"
        r = subprocess.run(
            [sys.executable, script, "--finalize-mock",
             "--skeleton", str(skel), "--output", str(out)],
            cwd=clone, env=env, capture_output=True, text=True)
        # Without authorizations the PR#25 fixtures block preflight — a
        # TYPED exit 2, not a traceback.
        assert r.returncode == 2, r.stdout + r.stderr
        assert "FINALIZE-MOCK BLOCKED" in r.stderr
        assert "Traceback" not in r.stderr

    def test_a_missing_skeleton_file_is_a_typed_error(self, tmp_path):
        env = _cli_env()
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "independent_verify.py"),
             "--finalize-mock", "--skeleton", str(tmp_path / "nope.json"),
             "--output", str(tmp_path / "out.json")], cwd=ROOT, env=env,
            capture_output=True, text=True)
        assert r.returncode == 2
        assert "cannot_read_skeleton" in r.stderr
        assert "Traceback" not in r.stderr


def _cli_env():
    import os
    env = dict(os.environ)
    env.update({
        "VERIFIER_MAX_OUTPUT_TOKENS": "8000",
        "VERIFIER_CONTEXT_MARGIN_TOKENS": "4000",
        "VERIFIER_COST_CAP_MICRO_USD": "50000000",
        "VERIFIER_REASONING_EFFORT_BY_MODEL":
            "gpt-5.3-codex=medium,gpt-5.6-sol=medium,gpt-4.1-mini=",
        "VERIFIER_MAX_REVIEW_UNITS": "5000",
        "VERIFIER_MAX_GENERATION_CALLS": "5000",
        "VERIFIER_MAX_COUNT_CALLS": "100000",
        "VERIFIER_COUNT_TIMEOUT_SECONDS": "30",
        "VERIFIER_COUNT_MAX_RETRIES": "2",
        "VERIFIER_GENERATION_TIMEOUT_SECONDS": "120",
        "VERIFIER_GENERATION_MAX_RETRIES": "1",
        "VERIFIER_TOKEN_DRIFT_TOLERANCE": "64",
    })
    return env
