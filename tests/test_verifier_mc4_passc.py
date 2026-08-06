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
    verdicts,
)
from verifier.errors import (  # noqa: E402
    CHUNK_COUNT_EXHAUSTED,
    SECRET_PREFLIGHT_FAILED,
    TOKEN_COUNT_RESPONSE_INVALID,
    BlockingError,
)

SECRET = "sk-proj-abcdef1234567890abcdef"          # pragma: allowlist secret
MAIN = "b08844a0755710035d62830faa84902d9d85d3fe"  # pragma: allowlist secret


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


# ------------------------------------------- clearance cannot be borrowed ----


class TestCrossBoundaryOccurrence:
    """A finding that is not wholly inside ONE atom span is never clearable.

    The renderer makes a natural straddle hard to produce — every atom is
    preceded by `NEW 000123 | `, which is now its own SCAFFOLDING span, and
    neither the token alphabet nor the Base64 one contains a space or a pipe.
    That is a property of today's renderer, not of the clearance rule, so
    both are asserted: the seam is checked to be unmatchable, and the
    resolution rule is driven directly with a straddling span to prove it
    refuses regardless."""

    def _spans(self):
        origin_map = origin.OriginMap("execution")
        origin_map.add(_span(0, 20, origin.ATOM_CONTENT,
                                   unit_sha256="u" * 64, atom_id="a" * 64,
                                   path_bytes_b64="cA==",
                                   source_text="x" * 20))
        origin_map.add(_span(20, 40, origin.ATOM_CONTENT,
                                   unit_sha256="u" * 64, atom_id="b" * 64,
                                   path_bytes_b64="cA==",
                                   source_text="y" * 20))
        return origin_map

    def _resolve(self, origin_map, finding, source_findings=lambda s: []):
        return origin.resolve_finding(finding, origin_map=origin_map,
                                      authorizations=None,
                                      source_findings=source_findings)

    def test_a_finding_spanning_two_atoms_is_unattributable(self):
        resolution = self._resolve(
            self._spans(),
            {"offset": 15, "length": 10, "value_sha256": "c" * 64,
             "category": "high_entropy_token"})
        assert resolution["cleared"] is False
        assert resolution["refusal"] == origin.OUTSIDE_ANY_SPAN

    def test_a_finding_inside_one_atom_resolves_to_that_atom(self):
        resolution = self._resolve(
            self._spans(),
            {"offset": 22, "length": 10, "value_sha256": "c" * 64,
             "category": "high_entropy_token"})
        assert resolution["span"].atom_id == "b" * 64

    def test_a_finding_in_context_is_refused_by_kind(self):
        origin_map = origin.OriginMap("execution")
        origin_map.add(_span(0, 20, origin.CONTEXT_CONTENT,
                                   source_text="z" * 20))
        resolution = self._resolve(
            origin_map, {"offset": 2, "length": 8, "value_sha256": "c" * 64,
                         "category": "high_entropy_token"})
        assert resolution["refusal"] == origin.IN_CONTEXT

    def test_a_finding_in_a_metadata_descriptor_is_refused_by_kind(self):
        origin_map = origin.OriginMap("execution")
        origin_map.add(_span(0, 20, origin.METADATA_CONTENT,
                                   source_text="z" * 20))
        resolution = self._resolve(
            origin_map, {"offset": 2, "length": 8, "value_sha256": "c" * 64,
                         "category": "high_entropy_token"})
        assert resolution["refusal"] == origin.IN_METADATA

    def test_the_rendered_seam_between_atoms_is_not_token_matchable(self):
        # The bytes the renderer puts between two atoms' content. If a future
        # renderer drops them, a token could span two atoms and this fails.
        seam = "\n" + unitpayload.line_prefix("new", 2)
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
    decorative — so it is driven directly, in each position that matters.

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

    def _scan(self, atom_ids, atom_map, *, lens="review this",
              authorizations=None):
        manifest = finalize.PreflightGenerationManifest(
            authorizations, atom_records=self._records(), atom_map=atom_map)
        unit = {"unit_sha256": self.UNIT, "atom_ids": list(atom_ids),
                "git_status": "M", "path_bytes_b64": self._path_b64()}
        manifest._units_by_hash[self.UNIT] = unit
        payload = unitpayload.structured_unit(unit, self._records(), atom_map)
        assembly = providerreq.assemble_request(
            "gpt-5.3-codex", [payload], lens=lens, challenge="CH-PATH",
            reasoning_effort="medium", max_output_tokens=8_000,
            path_bytes_b64_by_unit={self.UNIT: self._path_b64()})
        return manifest._scan_payload(
            assembly, payload_kind="execution", label="unit:x:execution",
            unit_count=1)

    def test_a_path_identity_in_a_metadata_atom_blocks(self):
        # The A2-F21 leak itself: the verifier's own descriptor carrying the
        # path it is describing.
        with pytest.raises(BlockingError) as e:
            self._scan([self.ATOM_META],
                       {self.ATOM_META: f'{{"kind":"new_file_mode",'
                                        f'"path_bytes_b64":'
                                        f'"{self._path_b64()}"}}'})
        assert "raw_path_identity_in_payload" in str(e.value)
        assert f"origin={origin.METADATA_CONTENT}" in str(e.value)

    def test_a_path_identity_in_scaffolding_blocks(self):
        with pytest.raises(BlockingError) as e:
            self._scan([self.ATOM_CONTENT],
                       {self.ATOM_CONTENT: "harmless line"},
                       lens=f"review this {self._path_b64()} carefully")
        assert "origin=scaffolding" in str(e.value)

    def test_a_path_identity_in_reviewed_source_content_is_allowed(self):
        # A changed source line that genuinely contains a Base64 path is
        # content the reviewer is meant to read. This repository has one.
        # It still needs an authorization like any other detected literal —
        # what it must NOT do is trip the synthesized-path guard.
        line = f'PATH_B64 = "{self._path_b64()}"'
        findings = preflight.distinct_occurrences(preflight.scan_text(line))
        claims = [authority.literal_claim(
            repository_identity="mglaeser/bubble-regime-monitor",
            target_base_sha="b" * 40, diff_base_sha="c" * 40,
            head_sha="d" * 40, path_bytes_b64=self._path_b64(),
            atom_id=self.ATOM_CONTENT, occurrence_index=index,
            literal=line[f["offset"]:f["offset"] + f["length"]],
            literal_category=f["category"], reason="reviewed fixture",
            reviewer_identity="op", authorized_at="t",
            authorization_source="s", test_fixture=True)
            for index, f in enumerate(findings)]
        aset = authority.LiteralAuthorizationSet(
            claims, repository_identity="mglaeser/bubble-regime-monitor",
            target_base_sha="b" * 40, diff_base_sha="c" * 40,
            head_sha="d" * 40)
        entry = self._scan([self.ATOM_CONTENT],
                           {self.ATOM_CONTENT: line}, authorizations=aset)
        assert entry["span_count_by_kind"][origin.ATOM_CONTENT] == 1

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
    """The executor's ledger, exercised through the real end-to-end path.

    These used a hand-written `plan_record` and a caller-supplied challenge.
    After C4-F05/F06/F13 neither is possible: the executor strict-loads the
    report, hash-matches every request, and reads the challenge from the
    plan. The equivalent assertions now live in
    tests/test_verifier_executor.py, which has the fixtures for it; what
    stays here is the part that needs no plan at all."""

    def test_the_retry_budget_is_reserved_before_the_first_attempt(self):
        pins = good_pins(VERIFIER_MAX_GENERATION_CALLS=2,
                         VERIFIER_GENERATION_MAX_RETRIES=2)
        ledger = executor.GenerationLedger(pins)
        with pytest.raises(BlockingError) as e:
            ledger.open_request("gpt-5.3-codex", "b0")
        assert e.value.code == CHUNK_COUNT_EXHAUSTED
        assert ledger.attempts == 0

    def test_the_ledger_states_it_is_not_provider_usage(self):
        ledger = executor.GenerationLedger(good_pins())
        record = ledger.record()
        assert "not provider usage" in record["honest_scope"]
        assert record["retries"] == 0
        assert record["max_retries_per_request"] == good_pins()[
            "VERIFIER_GENERATION_MAX_RETRIES"]

    def test_the_cap_refuses_the_attempt_that_would_exceed_it(self):
        ledger = executor.GenerationLedger(
            good_pins(VERIFIER_MAX_GENERATION_CALLS=1,
                      VERIFIER_GENERATION_MAX_RETRIES=0))
        ledger.open_request("gpt-5.3-codex", "b0")
        ledger.attempt("gpt-5.3-codex", "b0")
        with pytest.raises(BlockingError) as e:
            ledger.attempt("gpt-5.3-codex", "b0")
        assert e.value.code == CHUNK_COUNT_EXHAUSTED


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
            finalize.validate_report_shape(report)

    def test_relabelling_executable_fails_the_strict_loader(self, skeleton,
                                                            clone):
        report = self._report(skeleton, clone)
        report["executable"] = True
        with pytest.raises(BlockingError):
            finalize.validate_report_shape(report)

    def test_an_edited_count_does_not_survive_reconstruction(self, skeleton,
                                                             clone):
        # C4-F04: shape validation recomputes what the record contains.
        # Reconstruction asks whether the record describes its own commits.
        import copy
        report = self._report(skeleton, clone)
        tampered = copy.deepcopy(report)
        tampered["count_ledger"]["counts"][0]["input_tokens"] += 1
        tampered["mock_finalization_report_sha256"] = (
            finalize.mock_report_digest(tampered))
        # Self-consistent: the weaker check passes.
        finalize.validate_report_shape(tampered)
        # Not reproducible from the commits: the strict loader refuses.
        with pytest.raises(BlockingError) as e:
            finalize.validate_mock_finalization_strict(
                tampered, skeleton=skeleton, cwd=clone,
                operator_pins=good_pins(),
                authorizations=proposed_authorizations(skeleton, clone))
        assert "not_reproducible" in str(e.value)

    def test_a_faithful_report_reconstructs(self, skeleton, clone):
        report = self._report(skeleton, clone)
        result = finalize.validate_mock_finalization_strict(
            report, skeleton=skeleton, cwd=clone, operator_pins=good_pins(),
            authorizations=proposed_authorizations(skeleton, clone))
        assert result["reconstructed"] is True

    def test_the_ambiguous_validator_name_is_retired(self):
        with pytest.raises(BlockingError) as e:
            finalize.validate_plan_strict({})
        assert "ambiguous_validator" in str(e.value)


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
                assert scan["origin_mapping_version"] == (
                    origin.MAPPING_VERSION)
                # Scaffolding is mapped too, so the digest describes the
                # WHOLE document rather than only the reviewable part.
                assert scan["span_count_by_kind"][origin.SCAFFOLDING] >= 2
                assert scan["span_count_by_kind"][origin.ATOM_CONTENT] >= 1


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


