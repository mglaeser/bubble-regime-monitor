"""MC4 PASS C — the properties the mandate names, driven as tests.

Each of these is a claim the checkpoint report would otherwise make in prose.
The pattern throughout is the same: assert the property on the artefact the
production code actually produces, not on a restatement of it, and prefer an
assertion that fails when the property is quietly dropped over one that
merely passes today.
"""

from __future__ import annotations

import inspect
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
    _request,
    good_pins,
    proposed_authorizations,
)
from verifier import (  # noqa: E402
    artifact,
    authority,
    counting,
    counting2,
    evidence,
    executor,
    finalize,
    origin,
    plan,
    preflight,
    providerreq,
    reviewpolicy,
    unitpayload,
)
from verifier.errors import (  # noqa: E402
    CHUNK_COUNT_EXHAUSTED,
    SECRET_PREFLIGHT_FAILED,
    TOKEN_COUNT_RESPONSE_INVALID,
    BlockingError,
)

SECRET = "sk-proj-abcdef1234567890abcdef"          # pragma: allowlist secret
MAIN = "b08844a0755710035d62830faa84902d9d85d3fe"  # pragma: allowlist secret


# ------------------------------------------- clearance cannot be borrowed ----


class TestCrossBoundaryOccurrence:
    """A finding that is not wholly inside ONE atom is never clearable.

    The renderer makes a natural straddle hard to produce — every atom is
    prefixed with `NEW 000123 | `, and neither the token alphabet nor the
    Base64 one contains a space or a pipe, so a token match cannot cross the
    seam. That is a property of today's renderer, not of the clearance rule,
    so both are asserted: the seam is checked to be unmatchable, and the
    attribution rule is driven directly with a straddling span to prove it
    refuses regardless."""

    def _spans(self):
        origin_map = origin.OriginMap("execution")
        origin_map.add(origin.Span(0, 20, origin.ATOM_CONTENT,
                                   unit_sha256="u" * 64, atom_id="a" * 64,
                                   path_bytes_b64="cA=="))
        origin_map.add(origin.Span(20, 40, origin.ATOM_CONTENT,
                                   unit_sha256="u" * 64, atom_id="b" * 64,
                                   path_bytes_b64="cA=="))
        return origin_map

    def test_a_finding_spanning_two_atoms_is_unattributable(self):
        origin_map = self._spans()
        straddling = {"offset": 15, "length": 10, "value_sha256": "c" * 64,
                      "category": "high_entropy_token"}
        attributed, orphaned = origin.attribute([straddling], origin_map)
        assert attributed == []
        assert orphaned == [straddling]

    def test_a_finding_inside_one_atom_is_attributed_to_that_atom(self):
        origin_map = self._spans()
        inside = {"offset": 22, "length": 10, "value_sha256": "c" * 64,
                  "category": "high_entropy_token"}
        attributed, orphaned = origin.attribute([inside], origin_map)
        assert orphaned == []
        assert attributed[0][1].atom_id == "b" * 64

    def test_the_rendered_seam_between_atoms_is_not_token_matchable(self):
        # The bytes the renderer puts between two atoms' content. If a future
        # renderer drops them, a token could span two atoms and this fails.
        seam = "\n" + unitpayload.render_line("new", 2, "")
        assert " " in seam and "|" in seam
        assert not preflight.scan_text("A" * 40 + seam + "B" * 40)


# ------------------------------------------ nothing leaves before a block ----


