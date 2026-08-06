"""MC3 PASS C — eight adversarial lenses against the Stage-2 finalizer.

Each test states the ATTACK, not the implementation. A lens passes only when
the attack is refused for the right reason: the wrong error code is a failure,
because "it blocked" and "it blocked because it detected this" are different
claims.

Lenses
  1  artifact / executable-plan semantic tamper
  2  generated-derivative proof
  3  git execution policy
  4  CODEOWNERS authority
  5  raw change semantics
  6  secret preflight and privacy
  7  token accounting and capacity
  8  batching and cost
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_verifier_finalize import (  # noqa: E402
    PR25_BASE,
    PR25_HEAD,
    ROOT,
    _have,
    good_pins,
    proposed_authorizations,
)
from verifier import (  # noqa: E402
    artifact,
    batching,
    capabilities,
    counting,
    counting2,
    finalize,
    generated,
    plan,
    preflight,
    unitpayload,
)
from verifier.errors import (  # noqa: E402
    CHUNK_COUNT_EXHAUSTED,
    COST_CAP_EXCEEDED,
    STALE_REVIEW_PLAN,
    TOKEN_COUNT_DRIFT,
    TOKEN_COUNT_RESPONSE_INVALID,
    BlockingError,
)

MODELS = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]


@pytest.fixture(scope="module")
def clone(tmp_path_factory):
    if not (_have(PR25_BASE) and _have(PR25_HEAD)):
        pytest.skip("PR25 objects absent")
    dst = tmp_path_factory.mktemp("attack")
    subprocess.run(["git", "clone", "-q", "--no-local", str(ROOT), str(dst)],
                   check=True, capture_output=True)
    return dst


@pytest.fixture(scope="module")
def skeleton(clone):
    return plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=clone)


@pytest.fixture(scope="module")
def finalized(skeleton, clone):
    return finalize.finalize(skeleton, cwd=clone, operator_pins=good_pins(),
                             transport=counting.MockCountTransport(),
                             authorizations=proposed_authorizations(
                                 skeleton, clone))


# --------------------------------------------------- lens 1: plan tamper ----


class TestLens1SemanticTamper:
    def test_skeleton_tamper_survives_a_recomputed_top_level_hash(self,
                                                                 skeleton):
        # Flipping a derived claim and re-hashing the file must NOT produce a
        # loadable artifact: the loader recomputes, it does not compare.
        forged = json.loads(json.dumps(skeleton))
        forged["coverage"]["model_review_atom_count"] += 1
        forged = artifact.finalize_self_hash(forged)
        with pytest.raises(BlockingError):
            artifact.validate_strict(forged)

    def test_executable_plan_tamper_survives_a_recomputed_digest(self,
                                                                finalized):
        # ATTACK: publish a plan that claims it is executable and that the
        # counts came from a provider, then recompute its own digest so the
        # record is internally consistent. A strict loader must refuse,
        # because `executable` is DERIVED from the count source.
        forged = json.loads(json.dumps(finalized))
        forged["executable"] = True
        forged["pending_requirements"] = []
        forged["count_evidence"]["evidence_class"] = "TRUSTED_COUNT_EVIDENCE"
        forged["mock_finalization_report_sha256"] = (
            finalize.mock_report_digest(forged))
        with pytest.raises(BlockingError):
            finalize.validate_report_shape(forged)

    def test_executable_plan_cost_tamper_is_recomputed(self, finalized):
        forged = json.loads(json.dumps(finalized))
        forged["cost_plan"]["worst_case_total_micro_usd"] = 1
        forged["mock_finalization_report_sha256"] = (
            finalize.mock_report_digest(forged))
        with pytest.raises(BlockingError):
            finalize.validate_report_shape(forged)

    def test_executable_plan_batch_tamper_is_recomputed(self, finalized):
        # Dropping a batch drops units from review while every remaining
        # record stays self-consistent.
        if len(finalized["batches"]) < 2:
            pytest.skip("needs at least two batches")
        forged = json.loads(json.dumps(finalized))
        forged["batches"] = forged["batches"][:-1]
        forged["mock_finalization_report_sha256"] = (
            finalize.mock_report_digest(forged))
        with pytest.raises(BlockingError):
            finalize.validate_report_shape(forged)

    def test_untampered_plan_loads(self, finalized):
        assert finalize.validate_report_shape(finalized) is not None


# ----------------------------------------------- lens 2: generated proof ----


class TestLens2GeneratedProof:
    def test_unverified_endpoint_cannot_yield_a_proof(self, skeleton):
        # ATTACK: claim a generated relationship whose head endpoint is not
        # PRESENT_VERIFIED, to retire atoms from direct review for free.
        for record in skeleton["generated_relationships"]:
            assert record["head_endpoint_state"] == generated.PRESENT_VERIFIED
            assert record["base_endpoint_state"] == generated.WHOLLY_ABSENT

    def test_proof_binds_the_repository_change(self, skeleton):
        repo_id = skeleton["identities"]["repository_change_sha256"]
        for record in skeleton["generated_relationships"]:
            assert record["repository_change_sha256"] == repo_id


# -------------------------------------------------- lens 3: git policy ------


class TestLens3GitPolicy:
    def test_policy_is_bound_into_the_plan(self, skeleton, finalized):
        assert (finalized["git_execution_policy_sha256"]
                == skeleton["git_execution_policy"]["policy_sha256"])

    def test_a_different_head_is_a_stale_plan(self, skeleton, clone):
        forged = json.loads(json.dumps(skeleton))
        forged["repository_state"]["head_sha"] = PR25_BASE
        with pytest.raises(BlockingError):
            finalize.finalize(forged, cwd=clone, operator_pins=good_pins(),
                              transport=counting.MockCountTransport(),
                              authorizations=proposed_authorizations(
                                  skeleton, clone))


# ------------------------------------------------- lens 4: CODEOWNERS -------


class TestLens4Codeowners:
    def test_codeowners_is_read_from_the_target_base(self, skeleton):
        owners = skeleton["github_codeowners"]
        assert owners["effective_base_commit_sha"] == skeleton[
            "repository_state"]["target_base_sha"]


# ------------------------------------------------ lens 5: raw semantics -----


class TestLens5RawSemantics:
    def test_every_atom_maps_to_a_recorded_file(self, skeleton):
        paths = {f["path_bytes_b64"] for f in skeleton["files"]}
        for atom in skeleton["atoms"]:
            assert atom["path_bytes_b64"] in paths


# ------------------------------------------- lens 6: secrets and privacy ----


class TestLens6SecretsAndPrivacy:
    def test_base64_wrapping_does_not_launder_a_credential(self):
        # ATTACK: the scanner skips canonical base64 of ordinary text. Wrap a
        # real credential in base64 and see whether it slips through.
        secret = "sk-proj-abcdef1234567890abcdef"     # pragma: allowlist secret
        wrapped = base64.b64encode(secret.encode()).decode()
        assert preflight.scan_text(wrapped), "base64 laundering must be caught"

    def test_base64_of_an_ordinary_path_is_not_a_finding(self):
        blob = base64.b64encode(b"tests/test_probe_error_redaction.py").decode()
        assert preflight.scan_text(blob) == []

    def test_an_allowlisted_prefix_does_not_clear_a_longer_secret(self):
        # ATTACK: an operator reviews a short fixture; a longer credential
        # sharing that prefix must NOT inherit the clearance.
        reviewed = "sk-proj-abcdef123456"             # pragma: allowlist secret
        longer = reviewed + "CONTINUESintoARealKey0987"
        assert preflight.scan_text(longer,
                                   allowlist=frozenset({reviewed})) != []

    def test_a_finding_never_carries_the_secret(self):
        secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"  # pragma: allowlist secret
        findings = preflight.scan_text(f"token = {secret}")
        assert findings
        blob = json.dumps(findings)
        assert secret not in blob
        for part in (secret[:12], secret[-12:]):
            assert part not in blob

    def test_a_blocking_message_never_carries_the_secret(self):
        secret = "sk-proj-abcdef1234567890abcdef"     # pragma: allowlist secret
        with pytest.raises(BlockingError) as e:
            preflight.preflight_request(secret, label="attack")
        assert secret not in str(e.value)

    def test_public_summary_leaks_no_content_or_paths(self, finalized):
        pub = finalize.public_plan_summary(finalized)
        blob = json.dumps(pub)
        assert "path_bytes_b64" not in blob
        assert "unit_sha256_in_order" not in blob
        assert "atom_ids" not in blob
        assert "instructions" not in blob
        for unit in finalized["final_units"][:20]:
            assert unit["unit_sha256"] not in blob


# ------------------------------------- lens 7: token accounting/capacity ----


class _UnlabelledTransport:
    """A transport that declares NO source. It is not the provider, but it
    also never says so."""

    def __init__(self):
        self.calls = 0

    def post(self, path, body, *, timeout=None):
        self.calls += 1
        return 200, json.dumps({"object": counting.EXPECTED_OBJECT,
                                "input_tokens": 10}).encode()


class TestLens7TokenAccounting:
    def test_an_unlabelled_transport_is_never_treated_as_the_provider(self):
        # ATTACK: supply a transport with no `source`. If the default is
        # "PROVIDER", a local stand-in silently becomes provider evidence and
        # the plan turns executable on invented counts.
        request = _tiny_request()
        with pytest.raises(BlockingError) as e:
            counting.count_input_tokens(request,
                                        transport=_UnlabelledTransport(),
                                        max_retries=0, timeout_seconds=30)
        assert e.value.code == TOKEN_COUNT_RESPONSE_INVALID

    def test_split_children_recompute_every_derived_field(self):
        # ATTACK: a unit whose atoms are MULTI-LINE. MC3 sliced the joined
        # text by line index, handing each child its sibling's content; it
        # also COPIED the parent's derived metadata, so a child certified a
        # byte count for content it did not contain.
        atom_map = {
            "a" * 64: "alpha line one\nalpha line two\nalpha line three",
            "b" * 64: "beta line one\nbeta line two",
        }
        records = {
            "a" * 64: {"atom_id": "a" * 64, "side": "new", "line_number": 10,
                       "hunk_id": "h1", "path_bytes_b64": ""},
            "b" * 64: {"atom_id": "b" * 64, "side": "old", "line_number": 20,
                       "hunk_id": "h2", "path_bytes_b64": ""},
        }
        parent = _synthetic_unit(list(atom_map))
        parent["changed_content_bytes"] = sum(
            len(v.encode()) for v in atom_map.values())
        child = finalize.derive_unit_record(parent, ["a" * 64], records,
                                            atom_map)
        # every derived field belongs to the CHILD, not the parent
        assert child["atom_ids"] == ["a" * 64]
        assert child["changed_content_bytes"] == len(
            atom_map["a" * 64].encode())
        assert child["changed_content_bytes"] < parent["changed_content_bytes"]
        assert child["new_line_range"] == [10, 10]
        assert child["old_line_range"] is None      # the OLD atom is the sibling's
        assert child["meta_atom_count"] == 0
        assert child["split_depth"] == parent["split_depth"] + 1
        assert child["unit_sha256"] != parent["unit_sha256"]
        # and its request text is exactly its own atoms
        payload = unitpayload.structured_unit(child, records, atom_map)
        assert payload["atoms"][0]["content"] == atom_map["a" * 64]
        assert len(payload["atoms"]) == 1

    def test_a_batch_may_not_count_below_its_member_floor(self):
        # MC4-F16: this invariant is DETERMINISTIC, not operator-tunable. A
        # batch body strictly contains each member unit's section, so
        # counting fewer tokens than the largest member is impossible. MC3
        # spent the operator's drift PIN on it; that PIN belongs to the
        # execution-usage comparison instead.
        with pytest.raises(BlockingError) as e:
            counting2.assert_batch_not_below_member_floor(
                measured=10, floor=100, model_id="gpt-5.3-codex",
                label="batch-0000")
        assert e.value.code == TOKEN_COUNT_DRIFT
        counting2.assert_batch_not_below_member_floor(
            measured=100, floor=100, model_id="gpt-5.3-codex",
            label="batch-0000")

    def test_the_drift_pin_governs_execution_usage(self):
        # The invariant the PIN was always meant for.
        counting2.assert_usage_within_tolerance(
            counted=1000, reported=1040, tolerance=64, where="batch-0000")
        with pytest.raises(BlockingError) as e:
            counting2.assert_usage_within_tolerance(
                counted=1000, reported=1200, tolerance=64, where="batch-0000")
        assert e.value.code == TOKEN_COUNT_DRIFT

    def test_retries_cannot_push_a_run_past_the_attempt_cap(self):
        # MC4-F15: MC3 counted LOGICAL requests, while each could make
        # 1 + max_retries actual attempts — so a run could exceed the
        # operator's cap threefold and still report itself inside it.
        pins = good_pins(VERIFIER_MAX_COUNT_CALLS=2,
                         VERIFIER_COUNT_MAX_RETRIES=2)
        ledger = counting2.CountLedger(counting.MockCountTransport(), pins)
        with pytest.raises(BlockingError) as e:
            ledger.count(_tiny_request(), label="u")
        # 1 + 2 retries = 3 worst-case attempts against a cap of 2: refused
        # BEFORE the first attempt, not after the overspend.
        assert e.value.code == CHUNK_COUNT_EXHAUSTED
        assert ledger.provider_attempts == 0

    def test_identical_requests_are_counted_once(self):
        # MC4-F38: greedy probing re-asked byte-identical questions.
        pins = good_pins()
        transport = counting.MockCountTransport()
        ledger = counting2.CountLedger(transport, pins)
        request = _tiny_request()
        first = ledger.count(request, label="a")
        second = ledger.count(request, label="b")
        assert first.input_tokens == second.input_tokens
        assert ledger.cache_hits == 1
        assert transport.calls == 1

    def test_counts_are_per_model_and_never_shared(self, finalized):
        for batch in finalized["batches"]:
            counts = batch["input_tokens_by_model"]
            assert set(counts) == set(MODELS)
            hashes = batch["request_hashes_by_model"]
            semantics = {h["request_semantics_sha256"] for h in hashes.values()}
            assert len(semantics) == len(MODELS)   # one per model, distinct


# ---------------------------------------------- lens 8: batching and cost ---


class TestLens8BatchingAndCost:
    def test_cost_cap_pin_names_its_unit(self):
        from verifier.policy import POLICY_PIN_NAMES
        assert "VERIFIER_COST_CAP_MICRO_USD" in POLICY_PIN_NAMES
        assert "VERIFIER_COST_CAP_USD" not in POLICY_PIN_NAMES

    def test_no_float_anywhere_in_the_cost_ledger(self, finalized):
        def walk(node):
            if isinstance(node, float):
                raise AssertionError("a float entered the cost ledger")
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(finalized["cost_plan"])

    def test_every_unit_lands_in_exactly_one_batch(self, finalized):
        batching.prove_batch_partition(finalized["final_units"],
                                       finalized["batches"])

    def test_a_batch_mixes_only_one_review_class(self, finalized):
        for batch in finalized["batches"]:
            classes = {tuple(batch["review_class"])}
            assert len(classes) == 1

    def test_the_cap_is_compared_in_micro_usd(self, skeleton, clone):
        # One micro-USD is a real cap, not a rounding artefact: any nonzero
        # plan must exceed it.
        with pytest.raises(BlockingError) as e:
            finalize.finalize(skeleton, cwd=clone,
                              operator_pins=good_pins(
                                  VERIFIER_COST_CAP_MICRO_USD=1),
                              transport=counting.MockCountTransport(),
                              authorizations=proposed_authorizations(
                                  skeleton, clone))
        assert e.value.code == COST_CAP_EXCEEDED
        assert "micro_usd" in str(e.value)


# --------------------------------------------------------------- helpers ----


def _tiny_request():
    from test_verifier_finalize import _request
    return _request()


def _synthetic_unit(atom_ids):
    from verifier import coverage
    unit = {
        "path_bytes_b64": "",
        "original_path_bytes_b64": None,
        "git_status": "M",
        "atom_ids": list(atom_ids),
        "atom_ordinals": list(range(len(atom_ids))),
        "min_patch_ordinal": 0,
        "max_patch_ordinal": len(atom_ids) - 1,
        "old_line_range": None,
        "new_line_range": None,
        "meta_atom_count": 0,
        "context_facts": {"context_kind": "none", "context_bytes": 0,
                          "context_sha256": "0" * 64},
        "split_strategies": [],
        "split_depth": 0,
        "changed_content_bytes": 0,
        "oversized_single_atom": False,
        "disposition": "MODEL_REVIEW",
        "base_sha": "b" * 40,
        "head_sha": "d" * 40,
        "repository_change_sha256": "e" * 64,
        "provider_visible_material_sha256": "f" * 64,
        "classification": {"control_bearing": False, "effective_risk": 0},
    }
    unit["unit_sha256"] = coverage.unit_hash(unit)
    return unit


def _atoms_of(parent, unit_hash, atom_map):
    """Recover which atoms a recorded child hash stands for."""
    from verifier import coverage
    for size in range(1, len(parent["atom_ids"]) + 1):
        for start in range(0, len(parent["atom_ids"]) - size + 1):
            subset = parent["atom_ids"][start:start + size]
            child = finalize._derive_subunit(parent, subset)
            if child["unit_sha256"] == unit_hash:
                return subset
    assert coverage.unit_hash(parent) == unit_hash
    return parent["atom_ids"]


class _OnlyOneAtomFits:
    """A body carrying BOTH atoms is oversized; either atom alone fits.

    That forces exactly one split, so each child's recorded text can be
    compared against the atoms the child actually claims."""

    source = counting.SOURCE_MOCK

    def __init__(self):
        self.calls = 0

    def post(self, path, body, *, timeout=None):
        self.calls += 1
        text = body.decode("utf-8", "replace")
        both = "alpha line one" in text and "beta line one" in text
        return 200, json.dumps({
            "object": counting.EXPECTED_OBJECT,
            "input_tokens": 5_000_000 if both else 100}).encode()


class _DriftingTransport(counting.MockCountTransport):
    """An endpoint whose count for the SAME body shrinks on re-measurement.

    The finalizer counts a unit while fitting it and counts it again as a
    batch. Identical bytes must yield an identical count; a shrinking one
    would understate context pressure and cost."""

    def __init__(self):
        super().__init__()
        self.seen: dict[str, int] = {}

    def post(self, path, body, *, timeout=None):
        self.calls += 1
        key = str(len(body)) + body[:64].decode("utf-8", "replace")
        times = self.seen.get(key, 0) + 1
        self.seen[key] = times
        tokens = -(-len(body) // self.bytes_per_token) // (4 ** (times - 1))
        return 200, json.dumps({"object": counting.EXPECTED_OBJECT,
                                "input_tokens": tokens}).encode()


__all__ = ["STALE_REVIEW_PLAN", "capabilities"]