# ================================================================ PASS E =====
#
# The three findings that survived adversarial verification in the MC4 PASS E
# attack pass. Each is written as the attack, so a regression restores the
# attack rather than merely reddening an assertion.


class TestPassEAuthorizationScopeIsBound:
    """PASS E family A (P1) — the scope path was outside every hash.

    `Span.path_bytes_b64` decides which authorizations apply to a transmitted
    finding. It is excluded from the origin-map record for privacy, and it was
    excluded from provenance too — so two assemblies scoped to DIFFERENT FILES
    produced byte-identical payloads, identical request hashes and an
    identical origin-map record, and one of them cleared a secret on a
    clearance belonging to a file the secret does not live in.
    """

    SECRET = "sk-" + "AAAABBBBCCCCDDDDEEEEFFFF"   # pragma: allowlist secret
    VICTIM = b"app/victim.py"
    REVIEWED = b"app/reviewed_fixture.py"
    ATOM, UNIT = "a" * 64, "u" * 64
    RANGE = dict(repository_identity="r", target_base_sha="b" * 40,
                 diff_base_sha="c" * 40, head_sha="d" * 40)

    def _fixture(self):
        from verifier.canon import b64
        atom_map = {self.ATOM: f'TOKEN = "{self.SECRET}"'}
        atom_records = {self.ATOM: {"atom_id": self.ATOM, "side": "new",
                                    "line_number": 1, "hunk_id": "h",
                                    "path_bytes_b64": b64(self.VICTIM)}}
        unit = {"unit_sha256": self.UNIT, "atom_ids": [self.ATOM],
                "git_status": "M", "path_bytes_b64": b64(self.VICTIM)}
        # An operator reviewed this literal in a DIFFERENT file, file-wide.
        claim = authority.literal_claim(
            path_bytes_b64=b64(self.REVIEWED), atom_id=None,
            occurrence_index=None, literal=self.SECRET,
            literal_category="openai_key", reason="reviewed elsewhere",
            reviewer_identity="op", authorized_at="t",
            authorization_source="s", test_fixture=True, **self.RANGE)
        aset = authority.LiteralAuthorizationSet([claim], **self.RANGE)
        return atom_map, atom_records, unit, aset

    def _assemble(self, unit, atom_records, atom_map, scope_path):
        from verifier.canon import b64
        payload = unitpayload.structured_unit(unit, atom_records, atom_map)
        return providerreq.assemble_request(
            "gpt-5.3-codex", [payload], lens="review", challenge="CH",
            reasoning_effort="medium", max_output_tokens=8_000,
            path_bytes_b64_by_unit={self.UNIT: b64(scope_path)})

    def test_the_truthful_scope_blocks_an_unreviewed_secret(self):
        atom_map, atom_records, unit, aset = self._fixture()
        manifest = finalize.PreflightGenerationManifest(
            aset, atom_records=atom_records, atom_map=atom_map)
        manifest._units_by_hash[self.UNIT] = unit
        assembly = self._assemble(unit, atom_records, atom_map, self.VICTIM)
        with pytest.raises(BlockingError) as e:
            manifest._scan_payload(assembly, payload_kind="execution",
                                   label="x:execution", unit_count=1)
        assert origin.UNAUTHORIZED_OCCURRENCE in str(e.value)

    def test_a_swapped_scope_cannot_be_assembled_at_all(self):
        # THE ATTACK: point the scope at the reviewed file so the clearance
        # matches. The span now refuses, because the path it resolves
        # authorizations against is not the path its evidence records.
        atom_map, atom_records, unit, _ = self._fixture()
        with pytest.raises(BlockingError) as e:
            self._assemble(unit, atom_records, atom_map, self.REVIEWED)
        assert "span_path_scope_mismatch" in str(e.value)

    def test_the_scope_now_participates_in_request_identity(self):
        # Defence in depth: even without the span check, a swapped scope is a
        # different request. Driven at provenance_digest, which the span
        # check would otherwise prevent us from reaching.
        from verifier.canon import b64
        payloads = [unitpayload.structured_unit(
            *self._fixture()[2:3], self._fixture()[1], self._fixture()[0])]
        truthful = providerreq.provenance_digest(
            payloads, "text", "CH", "gpt-5.3-codex",
            {self.UNIT: b64(self.VICTIM)})
        swapped = providerreq.provenance_digest(
            payloads, "text", "CH", "gpt-5.3-codex",
            {self.UNIT: b64(self.REVIEWED)})
        assert truthful != swapped

    def test_provenance_binds_only_this_requests_units(self):
        # A caller may hold the scope map for a whole plan. Binding entries
        # for units this request does not carry would make identical requests
        # hash differently depending on what else the caller knew.
        from verifier.canon import b64
        atom_map, atom_records, unit, _ = self._fixture()
        payload = unitpayload.structured_unit(unit, atom_records, atom_map)
        narrow = providerreq.assemble_request(
            "gpt-5.3-codex", [payload], lens="review", challenge="CH",
            reasoning_effort="medium", max_output_tokens=8_000,
            path_bytes_b64_by_unit={self.UNIT: b64(self.VICTIM)})
        wide = providerreq.assemble_request(
            "gpt-5.3-codex", [payload], lens="review", challenge="CH",
            reasoning_effort="medium", max_output_tokens=8_000,
            path_bytes_b64_by_unit={self.UNIT: b64(self.VICTIM),
                                    "z" * 64: b64(b"other/file.py")})
        assert narrow.hashes() == wide.hashes()

    def test_an_undecodable_scope_blocks(self):
        with pytest.raises(BlockingError) as e:
            _span(0, 5, origin.ATOM_CONTENT, source_text="hello",
                  unit_sha256="u" * 64, atom_id="a" * 64,
                  path_sha256="a" * 64, path_bytes_b64="not!base64")
        assert "span_path_scope_undecodable" in str(e.value)