class TestZeroTransmissionBeforeBlocking:
    def test_a_secret_in_the_LAST_unit_still_costs_zero_calls(self, skeleton,
                                                              clone):
        # The generation is assembled and scanned in full before anything is
        # sent, so the position of the offending unit is irrelevant. Drop the
        # authorizations for the unit the generation reaches LAST: MC4's first
        # attempt counted every earlier unit before it got there.
        full = proposed_authorizations(skeleton, clone)
        authorized_atoms = {r["atom_id"] for r in full.records}
        last_unit = next(
            (u for u in reversed(skeleton["units"])
             if authorized_atoms & set(u["atom_ids"])), None)
        if last_unit is None:
            pytest.skip("no unit in this range carries a detected literal")
        drop = authorized_atoms & set(last_unit["atom_ids"])
        partial = authority.LiteralAuthorizationSet(
            [r for r in full.records if r["atom_id"] not in drop],
            repository_identity="mglaeser/bubble-regime-monitor",
            target_base_sha=skeleton["repository_state"]["target_base_sha"],
            diff_base_sha=skeleton["repository_state"]["diff_base_sha"],
            head_sha=skeleton["repository_state"]["head_sha"])
        # Earlier units ARE still fully authorized, so the block is caused by
        # the last one rather than by the first thing the scanner reaches.
        assert len(partial.records) < len(full.records)
        transport = counting.MockCountTransport()
        with pytest.raises(BlockingError) as e:
            finalize.finalize(skeleton, cwd=clone, operator_pins=good_pins(),
                              transport=transport, authorizations=partial)
        assert e.value.code == SECRET_PREFLIGHT_FAILED
        assert transport.calls == 0

    def test_an_undeclared_transport_source_costs_zero_calls(self):
        class Undeclared:
            """No `source` attribute at all."""

            def __init__(self):
                self.calls = 0

            def post(self, path, body, *, timeout=None):
                self.calls += 1
                return 200, b'{"object":"response.input_tokens",' \
                            b'"input_tokens":1}'

        transport = Undeclared()
        with pytest.raises(BlockingError) as e:
            counting.transport_source(transport)
        assert "transport_source_undeclared" in str(e.value)
        assert transport.calls == 0

    def test_a_cache_hit_performs_no_attempt(self):
        transport = counting.MockCountTransport()
        ledger = counting2.CountLedger(transport, good_pins())
        request = _request()
        ledger.count(request, label="first")
        attempts_after_first = ledger.provider_attempts
        ledger.count(request, label="second")
        assert ledger.cache_hits == 1
        assert ledger.provider_attempts == attempts_after_first
        assert transport.calls == 1
        assert ledger.record()["unique_count_requests"] == 1


# ------------------------------------------------- paths never travel --------


class TestPathIdentitiesNeverReachThePayload:
    """A path identity may reach a provider only as REVIEWED CONTENT.

    Fixing the metadata descriptor removed the only known way for the
    verifier to synthesize one, which is exactly when a guard quietly becomes
    decorative — so it is driven directly, in each of the three positions
    that matter.

    The scope took a correction. A first version searched the whole payload,
    which blocked the precursor range: a test fixture in this repository
    genuinely contains the Base64 of a changed path, as reviewable source
    content. Blocking that is an unclearable false positive — the rule is
    about what the verifier ADDS, not about censoring the repository."""

    ATOM_CONTENT, ATOM_META = "a" * 64, "b" * 64
    UNIT = "u" * 64
    # Computed, never written out: a literal here would itself be a Base64
    # path in this file's changed content, which is the exact false positive
    # the scoping correction was about.
    PATH = b"scripts/verifier/atoms.py"

    def _path_b64(self):
        from verifier.canon import b64
        return b64(self.PATH)

    def _records(self):
        return {
            self.ATOM_CONTENT: {"atom_id": self.ATOM_CONTENT, "side": "new",
                                "line_number": 1, "hunk_id": "h",
                                "path_bytes_b64": self._path_b64()},
            self.ATOM_META: {"atom_id": self.ATOM_META, "side": "meta",
                             "line_number": 0, "hunk_id": "h",
                             "path_bytes_b64": self._path_b64()},
        }

    def _manifest(self, atom_map):
        return finalize.PreflightGenerationManifest(
            None, atom_records=self._records(), atom_map=atom_map)

    def _scan(self, atom_ids, atom_map, *, lens="review this"):
        manifest = self._manifest(atom_map)
        unit = {"unit_sha256": self.UNIT, "atom_ids": list(atom_ids),
                "git_status": "M", "path_bytes_b64": self._path_b64()}
        manifest._units_by_hash[self.UNIT] = unit
        payload = unitpayload.structured_unit(unit, self._records(), atom_map)
        request = providerreq.build_request(
            "gpt-5.3-codex", [payload], lens=lens, challenge="CH-PATH",
            reasoning_effort="medium", max_output_tokens=8_000)
        return manifest._scan_payload(
            request.transmitted_text(), label="unit:x:execution",
            unit_count=1, request=request,
            cleared_atoms=frozenset(atom_ids), authorized_occurrences=0)

    def test_a_path_identity_in_a_metadata_atom_blocks(self):
        # The A2-F21 leak itself: the verifier's own descriptor carrying the
        # path it is describing.
        with pytest.raises(BlockingError) as e:
            self._scan([self.ATOM_META],
                       {self.ATOM_META: f'{{"kind":"new_file_mode",'
                                        f'"path_bytes_b64":'
                                        f'"{self._path_b64()}"}}'})
        assert "raw_path_identity_in_payload" in str(e.value)
        assert "origin=metadata_atom" in str(e.value)

    def test_a_path_identity_in_scaffolding_blocks(self):
        with pytest.raises(BlockingError) as e:
            self._scan([self.ATOM_CONTENT],
                       {self.ATOM_CONTENT: "harmless line"},
                       lens=f"review this {self._path_b64()} carefully")
        assert "origin=scaffolding" in str(e.value)

    def test_a_path_identity_in_reviewed_source_content_is_allowed(self):
        # A changed source line that genuinely contains a Base64 path is
        # content the reviewer is meant to read. This repository has one.
        entry = self._scan(
            [self.ATOM_CONTENT],
            {self.ATOM_CONTENT: f'PATH_B64 = "{self._path_b64()}"'})
        assert entry["atom_span_count"] == 1

    def test_every_metadata_descriptor_kind_is_path_free(self):
        from verifier import atoms as A
        path, original = b"scripts/gate.py", b"docs/notes.txt"
        for kind, extra in (("rename", {}), ("copy", {}),
                            ("mode_change", {"old_mode": "100644",
                                             "new_mode": "100755"}),
                            ("new_file_mode", {"mode": "100644"}),
                            ("deleted_file_mode", {"mode": "100644"}),
                            ("type_change", {}),
                            ("binary_change", {"note": "n",
                                               "binary_kind": "b"}),
                            ("hunkless_change", {"diff_metadata_sha256":
                                                 "0" * 64})):
            raw = A._descriptor(kind, path, original, "R100", extra)
            assert b"path_bytes_b64" not in raw, kind
            for candidate in (path, original):
                assert candidate not in raw, kind
                assert candidate.decode() not in raw.decode(), kind


