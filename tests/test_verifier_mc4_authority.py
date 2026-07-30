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


#: The categories the scanner ACTUALLY assigns to SECRET. Derived rather than
#: guessed: MC4-R02 makes the clearance match a category SET, so a fixture
#: that names one category no longer covers a literal detected as two.
DETECTED_CATEGORIES = sorted({
    f["category"] for f in preflight.scan_text(SECRET)})


def _claim(atom_id, occurrence_index=0, **over):
    kwargs = dict(path_bytes_b64="cA==", atom_id=atom_id,
                  occurrence_index=occurrence_index, literal=SECRET,
                  literal_categories=DETECTED_CATEGORIES, reason="reviewed",
                  reviewer_identity="op", authorized_at="t",
                  authorization_source="s", test_fixture=True, **RANGE)
    kwargs.update(over)
    return authority.literal_claim(**kwargs)


def _span(start, end, kind, *, source_text=None, **fields):
    """A Span with its source digest computed, for tests.

    MC4-R03 requires a content span to bind sha256(source_text). Computing it
    here keeps every test honest about the invariant without making each one
    restate the hash."""
    from verifier.canon import sha256_hex
    if source_text is not None:
        fields.setdefault("source_content_sha256", sha256_hex(
            source_text.encode("utf-8", "surrogateescape")))
    return origin.Span(start, end, kind,
                       source_text=source_text, **fields)


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
    """Clearance is EXACT-OCCURRENCE, resolved inside the real assembled body.

    These drive `providerreq.assemble_request`, because every property here
    is a property of the transmitted bytes. A hand-written stand-in also
    cannot exhibit the two defects that forced this design: JSON escaping
    shifts a literal's extent (A2-F02), and clearing a whole atom accepts
    transmitted findings that no source occurrence corresponds to (C4-F01).
    """

    ATOM_A, ATOM_B = "a" * 64, "b" * 64
    UNIT_A, UNIT_B = "u" * 64, "v" * 64
    PATH_B64 = "cA=="

    ATOM_TEXTS = {
        # Atom A's literal sits inside a quoted string, so its transmitted
        # bytes carry backslashes the reviewed bytes do not.
        ATOM_A: f'key = "{SECRET}"',
        ATOM_B: f'other = "{SECRET}"',
    }

    def _atom_map(self, **over):
        return {**self.ATOM_TEXTS, **over}

    def _atom_records(self):
        return {
            self.ATOM_A: {"atom_id": self.ATOM_A, "side": "new",
                          "line_number": 1, "hunk_id": "h",
                          "path_bytes_b64": self.PATH_B64},
            self.ATOM_B: {"atom_id": self.ATOM_B, "side": "new",
                          "line_number": 2, "hunk_id": "h",
                          "path_bytes_b64": self.PATH_B64},
        }

    def _manifest(self, records, atom_map=None):
        aset = authority.LiteralAuthorizationSet(records, **RANGE)
        return finalize.PreflightGenerationManifest(
            aset, atom_records=self._atom_records(),
            atom_map=atom_map or self._atom_map())

    def _unit(self, unit_hash, atom_id):
        return {"unit_sha256": unit_hash, "atom_ids": [atom_id],
                "git_status": "M", "path_bytes_b64": self.PATH_B64}

    def _scan(self, manifest, units, *, lens="review this"):
        atom_records = self._atom_records()
        for unit in units:
            manifest._units_by_hash[unit["unit_sha256"]] = unit
        payloads = [unitpayload.structured_unit(u, atom_records,
                                                manifest.atom_map)
                    for u in units]
        assembly = providerreq.assemble_request(
            "gpt-5.3-codex", payloads, lens=lens, challenge="CH-TEST",
            reasoning_effort="medium", max_output_tokens=8_000,
            path_bytes_b64_by_unit=manifest.path_bytes_b64_by_unit(units))
        return manifest._scan_payload(
            assembly, payload_kind="execution", label="batch:0:execution",
            unit_count=len(units))

    def test_an_authorized_occurrence_is_cleared(self):
        manifest = self._manifest([_claim(self.ATOM_A)])
        entry = self._scan(manifest, [self._unit(self.UNIT_A, self.ATOM_A)])
        # The literal WAS found in the transmitted bytes and WAS resolved to
        # the reviewed occurrence — not merely absent from the scan.
        assert entry["finding_count"] >= 1
        assert entry["cleared_occurrence_count"] == entry["finding_count"]

    def test_an_escaped_literal_is_still_matched_to_its_source_occurrence(self):
        # The regression that forced span resolution: the reviewed bytes are
        # `key = "sk-proj-…"`, the transmitted bytes are `key = \"sk-proj-…\"`.
        # A hash-of-literal comparison sees two different literals and blocks
        # a correctly authorized occurrence.
        manifest = self._manifest([_claim(self.ATOM_A)])
        assert '"' in manifest.atom_map[self.ATOM_A]
        entry = self._scan(manifest, [self._unit(self.UNIT_A, self.ATOM_A)])
        assert entry["cleared_occurrence_count"] >= 1

    def test_the_same_literal_in_an_unauthorized_atom_still_blocks(self):
        # ATTACK: atom A is reviewed. Atom B carries the same bytes and is
        # not. Pack both into one batch. MC4's first attempt reduced the
        # authorizations to a set of literal hashes for the whole request,
        # so B's occurrence was cleared by A's review.
        manifest = self._manifest([_claim(self.ATOM_A)])
        with pytest.raises(BlockingError) as e:
            self._scan(manifest, [self._unit(self.UNIT_A, self.ATOM_A),
                                  self._unit(self.UNIT_B, self.ATOM_B)])
        assert origin.UNAUTHORIZED_OCCURRENCE in str(e.value)

    def test_a_literal_injected_into_scaffolding_is_never_cleared(self):
        # A source-atom authorization must not clear the instructions. The
        # secret is injected through the model's LENS, which is real
        # scaffolding assembled into every request.
        manifest = self._manifest([_claim(self.ATOM_A)])
        with pytest.raises(BlockingError) as e:
            self._scan(manifest, [self._unit(self.UNIT_A, self.ATOM_A)],
                       lens=f"review this {SECRET} carefully")
        assert origin.IN_SCAFFOLDING in str(e.value)

    def test_a_transmitted_finding_with_no_source_occurrence_is_refused(self):
        # C4-F01, driven at the resolution rule itself: the previous design
        # put an atom with zero raw findings straight into `cleared_atoms`,
        # so anything appearing inside it in the serialized form was
        # accepted. Here the span's source has no finding at the mapped
        # range, and the resolver refuses rather than defaulting to the
        # atom's status.
        origin_map = origin.OriginMap("execution")
        origin_map.add(_span(
            0, 20, origin.ATOM_CONTENT, unit_sha256=self.UNIT_A,
            atom_id=self.ATOM_A, path_bytes_b64=self.PATH_B64,
            source_text="a harmless changed line"))
        resolution = origin.resolve_finding(
            {"offset": 2, "length": 8, "value_sha256": "f" * 64,
             "category": "high_entropy_token"},
            origin_map=origin_map,
            authorizations=authority.LiteralAuthorizationSet(
                [_claim(self.ATOM_A)], **RANGE),
            source_findings=lambda span: [])
        assert resolution["cleared"] is False
        assert resolution["refusal"] == origin.NO_SOURCE_OCCURRENCE

    def test_a_finding_starting_inside_an_escape_has_no_source_preimage(self):
        # The serializer turns one source character into two transmitted
        # ones. A match beginning at the backslash describes bytes the
        # repository does not contain, so there is no occurrence anyone could
        # have reviewed.
        raw = 'a"bc'
        assert origin.json_escaped(raw) == 'a\\"bc'
        # offset 1 is the backslash: a real raw boundary, so it maps.
        assert origin.raw_preimage(raw, 1, 3) == (1, 2)
        # offset 2 is the quote INSIDE the escape: no raw boundary.
        assert origin.raw_preimage(raw, 2, 3) is None

    def test_escaping_cannot_manufacture_a_token_finding(self):
        # Why the rule above is defence in depth rather than a live path with
        # today's patterns: every escape sequence begins with a backslash,
        # which is in none of the scanner's alphabets, so serialization can
        # shorten or displace a match but never join two runs into one.
        joined = "A" * 20 + "\n" + "B" * 20
        assert not preflight.scan_text(joined)
        assert not preflight.scan_text(origin.json_escaped(joined))
        assert "\\" in origin.json_escaped(joined)

    def test_a_different_transmitted_literal_in_a_reviewed_atom_blocks(self):
        # Authorize occurrence 0; the atom carries a SECOND, different
        # literal. Clearing the atom wholly would have accepted both.
        second = "ghp_" + "z" * 36
        manifest = self._manifest(
            [_claim(self.ATOM_A)],
            atom_map=self._atom_map(**{
                self.ATOM_A: f'key = "{SECRET}" spare = "{second}"'}))
        with pytest.raises(BlockingError) as e:
            self._scan(manifest, [self._unit(self.UNIT_A, self.ATOM_A)])
        assert origin.UNAUTHORIZED_OCCURRENCE in str(e.value)

    def test_a_category_mismatch_blocks(self):
        # Same bytes, same atom, same occurrence — reviewed under a category
        # set that is not what the scanner detected. An operator cleared a
        # different statement about those bytes.
        manifest = self._manifest(
            [_claim(self.ATOM_A,
                    literal_categories=["not_the_detected_category"])])
        with pytest.raises(BlockingError) as e:
            self._scan(manifest, [self._unit(self.UNIT_A, self.ATOM_A)])
        assert origin.UNAUTHORIZED_OCCURRENCE in str(e.value)

    def test_the_verifier_line_prefix_is_not_atom_content(self):
        # C4-F02: `NEW 000001 | ` is written by this code, not by the
        # repository, so it must be scaffolding. If it were inside the atom
        # span, a literal straddling the marker and the source could borrow
        # the source's authorization.
        manifest = self._manifest([_claim(self.ATOM_A)])
        units = [self._unit(self.UNIT_A, self.ATOM_A)]
        payloads = [unitpayload.structured_unit(u, self._atom_records(),
                                                manifest.atom_map)
                    for u in units]
        assembly = providerreq.assemble_request(
            "gpt-5.3-codex", payloads, lens="review", challenge="CH-TEST",
            reasoning_effort="medium", max_output_tokens=8_000,
            path_bytes_b64_by_unit=manifest.path_bytes_b64_by_unit(units))
        spans = assembly.execution_origin_map.spans
        atom_spans = [s for s in spans if s.kind == origin.ATOM_CONTENT]
        assert len(atom_spans) == 1
        # The span holds the source bytes EXACTLY — no marker, no delimiter.
        assert atom_spans[0].source_text == manifest.atom_map[self.ATOM_A]

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