class TestPassEEveryProviderTextFieldIsScanned:
    """PASS E family C (P2) — `checked_categories` was never scanned.

    Six free-form 48-character strings per unit, validated for length and
    uniqueness only, copied verbatim into the persisted evidence, and scanned
    by nothing — while the same string in `reason` was refused.
    """

    def _record(self, **verdict):
        base = {"reason": "clean", "proof_of_check": "read it",
                "checked_categories": ["logic"]}
        base.update(verdict)
        return {"model_id": "gpt-5.3-codex",
                "verdicts_by_unit": {"a" * 64: base}}

    def test_the_scanned_field_set_is_every_provider_written_field(self):
        # Derived from the verdict schema's own text fields, so a new one
        # cannot be added there and forgotten here.
        assert executor.PROVIDER_TEXT_FIELDS == {
            "reason", "proof_of_check", "checked_categories"}

    def test_a_secret_in_checked_categories_blocks(self):
        leaked = "sk-proj-" + "AbCd1234EfGh5678IjKl9012"  # pragma: allowlist secret
        with pytest.raises(BlockingError) as e:
            executor.assert_output_carries_no_secret(
                [self._record(checked_categories=["logic", leaked])],
                path_identities=frozenset())
        assert "secret_in_provider_output" in str(e.value)
        assert "checked_categories[1]" in str(e.value)

    def test_a_path_identity_in_checked_categories_blocks(self):
        with pytest.raises(BlockingError) as e:
            executor.assert_output_carries_no_secret(
                [self._record(checked_categories=["cA=="])],
                path_identities=frozenset({"cA=="}))
        assert "path_identity_in_provider_output" in str(e.value)

    def test_list_elements_are_scanned_individually(self):
        # Joining the list would let a literal hide across an element
        # boundary; scanning str(list) would scan Python's repr.
        result = executor.assert_output_carries_no_secret(
            [self._record(checked_categories=["logic", "state", "io"])],
            path_identities=frozenset())
        assert result["scanned_field_count"] == 5   # 2 strings + 3 elements