# --------------------------------------------- the request itself is inert ----


class TestRequestCarriesNoCapabilities:
    def test_no_tools_no_background_no_truncation_in_the_payload(self):
        body = json.loads(_request().transmitted_text())
        for forbidden in ("tools", "tool_choice", "functions", "background",
                          "store", "parallel_tool_calls", "stream"):
            assert forbidden not in body, forbidden
        assert body["truncation"] == "disabled"

    def test_the_policy_states_the_same_prohibition_it_enforces(self):
        policy = reviewpolicy.policy_record(
            ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"],
            required_approver="gpt-5.6-sol", minimum_other_approvers=1,
            max_output_tokens=8_000)
        assert "no tools" in policy["tools_policy"]
        assert "no background mode" in policy["tools_policy"]
        assert policy["truncation"] == providerreq.TRUNCATION

    def test_the_count_body_omits_the_execution_only_field(self):
        request = _request()
        assert "max_output_tokens" not in request.count_payload()
        assert "max_output_tokens" in request.execution_payload()


# ------------------------------------------------- output capacity record ----


class TestOutputCapacityRecord:
    POLICY_FIELDS = {
        "per_unit_output_tokens", "response_overhead_tokens",
        "max_units_per_batch", "capacity_basis", "capacity_honest_scope",
    }

    def _policy(self, max_output_tokens=8_000):
        return reviewpolicy.policy_record(
            ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"],
            required_approver="gpt-5.6-sol", minimum_other_approvers=1,
            max_output_tokens=max_output_tokens)

    def test_the_record_carries_every_capacity_field(self):
        assert self.POLICY_FIELDS <= set(self._policy())

    def test_the_projection_recomputes_from_the_policy_bounds(self):
        policy = self._policy()
        assert policy["per_unit_output_tokens"] == (
            reviewpolicy.per_unit_output_tokens())
        assert policy["max_units_per_batch"] == (
            reviewpolicy.max_units_per_batch(policy["max_output_tokens"]))
        # The projection is an arithmetic consequence of the two, not a
        # separately maintained constant that could drift from them.
        budget = (policy["max_output_tokens"]
                  - policy["response_overhead_tokens"])
        assert policy["max_units_per_batch"] == (
            budget // policy["per_unit_output_tokens"])

    def test_the_record_refuses_to_call_the_projection_a_measurement(self):
        policy = self._policy()
        assert policy["capacity_basis"] == (
            "POLICY_PROJECTION_NOT_PROVIDER_MEASUREMENT")
        assert "no provider was consulted" in policy["capacity_honest_scope"]

    def test_a_batch_beyond_capacity_blocks(self):
        policy = self._policy()
        over = policy["max_units_per_batch"] + 1
        with pytest.raises(BlockingError) as e:
            reviewpolicy.assert_output_capacity(
                over, policy["max_output_tokens"], where="test")
        assert "output_capacity_exceeded" in str(e.value)


# ------------------------------------------------ the generation ledger ------


class TestGenerationAttemptAccounting:
    def _plan(self, **pins):
        return {
            "review_skeleton_sha256": "f" * 64,
            "executable_plan_sha256": "e" * 64,
            "review_request_policy": reviewpolicy.policy_record(
                ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"],
                required_approver="gpt-5.6-sol", minimum_other_approvers=1,
                max_output_tokens=8_000),
            "operator_pin_record": {"pins": good_pins(**pins)},
            "batches": [{"batch_id": "batch-0000",
                         "unit_sha256_in_order": ["a" * 64],
                         "input_tokens_by_model": {}}],
        }

    def test_the_cap_blocks_before_the_attempt_that_would_exceed_it(self):
        transport = executor.MockGenerationTransport()
        with pytest.raises(BlockingError) as e:
            executor.execute_mock(self._plan(VERIFIER_MAX_GENERATION_CALLS=2),
                                  transport=transport, challenge="CH-CAP")
        assert e.value.code == CHUNK_COUNT_EXHAUSTED
        # Two attempts were permitted and made; the third was refused BEFORE
        # the transport saw it.
        assert transport.calls == 2

    def test_every_attempt_is_recorded_with_its_settlement(self):
        report = executor.execute_mock(
            self._plan(), transport=executor.MockGenerationTransport(),
            challenge="CH-LEDGER")
        ledger = report["generation_ledger"]
        assert ledger["generation_attempts"] == 3
        assert len(ledger["attempts"]) == 3
        assert all(a["result_category"] == "ok" for a in ledger["attempts"])
        assert [a["attempt_number"] for a in ledger["attempts"]] == [1, 2, 3]

    def test_the_ledger_states_it_is_not_provider_usage(self):
        report = executor.execute_mock(
            self._plan(), transport=executor.MockGenerationTransport(),
            challenge="CH-SCOPE")
        assert "not provider usage" in (
            report["generation_ledger"]["honest_scope"])

    def test_without_a_skeleton_drift_is_declared_unchecked_not_skipped(self):
        report = executor.execute_mock(
            self._plan(), transport=executor.MockGenerationTransport(),
            challenge="CH-DRIFT")
        assert report["usage_drift_checked"] is False


# --------------------------------------------- mock evidence stays mock ------


class TestMockCannotBeResealedTrusted:
    def _report(self, skeleton, clone):
        return finalize.finalize(
            skeleton, cwd=clone, operator_pins=good_pins(),
            transport=counting.MockCountTransport(),
            authorizations=proposed_authorizations(skeleton, clone))

    def test_relabelling_the_evidence_class_fails_the_strict_loader(
            self, skeleton, clone):
        report = self._report(skeleton, clone)
        report["count_evidence"]["evidence_class"] = (
            evidence.TRUSTED_COUNT_EVIDENCE)
        with pytest.raises(BlockingError):
            finalize.validate_plan_strict(report)

    def test_relabelling_executable_fails_the_strict_loader(self, skeleton,
                                                            clone):
        report = self._report(skeleton, clone)
        report["executable"] = True
        with pytest.raises(BlockingError):
            finalize.validate_plan_strict(report)

    def test_a_mock_execution_report_declares_no_authority(self):
        report = executor.execute_mock(
            {"review_skeleton_sha256": "f" * 64,
             "executable_plan_sha256": "e" * 64,
             "review_request_policy": reviewpolicy.policy_record(
                 ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"],
                 required_approver="gpt-5.6-sol", minimum_other_approvers=1,
                 max_output_tokens=8_000),
             "operator_pin_record": {"pins": good_pins()},
             "batches": [{"batch_id": "b0",
                          "unit_sha256_in_order": ["a" * 64],
                          "input_tokens_by_model": {}}]},
            transport=executor.MockGenerationTransport(), challenge="CH-M")
        assert report["evidence_class"] == evidence.MOCK_TEST_EVIDENCE
        assert report["executable_authority"] is False
        assert evidence.is_executable_authority(
            report["execution_evidence"]) is False


# ------------------------------------------------ no deprecated machinery ----


class TestNoSupersededSplitHelper:
    def test_the_line_index_splitter_is_gone(self):
        # `_fit_unit` sliced a unit's JOINED text by line index, so a split
        # child could receive its sibling's lines. It was replaced by
        # per-atom content; leaving the old helper importable invites a
        # caller back onto it.
        assert not hasattr(finalize, "_fit_unit")

    def test_splitting_goes_through_the_authoritative_constructor(self):
        source = inspect.getsource(finalize.derive_unit_record)
        assert "units.child_unit_record" in source

    def test_a_child_carries_only_its_own_atoms_content(self, skeleton, clone):
        from verifier import units
        atom_map = finalize.atom_texts(skeleton, cwd=clone)
        atom_records = unitpayload.index_atom_records(skeleton)
        parent = max(skeleton["units"], key=lambda u: len(u["atom_ids"]))
        if len(parent["atom_ids"]) < 2:
            pytest.skip("range has no multi-atom unit to split")
        first, rest = parent["atom_ids"][:1], parent["atom_ids"][1:]
        left = units.child_unit_record(parent, first, atom_records, atom_map,
                                       budget=1 << 30)
        assert left["atom_ids"] == first
        # The sibling's content is absent from the child, byte for byte.
        for atom_id in rest:
            if atom_map[atom_id].strip():
                assert atom_map[atom_id] not in json.dumps(left)


# ---------------------------------------------------- the code under test ----


class TestReportStatesItsOwnProvenance:
    def test_the_report_names_its_stage_and_publication_class(self, skeleton,
                                                              clone):
        report = finalize.finalize(
            skeleton, cwd=clone, operator_pins=good_pins(),
            transport=counting.MockCountTransport(),
            authorizations=proposed_authorizations(skeleton, clone))
        assert report["artifact"] == "mock-finalization-report"
        assert report["publication_class"] == "private"
        assert report["repository_state"]["head_sha"] == (
            skeleton["repository_state"]["head_sha"])
        # The report's skeleton reference is the skeleton's OWN recomputed
        # checksum, so a report cannot claim a skeleton it was not built from.
        assert report["review_skeleton_sha256"] == artifact.compute_self_hash(
            skeleton)

    def test_the_preflight_manifest_binds_both_payload_and_origin_digests(
            self, skeleton, clone):
        report = finalize.finalize(
            skeleton, cwd=clone, operator_pins=good_pins(),
            transport=counting.MockCountTransport(),
            authorizations=proposed_authorizations(skeleton, clone))
        entries = report["preflight_manifest"]["entries"]
        assert entries
        for entry in entries:
            for scan in (entry["count_payload_scan"],
                         entry["execution_payload_scan"]):
                assert len(scan["payload_sha256"]) == 64
                assert len(scan["origin_map_sha256"]) == 64
                assert scan["atom_span_count"] >= 1


# --------------------------------------------------------- the baseline ------


class TestSecretBaselineIsUntouched:
    def test_the_baseline_is_byte_identical_to_main(self):
        # Not "no larger": IDENTICAL. MC3 edited this file and silently
        # dropped the .venv/, __pycache__/ and cache exclusions, because
        # detect-secrets keys filters by function path and a second entry
        # REPLACES the first.
        ours = (ROOT / ".secrets.baseline").read_bytes()
        theirs = subprocess.run(
            ["git", "show", f"{MAIN}:.secrets.baseline"], cwd=ROOT,
            capture_output=True)
        if theirs.returncode != 0:
            pytest.skip("main baseline unavailable")
        assert ours == theirs.stdout


# ---------------------------------------------------------- fixtures ---------


@pytest.fixture(scope="module")
def clone(tmp_path_factory):
    # A real clone, as in the finalize suite: the git execution policy refuses
    # a repository whose LOCAL config carries unsafe settings, and the working
    # repository has some.
    if not (_have(PR25_BASE) and _have(PR25_HEAD)):
        pytest.skip("PR25 objects absent")
    dst = tmp_path_factory.mktemp("passc-clone")
    subprocess.run(["git", "clone", "-q", "--no-local", str(ROOT), str(dst)],
                   check=True, capture_output=True)
    return dst


@pytest.fixture(scope="module")
def skeleton(clone):
    return plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=clone)


def test_strict_response_validation_refuses_a_forged_local_failure():
    # A provider can only produce dict/list/str/int/float/bool/None, so a
    # typed LocalFailure cannot be forged over the wire — but a JSON object
    # shaped like one must not be read as a status either.
    with pytest.raises(BlockingError) as e:
        counting._validate_response({"category": "timeout"}, b"{}")
    assert e.value.code == TOKEN_COUNT_RESPONSE_INVALID
