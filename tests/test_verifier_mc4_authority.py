"""MC4-A2 — the findings external review REOPENED, and why they stay closed.

Each of these was claimed closed in the MC4 interim report and was not. The
tests are written as the attack, so a regression restores the attack rather
than merely reddening an assertion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_verifier_finalize import _request, good_pins  # noqa: E402
from verifier import (  # noqa: E402
    authority,
    counting,
    counting2,
    evidence,
    finalize,
    origin,
    preflight,
    providerreq,
    unitpayload,
)
from verifier.errors import (  # noqa: E402
    CHUNK_COUNT_EXHAUSTED,
    TOKEN_COUNT_RESPONSE_INVALID,
    BlockingError,
)

# A perfectly well-formed anchor. Every field is the right type, the kind is
# real, the digest is 64 lowercase hex. It refers to nothing.
SHAPED_ANCHOR = {
    "anchor_kind": "TRUSTED_WORKFLOW_RUN",
    "anchor_reference": "mglaeser/bubble-regime-monitor/actions/runs/999",
    "anchor_digest": "a" * 64,
}

SECRET = "sk-proj-abcdef1234567890abcdef"          # pragma: allowlist secret
RANGE = dict(repository_identity="mglaeser/bubble-regime-monitor",
             target_base_sha="b" * 40, diff_base_sha="c" * 40,
             head_sha="d" * 40)


def _claim(atom_id, occurrence_index=0, **over):
    kwargs = dict(path_bytes_b64="cA==", atom_id=atom_id,
                  occurrence_index=occurrence_index, literal=SECRET,
                  literal_category="openai_key", reason="reviewed",
                  reviewer_identity="op", authorized_at="t",
                  authorization_source="s", test_fixture=True, **RANGE)
    kwargs.update(over)
    return authority.literal_claim(**kwargs)


# ------------------------------------ F03/F04/F06: trust is not a shape ------


class TestShapedAnchorConfersNothing:
    def test_a_perfectly_shaped_anchor_yields_only_a_claim(self):
        record = _claim("a" * 64, test_fixture=False,
                        external_anchor=SHAPED_ANCHOR)
        assert record["authority_class"] == authority.UNVERIFIED_EXTERNAL_CLAIM
        assert record["anchor_status"]["verified"] is False
        assert record["anchor_status"]["verification_status"] == (
            "SHAPE_ONLY_NOT_AUTHENTICATED")

    def test_the_constructor_has_no_way_to_ask_for_a_verified_class(self):
        # There is no authority_class parameter, so "just pass the class you
        # want" is not an available move.
        import inspect
        params = inspect.signature(authority.literal_claim).parameters
        assert "authority_class" not in params

    def test_the_default_verifier_refuses_every_promotion(self):
        record = _claim("a" * 64, test_fixture=False,
                        external_anchor=SHAPED_ANCHOR)
        with pytest.raises(BlockingError) as e:
            authority.promote_literal_authorizations([record])
        assert "trust_promotion_refused" in str(e.value)

    def test_a_hand_built_trusted_envelope_is_not_executable_authority(self):
        # ATTACK: skip the constructors entirely. Build the dictionary by
        # hand with every envelope field present and a shaped anchor, then
        # recompute the digest so the record is internally consistent.
        forged = {
            "schema_version": 1,
            "evidence_class": evidence.TRUSTED_COUNT_EVIDENCE,
            "trusted_service_identity": "trusted-lane",
            "trusted_engine_digest": "e" * 64,
            "repository_identity": RANGE["repository_identity"],
            "target_base_sha": RANGE["target_base_sha"],
            "diff_base_sha": RANGE["diff_base_sha"],
            "head_sha": RANGE["head_sha"],
            "review_skeleton_sha256": "f" * 64,
            "capability_policy_sha256": "0" * 64,
            "pin_record_sha256": "1" * 64,
            "reviewed_literal_authorization_set_sha256": "2" * 64,
            "review_request_policy_sha256": "3" * 64,
            "counts": [], "logical_request_count": 0,
            "provider_attempt_count": 0,
            "endpoint": counting.COUNT_PATH, "billing_state": "UNKNOWN",
            "produced_at": "t", "external_anchor": SHAPED_ANCHOR,
            "executable_authority": False, "transport_ids_persisted": False,
        }
        forged["evidence_sha256"] = evidence.evidence_digest(forged)
        assert evidence.is_executable_authority(forged) is False

    def test_candidate_code_cannot_construct_trusted_evidence(self):
        with pytest.raises(BlockingError):
            evidence.candidate_evidence_record(
                evidence.TRUSTED_COUNT_EVIDENCE, counts=[],
                logical_request_count=0, provider_attempt_count=0,
                endpoint=counting.COUNT_PATH, billing_state="UNKNOWN")

    def test_a_fixture_set_never_confers_real_call_authority(self):
        aset = authority.LiteralAuthorizationSet([_claim("a" * 64)], **RANGE)
        assert aset.authority_class == authority.TEST_FIXTURE_UNAUTHORIZED
        assert aset.confers_real_call_authority is False


# ------------------------ F03/F14: clearance is occurrence-scoped ------------


class TestOccurrenceScopedClearance:
    """Clearance is resolved by SPAN inside the real assembled request.

    These drive `providerreq.build_request` rather than a hand-written
    stand-in string, because the thing that has to hold is a property of the
    transmitted bytes. A stand-in also cannot exhibit the defect that made
    this rewrite necessary: JSON escaping shifts a literal's extent, so
    comparing literal hashes between the per-atom scan and the assembled body
    fails for any source line containing a quote (A2-F02)."""

    ATOM_A, ATOM_B = "a" * 64, "b" * 64
    UNIT_A, UNIT_B = "u" * 64, "v" * 64

    def _atom_map(self):
        # Atom A's literal sits inside a quoted string, so its transmitted
        # bytes carry backslashes the reviewed bytes do not.
        return {self.ATOM_A: f'key = "{SECRET}"',
                self.ATOM_B: f'other = "{SECRET}"'}

    def _atom_records(self):
        return {
            self.ATOM_A: {"atom_id": self.ATOM_A, "side": "new",
                          "line_number": 1, "hunk_id": "h",
                          "path_bytes_b64": "cA=="},
            self.ATOM_B: {"atom_id": self.ATOM_B, "side": "new",
                          "line_number": 2, "hunk_id": "h",
                          "path_bytes_b64": "cA=="},
        }

    def _manifest(self, records):
        aset = authority.LiteralAuthorizationSet(records, **RANGE)
        atom_map, atom_records = self._atom_map(), self._atom_records()
        manifest = finalize.PreflightGenerationManifest(
            aset, atom_records=atom_records, atom_map=atom_map)
        return manifest, atom_map, atom_records

    def _unit(self, unit_hash, atom_id):
        return {"unit_sha256": unit_hash, "atom_ids": [atom_id],
                "git_status": "M", "path_bytes_b64": "cA=="}

    def _scan(self, manifest, units, *, lens="review this"):
        atom_map, atom_records = self._atom_map(), self._atom_records()
        for unit in units:
            manifest._units_by_hash[unit["unit_sha256"]] = unit
        payloads = [unitpayload.structured_unit(u, atom_records, atom_map)
                    for u in units]
        request = providerreq.build_request(
            "gpt-5.3-codex", payloads, lens=lens, challenge="CH-TEST",
            reasoning_effort="medium", max_output_tokens=8_000)
        cleared, authorized = manifest._cleared_atoms(
            [u["unit_sha256"] for u in units])
        return manifest._scan_payload(
            request.transmitted_text(), label="batch:0:execution",
            unit_count=len(units), request=request, cleared_atoms=cleared,
            authorized_occurrences=authorized)

    def test_an_authorized_atom_alone_is_cleared(self):
        manifest, _, _ = self._manifest([_claim(self.ATOM_A)])
        entry = self._scan(manifest, [self._unit(self.UNIT_A, self.ATOM_A)])
        # The literal WAS found in the transmitted bytes and WAS attributed to
        # the reviewed atom — not merely absent from the scan.
        assert entry["finding_count"] >= 1
        assert entry["attributed_finding_count"] == entry["finding_count"]
        assert entry["atom_span_count"] == 1

    def test_an_escaped_literal_is_still_matched_to_its_reviewed_atom(self):
        # The regression that forced span resolution: the reviewed bytes are
        # `key = "sk-proj-…"`, the transmitted bytes are `key = \"sk-proj-…\"`.
        # A hash-of-literal comparison sees two different literals and blocks a
        # correctly authorized atom.
        manifest, atom_map, _ = self._manifest([_claim(self.ATOM_A)])
        assert '"' in atom_map[self.ATOM_A]
        entry = self._scan(manifest, [self._unit(self.UNIT_A, self.ATOM_A)])
        assert entry["attributed_finding_count"] >= 1

    def test_the_same_literal_in_an_unauthorized_atom_still_blocks(self):
        # ATTACK: atom A is reviewed. Atom B carries the same bytes and is
        # not. Pack both into one batch. MC4's first attempt reduced the
        # authorizations to a set of literal hashes for the whole request,
        # so B's occurrence was cleared by A's review.
        manifest, _, _ = self._manifest([_claim(self.ATOM_A)])
        with pytest.raises(BlockingError) as e:
            self._scan(manifest, [self._unit(self.UNIT_A, self.ATOM_A),
                                  self._unit(self.UNIT_B, self.ATOM_B)])
        assert "unauthorized_atom_count" in str(e.value)

    def test_a_literal_injected_into_scaffolding_is_never_cleared(self):
        # A source-atom authorization must not clear the instructions. The
        # secret is injected through the model's LENS, which is real
        # scaffolding assembled into every request.
        manifest, _, _ = self._manifest([_claim(self.ATOM_A)])
        with pytest.raises(BlockingError) as e:
            self._scan(manifest, [self._unit(self.UNIT_A, self.ATOM_A)],
                       lens=f"review this {SECRET} carefully")
        assert "secret_outside_reviewed_content" in str(e.value)

    def test_an_atom_the_map_cannot_locate_blocks_rather_than_passing(self):
        # If the span map and the assembled bytes disagree, every finding
        # would silently become unattributable — or, worse, a region would go
        # unmapped. Fail closed instead.
        manifest, _, atom_records = self._manifest([_claim(self.ATOM_A)])
        unit = self._unit(self.UNIT_A, self.ATOM_A)
        payload = unitpayload.structured_unit(unit, atom_records,
                                              self._atom_map())
        with pytest.raises(BlockingError) as e:
            origin.build_for_payload("execution", "unrelated payload text",
                                     [payload], atom_records)
        assert "origin_span_unlocatable" in str(e.value)

    def test_one_pattern_hit_is_one_occurrence(self):
        # "sk-proj-…" matches openai_key AND openai_project_key. Counting
        # them as two occurrences made an authorization for occurrence 0 fail
        # to cover a literal reviewed exactly once.
        findings = preflight.scan_text(f"key = {SECRET}")
        assert len(findings) > 1
        assert len(preflight.distinct_occurrences(findings)) == 1


# ---------------------- F12: preflight precedes every transmission -----------


class _SecretOnLastRequest(counting.MockCountTransport):
    """Counts happily. The point is whether it is ever reached."""


class TestGenerationPreflight:
    def test_counting_an_unsealed_generation_is_refused(self):
        manifest = finalize.PreflightGenerationManifest(
            None, atom_records={}, atom_map={})
        generation = finalize.RequestGeneration("solo-0")
        generation.add(_request(), label="u", units=[])
        ledger = counting2.CountLedger(counting.MockCountTransport(),
                                       good_pins())
        with pytest.raises(BlockingError) as e:
            manifest.count_generation(generation, ledger)
        assert "snapshot_before_seal" in str(e.value)
        assert ledger.provider_attempts == 0

    def test_a_request_added_after_seal_is_refused(self):
        manifest = finalize.PreflightGenerationManifest(
            None, atom_records={}, atom_map={})
        generation = finalize.RequestGeneration("solo-0")
        ledger = counting2.CountLedger(counting.MockCountTransport(),
                                       good_pins())
        manifest.seal(generation, ledger)
        with pytest.raises(BlockingError) as e:
            generation.add(_request(), label="late", units=[])
        assert "request_added_after_seal" in str(e.value)


# ------------------- F15: attempts are counted before they happen ------------


class _AlwaysRetryable(counting.MockCountTransport):
    def post(self, path, body, *, timeout=None):
        self.calls += 1
        return 503, b""


class _NoTimeoutParameter:
    source = counting.SOURCE_MOCK

    def post(self, path, body):
        return 200, b'{"object":"response.input_tokens","input_tokens":1}'


class TestAttemptAccounting:
    def test_failed_retries_are_counted(self):
        # MC4's first attempt added attempts only from a successful result,
        # so a request that burned three calls and failed reported zero.
        ledger = counting2.CountLedger(_AlwaysRetryable(),
                                       good_pins(VERIFIER_COUNT_MAX_RETRIES=2))
        with pytest.raises(BlockingError):
            ledger.count(_request(), label="u")
        assert ledger.provider_attempts == 3
        record = ledger.record()
        assert record["failed_attempt_count"] == 3
        assert all(a["result_category"].startswith("failed")
                   for a in record["attempts"])

    def test_no_provider_or_exception_text_reaches_the_ledger(self):
        ledger = counting2.CountLedger(_AlwaysRetryable(),
                                       good_pins(VERIFIER_COUNT_MAX_RETRIES=1))
        with pytest.raises(BlockingError):
            ledger.count(_request(), label="u")
        import json
        blob = json.dumps(ledger.record())
        assert "503" not in blob
        assert "Traceback" not in blob

    def test_a_transport_without_the_timeout_protocol_is_refused(self):
        # MC4's first attempt caught TypeError and retried WITHOUT a timeout,
        # silently discarding the operator's bound.
        with pytest.raises(BlockingError) as e:
            counting.count_input_tokens(_request(),
                                        transport=_NoTimeoutParameter(),
                                        max_retries=0, timeout_seconds=30)
        assert e.value.code == TOKEN_COUNT_RESPONSE_INVALID
        assert "timeout_protocol" in str(e.value)

    def test_the_operator_timeout_reaches_the_transport(self):
        transport = counting.MockCountTransport()
        counting.count_input_tokens(_request(), transport=transport,
                                    max_retries=0, timeout_seconds=17)
        assert transport.last_timeout == 17

    def test_the_cap_bounds_attempts_not_logical_requests(self):
        ledger = counting2.CountLedger(
            counting.MockCountTransport(),
            good_pins(VERIFIER_MAX_COUNT_CALLS=2,
                      VERIFIER_COUNT_MAX_RETRIES=2))
        with pytest.raises(BlockingError) as e:
            ledger.count(_request(), label="u")
        assert e.value.code == CHUNK_COUNT_EXHAUSTED
        assert ledger.provider_attempts == 0