class TestPassECorroboratorsAreComparedToEachOther:
    """PASS E family D (P2) — the anti-canned gate had a blind spot.

    Each corroborator was compared only with the APPROVER. So N models could
    return one byte-identical canned approval among themselves, differ from
    the approver alone, and each be counted as an independent review — which
    satisfies `minimum_other_approvers >= 2` with one sentence repeated.
    """

    CHALLENGE = "CH-1"

    def _verdict(self, reason, proof, refuted=False):
        return {"refuted": refuted,
                "reason": f"{self.CHALLENGE} {reason}",
                "proof_of_check": f"{self.CHALLENGE} {proof}"}

    def test_two_corroborators_with_one_canned_sentence_block(self):
        by_model = {
            "gpt-5.6-sol": {"u": self._verdict("sol looked closely",
                                               "sol read the diff")},
            "gpt-5.3-codex": {"u": self._verdict("looks fine to me",
                                                 "skimmed it")},
            "gpt-4.1-mini": {"u": self._verdict("looks fine to me",
                                                "skimmed it")},
        }
        with pytest.raises(BlockingError) as e:
            verdicts.assert_distinct_reasoning(
                by_model, unit_hash="u", approver="gpt-5.6-sol",
                challenge=self.CHALLENGE,
                corroborators=["gpt-5.3-codex", "gpt-4.1-mini"])
        assert "canned_identical_approval" in str(e.value)

    def test_two_corroborators_with_one_canned_proof_block(self):
        by_model = {
            "gpt-5.6-sol": {"u": self._verdict("sol looked", "sol read it")},
            "gpt-5.3-codex": {"u": self._verdict("codex says a", "same proof")},
            "gpt-4.1-mini": {"u": self._verdict("mini says b", "same proof")},
        }
        with pytest.raises(BlockingError) as e:
            verdicts.assert_distinct_reasoning(
                by_model, unit_hash="u", approver="gpt-5.6-sol",
                challenge=self.CHALLENGE,
                corroborators=["gpt-5.3-codex", "gpt-4.1-mini"])
        assert "canned_identical_proof" in str(e.value)

    def test_three_genuinely_distinct_approvals_pass(self):
        by_model = {
            "gpt-5.6-sol": {"u": self._verdict("no data flow change",
                                               "traced the writes")},
            "gpt-5.3-codex": {"u": self._verdict("the guard still holds",
                                                 "read the branch")},
            "gpt-4.1-mini": {"u": self._verdict("invariants unchanged",
                                                "checked the loop bound")},
        }
        result = verdicts.assert_distinct_reasoning(
            by_model, unit_hash="u", approver="gpt-5.6-sol",
            challenge=self.CHALLENGE,
            corroborators=["gpt-5.3-codex", "gpt-4.1-mini"])
        assert result["distinct_reasoning_count"] == 2

    def test_refuting_models_are_still_exempt(self):
        # Two models describing the same real defect identically is
        # agreement. The gate applies to approvals.
        same = "the null check was removed"
        by_model = {
            "gpt-5.6-sol": {"u": self._verdict("sol approves", "sol read")},
            "gpt-5.3-codex": {"u": self._verdict(same, same, refuted=True)},
            "gpt-4.1-mini": {"u": self._verdict(same, same, refuted=True)},
        }
        verdicts.assert_distinct_reasoning(
            by_model, unit_hash="u", approver="gpt-5.6-sol",
            challenge=self.CHALLENGE,
            corroborators=["gpt-5.3-codex", "gpt-4.1-mini"])
