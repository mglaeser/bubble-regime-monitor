"""MC4 C-HEAL-2 — the findings from the final-report external review.

Each is written as the attack. Two of them (R08, R09) were flagged in the
previous report as limitations I thought were real and the adversarial
verifiers had refuted as intended-scope; the external review agreed they were
real. That is worth recording next to the fix: an "intended scope" refutation
is only as good as the intent, and the intent here was wrong.
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata
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
    authority,
    counting,
    executor,
    finalize,
    generated,
    origin,
    plan,
    preflight,
    providerreq,
    reviewpolicy,
    trustedlane,
    unitpayload,
    verdicts,
)
from verifier.canon import sha256_hex  # noqa: E402
from verifier.errors import (  # noqa: E402
    REQUIRED_APPROVER_REFUTED,
    SECRET_PREFLIGHT_FAILED,
    BlockingError,
)

MODELS = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
RANGE = dict(repository_identity="r", target_base_sha="b" * 40,
             diff_base_sha="c" * 40, head_sha="d" * 40)


def _span(start, end, kind, *, source_text=None, **fields):
    if source_text is not None:
        fields.setdefault("source_content_sha256", sha256_hex(
            source_text.encode("utf-8", "surrogateescape")))
    return origin.Span(start, end, kind, source_text=source_text, **fields)


# =============================================== R02: category sets ==========


class TestR02CategorySetsAreCompared:
    """The transmitted classification must be the SOURCE classification.

    Passing only the source category into the lookup meant a transmitted
    finding classified as Y could be cleared by an authorization for X
    whenever both matched the same raw bytes — the same bytes, but a different
    statement about what they are.
    """

    SECRET = "sk-proj-abcdef1234567890abcdef"        # pragma: allowlist secret

    def test_a_multi_pattern_literal_reports_a_deterministic_category_set(self):
        # "sk-proj-…" is both an openai_key and an openai_project_key. The
        # surviving category used to be whichever pattern was declared first.
        merged = preflight.distinct_occurrences(
            preflight.scan_text(f'k = "{self.SECRET}"'))
        assert len(merged) == 1
        assert merged[0]["categories"] == sorted(merged[0]["categories"])
        assert len(merged[0]["categories"]) >= 2
        assert merged[0]["category_set_sha256"] == (
            preflight.category_set_sha256(merged[0]["categories"]))

    def test_the_set_digest_is_order_independent(self):
        assert preflight.category_set_sha256(["b", "a"]) == (
            preflight.category_set_sha256(["a", "b"]))

    def test_a_different_transmitted_category_set_blocks(self):
        origin_map = origin.OriginMap("execution")
        origin_map.add(_span(0, 40, origin.ATOM_CONTENT, source_text="x" * 40,
                             unit_sha256="u" * 64, atom_id="a" * 64,
                             path_sha256=sha256_hex(b"p"),
                             path_bytes_b64="cA=="))
        source = {"offset": 0, "length": 10, "value_sha256": "f" * 64,
                  "categories": ["openai_key", "openai_project_key"],
                  "category": "openai_key"}
        transmitted = {"offset": 0, "length": 10, "value_sha256": "f" * 64,
                       "categories": ["high_entropy_token"],
                       "category": "high_entropy_token"}
        resolution = origin.resolve_finding(
            transmitted, origin_map=origin_map, authorizations=None,
            source_findings=lambda span: [(0, "f" * 64, source)])
        assert resolution["refusal"] == origin.CATEGORY_SET_MISMATCH

    def test_an_authorization_for_one_category_does_not_clear_another(self):
        claim = authority.literal_claim(
            path_bytes_b64="cA==", atom_id="a" * 64, occurrence_index=0,
            literal=self.SECRET, literal_categories=["openai_key"],
            reason="r", reviewer_identity="op", authorized_at="t",
            authorization_source="s", test_fixture=True, **RANGE)
        aset = authority.LiteralAuthorizationSet([claim], **RANGE)
        assert aset.clears_occurrence(
            literal_sha256=claim["literal_sha256"], path_bytes_b64="cA==",
            atom_id="a" * 64, occurrence_index=0,
            literal_categories=["openai_key"]) is True
        assert aset.clears_occurrence(
            literal_sha256=claim["literal_sha256"], path_bytes_b64="cA==",
            atom_id="a" * 64, occurrence_index=0,
            literal_categories=["openai_key",
                                "openai_project_key"]) is False

    def test_changing_the_scanner_policy_invalidates_a_clearance(self):
        # The clearance binds the vocabulary it was approved under, so a
        # pattern set change cannot be reinterpreted silently.
        claim = authority.literal_claim(
            path_bytes_b64="cA==", atom_id="a" * 64, occurrence_index=0,
            literal=self.SECRET, literal_categories=["openai_key"],
            reason="r", reviewer_identity="op", authorized_at="t",
            authorization_source="s", test_fixture=True, **RANGE)
        assert claim["scanner_policy_sha256"] == (
            preflight.scanner_policy_sha256())
        stale = dict(claim, scanner_policy_sha256="0" * 64)
        stale["authorization_sha256"] = (
            authority.literal_authorization_digest(stale))
        # Still shape-valid — it is a well-formed record for a DIFFERENT
        # policy — and the trusted lane compares the digest before use.
        authority.validate_literal_authorization(stale)
        assert stale["scanner_policy_sha256"] != (
            preflight.scanner_policy_sha256())


# =============================================== R03: source binding ========


class TestR03SourceTextIsBound:
    def test_a_content_span_must_carry_its_source_digest(self):
        with pytest.raises(BlockingError) as e:
            origin.Span(0, 5, origin.ATOM_CONTENT, source_text="hello")
        assert "span_source_digest_missing" in str(e.value)

    def test_a_mutated_source_text_blocks(self):
        with pytest.raises(BlockingError) as e:
            origin.Span(0, 5, origin.ATOM_CONTENT, source_text="hello",
                        source_content_sha256=sha256_hex(b"goodbye"))
        assert "span_source_digest_mismatch" in str(e.value)

    def test_a_content_span_without_source_text_blocks(self):
        # A span whose findings could never be mapped back is not a usable
        # span; it is a hole the resolver would report as "unattributable".
        with pytest.raises(BlockingError) as e:
            origin.Span(0, 5, origin.ATOM_CONTENT,
                        source_content_sha256=sha256_hex(b"x"))
        assert "span_source_text_missing" in str(e.value)

    def test_scaffolding_needs_no_source_text(self):
        span = origin.Span(0, 5, origin.SCAFFOLDING)
        assert span.kind == origin.SCAFFOLDING


# =============================================== R04: partition proof =======


class TestR04OriginMapIsAnExactPartition:
    def _map(self, *spans):
        origin_map = origin.OriginMap("execution")
        for span in spans:
            origin_map.add(span)
        return origin_map

    def _atom(self, start, end, text):
        return _span(start, end, origin.ATOM_CONTENT, source_text=text,
                     unit_sha256="u" * 64, atom_id="a" * 64,
                     path_sha256=sha256_hex(b"p"), path_bytes_b64="cA==")

    def test_a_gap_blocks(self):
        origin_map = self._map(origin.Span(0, 5, origin.SCAFFOLDING),
                               origin.Span(7, 10, origin.SCAFFOLDING))
        with pytest.raises(BlockingError) as e:
            origin_map.validate("x" * 10)
        assert "origin_map_not_contiguous" in str(e.value)

    def test_an_overlap_blocks(self):
        origin_map = self._map(origin.Span(0, 6, origin.SCAFFOLDING),
                               origin.Span(5, 10, origin.SCAFFOLDING))
        with pytest.raises(BlockingError) as e:
            origin_map.validate("x" * 10)
        assert "origin_map_not_contiguous" in str(e.value)

    def test_out_of_order_spans_block(self):
        origin_map = self._map(origin.Span(5, 10, origin.SCAFFOLDING),
                               origin.Span(0, 5, origin.SCAFFOLDING))
        with pytest.raises(BlockingError) as e:
            origin_map.validate("x" * 10)
        assert "origin_map_not_contiguous" in str(e.value)

    def test_an_uncovered_suffix_blocks(self):
        origin_map = self._map(origin.Span(0, 5, origin.SCAFFOLDING))
        with pytest.raises(BlockingError) as e:
            origin_map.validate("x" * 10)
        assert "does_not_cover_payload" in str(e.value)

    def test_an_empty_span_blocks(self):
        origin_map = self._map(origin.Span(0, 5, origin.SCAFFOLDING),
                               origin.Span(5, 5, origin.SCAFFOLDING),
                               origin.Span(5, 10, origin.SCAFFOLDING))
        with pytest.raises(BlockingError) as e:
            origin_map.validate("x" * 10)
        assert "empty_or_inverted" in str(e.value)

    def test_an_atom_span_without_path_binding_blocks(self):
        origin_map = self._map(
            _span(0, 10, origin.ATOM_CONTENT, source_text="x" * 10,
                  unit_sha256="u" * 64, atom_id="a" * 64))
        with pytest.raises(BlockingError) as e:
            origin_map.validate("x" * 10)
        assert "origin_span_field_missing" in str(e.value)

    def test_scaffolding_carrying_an_atom_identity_blocks(self):
        origin_map = self._map(
            origin.Span(0, 10, origin.SCAFFOLDING, atom_id="a" * 64))
        with pytest.raises(BlockingError) as e:
            origin_map.validate("x" * 10)
        assert "origin_span_field_forbidden" in str(e.value)

    def test_context_carrying_an_atom_identity_blocks(self):
        # Context is not part of the change, so it must not carry an identity
        # a source clearance could key on.
        origin_map = self._map(
            _span(0, 10, origin.CONTEXT_CONTENT, source_text="x" * 10,
                  atom_id="a" * 64))
        with pytest.raises(BlockingError) as e:
            origin_map.validate("x" * 10)
        assert "origin_span_field_forbidden" in str(e.value)

    def test_an_empty_map_blocks(self):
        with pytest.raises(BlockingError) as e:
            origin.OriginMap("execution").validate("x")
        assert "origin_map_empty" in str(e.value)

    def test_a_valid_partition_proves_and_binds_the_payload(self):
        text = "x" * 4 + "abcd" + "y" * 2
        origin_map = self._map(origin.Span(0, 4, origin.SCAFFOLDING),
                               self._atom(4, 8, "abcd"),
                               origin.Span(8, 10, origin.SCAFFOLDING))
        proof = origin_map.validate(text, request_hash="r" * 64)
        assert proof["partition_proved"] is True
        assert proof["payload_chars"] == 10
        assert proof["payload_sha256"] == sha256_hex(text.encode())
        assert proof["request_hash"] == "r" * 64

    def test_the_real_assembler_produces_a_valid_partition(self):
        atom_map = {"a" * 64: 'value = "ordinary line"'}
        records = {"a" * 64: {"atom_id": "a" * 64, "side": "new",
                              "line_number": 1, "hunk_id": "h",
                              "path_bytes_b64": "cA=="}}
        unit = {"unit_sha256": "u" * 64, "atom_ids": ["a" * 64],
                "git_status": "M", "path_bytes_b64": "cA=="}
        payload = unitpayload.structured_unit(unit, records, atom_map)
        assembly = providerreq.assemble_request(
            "gpt-5.3-codex", [payload], lens="review", challenge="CH",
            reasoning_effort="medium", max_output_tokens=8_000,
            path_bytes_b64_by_unit={"u" * 64: "cA=="})
        for kind, text in (("count", assembly.count_text()),
                           ("execution", assembly.execution_text())):
            proof = assembly.origin_map_for(kind).validate(text)
            assert proof["partition_proved"] is True
            assert proof["payload_chars"] == len(text)


# =============================================== R05: exact scope only ======


class TestR05RealCallsRequireExactScope:
    def _claim(self, **over):
        kwargs = dict(path_bytes_b64="cA==", atom_id="a" * 64,
                      occurrence_index=0, literal="x" * 30,
                      literal_categories=["openai_key"], reason="r",
                      reviewer_identity="op", authorized_at="t",
                      authorization_source="s", test_fixture=True, **RANGE)
        kwargs.update(over)
        return authority.literal_claim(**kwargs)

    def test_a_file_wide_clearance_gets_its_own_class(self):
        record = self._claim(atom_id=None, occurrence_index=None)
        assert record["authority_class"] == authority.TEST_FILE_WIDE_CLEARANCE

    def test_an_atom_wide_clearance_gets_its_own_class(self):
        record = self._claim(occurrence_index=None)
        assert record["authority_class"] == authority.TEST_ATOM_WIDE_CLEARANCE

    def test_an_exact_clearance_keeps_the_fixture_class(self):
        assert self._claim()["authority_class"] == (
            authority.TEST_FIXTURE_UNAUTHORIZED)

    @pytest.mark.parametrize("broad", [
        {"atom_id": None, "occurrence_index": None},
        {"occurrence_index": None},
    ])
    def test_a_broad_scope_never_authorizes_a_real_call(self, broad):
        with pytest.raises(BlockingError) as e:
            authority.assert_usable_for_real_call(self._claim(**broad))
        assert "real_call" in str(e.value)

    def test_every_test_only_class_is_refused_for_a_real_call(self):
        for cls in authority.TEST_ONLY_CLASSES:
            record = dict(self._claim(), authority_class=cls)
            record["authorization_sha256"] = (
                authority.literal_authorization_digest(record))
            with pytest.raises(BlockingError):
                authority.assert_usable_for_real_call(record)

    def test_the_real_call_field_list_names_every_exact_scope_component(self):
        for field in ("path_bytes_b64", "atom_id", "occurrence_index",
                      "literal_categories", "scanner_policy_sha256",
                      "expires_at"):
            assert field in authority.REAL_CALL_REQUIRED_EXACT


# =============================================== R06: stray Part 2 ==========


class TestR06StrayPartTwoIsNotWhollyAbsent:
    """A base holding ONLY a stray part2.md looked like a base holding nothing.

    `relationship_at_single` decided occupancy from `relationship_paths()`,
    which omits the paths v1 declares ABSENT — so an added v1 relationship
    could satisfy its base-absence requirement over a base that already
    contained Part 2's text, which v1's own header says does not exist.
    """

    def _repo(self, tmp_path, files):
        import os
        repo = tmp_path / "r"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@e"}
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        for name, body in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=repo,
                       check=True, env=env)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()
        return repo, head

    def test_the_absent_path_universe_names_part_two(self):
        assert "governance/mandate/part2.md" in generated.V1_ABSENT_PATHS

    def test_a_commit_with_only_a_stray_part_two_is_partial_broken(
            self, tmp_path):
        repo, head = self._repo(tmp_path, {
            "governance/mandate/part2.md": b"recovered volume two\n"})
        state = generated.relationship_at_single(head, cwd=repo)
        assert state["state"] == generated.PARTIAL_BROKEN
        assert state["declared_absent_paths_present"] == 1
        assert state["reason_category"] == "declared_absent_path_present_alone"

    def test_a_commit_with_nothing_is_wholly_absent(self, tmp_path):
        repo, head = self._repo(tmp_path, {"README.md": b"hi\n"})
        state = generated.relationship_at_single(head, cwd=repo)
        assert state["state"] == generated.WHOLLY_ABSENT

    def test_an_added_relationship_over_a_stray_base_is_ineligible(self):
        # The eligibility check requires base WHOLLY_ABSENT. With the stray
        # path in the universe, that base is PARTIAL_BROKEN, so an ADDED v1
        # relationship over it blocks.
        endpoints = {
            "head": {"state": generated.PRESENT_VERIFIED},
            "base": {"state": generated.PARTIAL_BROKEN,
                     "declared_absent_paths_present": 1},
        }

        class Change:
            status = "A"
            orig_path = None
            path = b"governance/mandate.md"
            new_mode = generated.PINNED_MODE
            old_mode = None

        class Atom:
            def __init__(self, side):
                self.atom_id = "c" * 64
                self.side = side
                self.path = b"governance/mandate.md"

        with pytest.raises(BlockingError) as e:
            generated.eligible_generated_atoms(
                Change(), [Atom("new")], endpoints, {})
        assert "added_generated_file_with_base_relationship" in str(e.value)


# =============================================== R07: refutation artifact ===


@pytest.fixture(scope="module")
def clone(tmp_path_factory):
    if not (_have(PR25_BASE) and _have(PR25_HEAD)):
        pytest.skip("PR25 objects absent")
    dst = tmp_path_factory.mktemp("cheal2-clone")
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


class TestR07RefutationSurvivesAsEvidence:
    def _run(self, plan_record, skeleton, clone, transport):
        return executor.execute_mock(
            plan_record, transport=transport, skeleton=skeleton, cwd=clone,
            operator_pins=good_pins(),
            authorizations=proposed_authorizations(skeleton, clone))

    def test_a_later_batch_refutation_is_retained(self, plan_record, skeleton,
                                                  clone):
        # Not only the first batch: the process must reach the end and keep
        # every finding on the way.
        if len(plan_record["batches"]) < 2:
            pytest.skip("range has a single batch")
        last = set(plan_record["batches"][-1]["unit_sha256_in_order"])
        report = self._run(plan_record, skeleton, clone,
                           executor.MockGenerationTransport(
                               refute={"gpt-5.6-sol": last}))
        assert report["review_status"] == "BLOCKED"
        assert set(report["synthesis"]["refuted_unit_sha256"]) >= last
        # Every batch ran, so the ledger is complete rather than truncated.
        assert report["generation_ledger"]["generation_attempts"] == len(
            plan_record["batches"]) * len(MODELS)

    def test_the_block_is_a_separate_step_from_the_artifact(self, plan_record,
                                                            skeleton, clone):
        units = set(plan_record["batches"][0]["unit_sha256_in_order"])
        report = self._run(plan_record, skeleton, clone,
                           executor.MockGenerationTransport(
                               refute={"gpt-5.6-sol": units}))
        assert report["mock_execution_report_sha256"]
        assert REQUIRED_APPROVER_REFUTED in report["block_codes"]
        with pytest.raises(BlockingError):
            executor.assert_review_clean(report)

    def test_synthesis_still_cannot_clear_a_retained_refutation(
            self, plan_record, skeleton, clone):
        units = set(plan_record["batches"][0]["unit_sha256_in_order"])
        report = self._run(plan_record, skeleton, clone,
                           executor.MockGenerationTransport(
                               refute={"gpt-4.1-mini": units}))
        assert report["synthesis"]["overall_approved"] is False
        assert set(report["synthesis"]["refuted_unit_sha256"]) >= units


# =============================================== R08: lens categories =======


class TestR08LensCategoriesAreEnforced:
    """Different instructions are an input; lens diversity is about output.

    v1 accepted arbitrary bounded text and the mock returned ["logic","state"]
    for every model, so nothing in the evidence showed that Sol performed a
    security review or Mini an invariant review.
    """

    def test_each_model_has_a_lens_id_and_a_closed_vocabulary(self):
        for model_id in MODELS:
            assert reviewpolicy.lens_id(model_id)
            assert reviewpolicy.lens_categories(model_id)
            assert set(reviewpolicy.lens_required_group(model_id)) <= set(
                reviewpolicy.lens_categories(model_id))

    def test_the_vocabularies_do_not_overlap_between_lenses(self):
        seen = set()
        for model_id in MODELS:
            categories = set(reviewpolicy.lens_categories(model_id))
            assert not (categories & seen), model_id
            seen |= categories

    def test_the_schema_pins_the_lens_id_and_the_enum(self):
        schema = providerreq.verdict_schema(
            ["a" * 64], challenge="CH", model_id="gpt-5.6-sol")
        assert schema["schema"]["properties"]["lens_id"]["const"] == (
            reviewpolicy.lens_id("gpt-5.6-sol"))
        items = schema["schema"]["properties"]["verdicts_by_unit"][
            "properties"]["a" * 64]["properties"]["checked_categories"]["items"]
        assert items["enum"] == list(
            reviewpolicy.lens_categories("gpt-5.6-sol"))

    def _parsed(self, model_id, categories, lens=None):
        return {
            "lens_id": lens or reviewpolicy.lens_id(model_id),
            "challenge": "CH",
            "verdicts_by_unit": {"a" * 64: {
                "refuted": False, "confidence": "high",
                "reason": "CH a substantive statement about this change",
                "proof_of_check": "CH traced the changed statements",
                "checked_categories": categories}},
        }

    def _policy(self):
        return reviewpolicy.policy_record(
            MODELS, required_approver="gpt-5.6-sol",
            minimum_other_approvers=1, max_output_tokens=8_000)

    def test_a_category_outside_the_lens_vocabulary_blocks(self):
        with pytest.raises(BlockingError) as e:
            verdicts.validate_verdicts(
                self._parsed("gpt-5.6-sol", ["logic"]),
                unit_hashes=["a" * 64], challenge="CH",
                review_policy=self._policy(), model_id="gpt-5.6-sol")
        assert "outside_lens_vocabulary" in str(e.value)

    def test_a_vocabulary_category_outside_the_required_group_blocks(self):
        with pytest.raises(BlockingError) as e:
            verdicts.validate_verdicts(
                self._parsed("gpt-5.6-sol", ["privacy"]),
                unit_hashes=["a" * 64], challenge="CH",
                review_policy=self._policy(), model_id="gpt-5.6-sol")
        assert "no_required_lens_category" in str(e.value)

    def test_a_wrong_lens_id_blocks(self):
        with pytest.raises(BlockingError) as e:
            verdicts.validate_verdicts(
                self._parsed("gpt-5.6-sol", ["data_flow"],
                             lens=reviewpolicy.lens_id("gpt-4.1-mini")),
                unit_hashes=["a" * 64], challenge="CH",
                review_policy=self._policy(), model_id="gpt-5.6-sol")
        assert "lens_id_mismatch" in str(e.value)

    def test_a_correct_lens_verdict_validates(self):
        verdicts.validate_verdicts(
            self._parsed("gpt-5.6-sol", ["data_flow", "secret_handling"]),
            unit_hashes=["a" * 64], challenge="CH",
            review_policy=self._policy(), model_id="gpt-5.6-sol")

    def test_the_mock_returns_model_specific_categories(self, plan_record,
                                                        skeleton, clone):
        report = executor.execute_mock(
            plan_record, transport=executor.MockGenerationTransport(),
            skeleton=skeleton, cwd=clone, operator_pins=good_pins(),
            authorizations=proposed_authorizations(skeleton, clone))
        by_model = {r["model_id"]: r for r in
                    report["batch_results"][0]["per_model_verdict_evidence"]}
        seen = {}
        for model_id, record in by_model.items():
            verdict = next(iter(record["verdicts_by_unit"].values()))
            categories = set(verdict["checked_categories"])
            assert categories <= set(reviewpolicy.lens_categories(model_id))
            seen[model_id] = categories
        # Three lenses, three disjoint category sets: the evidence now
        # distinguishes them.
        assert len({frozenset(v) for v in seen.values()}) == len(MODELS)


# =============================================== R09: anti-canned ===========


class TestR09AntiCannedBypassesAreClosed:
    CHALLENGE = "CH-1"
    LENS_IDS = [reviewpolicy.lens_id(m) for m in MODELS]

    def _gate(self, by_model, corroborators):
        return verdicts.assert_distinct_reasoning(
            by_model, unit_hash="u", approver="gpt-5.6-sol",
            challenge=self.CHALLENGE, corroborators=corroborators,
            model_ids=MODELS, lens_ids=self.LENS_IDS)

    def _v(self, reason, proof, refuted=False):
        return {"refuted": refuted,
                "reason": f"{self.CHALLENGE} {reason}",
                "proof_of_check": f"{self.CHALLENGE} {proof}"}

    def test_the_gate_names_itself_a_tripwire_not_a_proof(self):
        assert verdicts.GATE_SEMANTICS == (
            "ANTI_COPY_TRIPWIRE_NOT_PROOF_OF_INDEPENDENT_REASONING")

    def test_a_model_name_suffix_no_longer_makes_a_copy_distinct(self):
        # The bypass the previous mock itself demonstrated.
        canned = "nothing wrong with this unit"
        by_model = {
            "gpt-5.6-sol": {"u": self._v(canned, "read it as gpt-5.6-sol")},
            "gpt-5.3-codex": {"u": self._v(f"{canned} as gpt-5.3-codex",
                                           "read it as gpt-5.3-codex")},
        }
        with pytest.raises(BlockingError) as e:
            self._gate(by_model, ["gpt-5.3-codex"])
        assert "canned_identical" in str(e.value)

    def test_a_lens_name_suffix_no_longer_makes_a_copy_distinct(self):
        canned = "nothing wrong with this unit"
        by_model = {
            "gpt-5.6-sol": {"u": self._v(canned, "alpha proof")},
            "gpt-5.3-codex": {"u": self._v(
                f"{canned} {reviewpolicy.lens_id('gpt-5.3-codex')}",
                "beta proof")},
        }
        with pytest.raises(BlockingError) as e:
            self._gate(by_model, ["gpt-5.3-codex"])
        assert "canned_identical" in str(e.value)

    def test_a_cyrillic_homoglyph_is_refused_by_the_charset_policy(self):
        canned = "nothing was wrong with this changed unit"
        homoglyph = canned.replace("a", "а", 1)   # Cyrillic small a
        assert homoglyph != canned
        assert verdicts.normalize_reason(homoglyph, challenge="") != (
            verdicts.normalize_reason(canned, challenge="")), (
            "the fixture must actually defeat plain equality")
        by_model = {
            "gpt-5.6-sol": {"u": self._v(canned, "alpha proof")},
            "gpt-5.3-codex": {"u": self._v(homoglyph, "beta proof")},
        }
        with pytest.raises(BlockingError) as e:
            self._gate(by_model, ["gpt-5.3-codex"])
        assert "reason_charset_outside_policy" in str(e.value)

    def test_a_zero_width_character_is_refused_by_the_charset_policy(self):
        canned = "nothing wrong with this unit"
        by_model = {
            "gpt-5.6-sol": {"u": self._v(canned, "alpha proof")},
            "gpt-5.3-codex": {"u": self._v(canned.replace(" ", "​ ", 1),
                                           "beta proof")},
        }
        with pytest.raises(BlockingError) as e:
            self._gate(by_model, ["gpt-5.3-codex"])
        assert "reason_charset_outside_policy" in str(e.value)

    def test_normalization_folds_compatibility_forms_and_ignorables(self):
        # The fold is exercised directly too: the charset policy is the first
        # line of defence, and this is the second.
        text = unicodedata.normalize("NFD", "café") + "‍"
        assert verdicts.normalize_reason(text, challenge="") == (
            verdicts.normalize_reason("café", challenge=""))

    def test_punctuation_only_variation_is_still_a_copy(self):
        canned = "nothing wrong with this unit"
        by_model = {
            "gpt-5.6-sol": {"u": self._v(canned, "alpha proof")},
            "gpt-5.3-codex": {"u": self._v(canned + "!!!", "beta proof")},
        }
        with pytest.raises(BlockingError):
            self._gate(by_model, ["gpt-5.3-codex"])

    def test_a_reordered_sentence_is_still_a_copy(self):
        by_model = {
            "gpt-5.6-sol": {"u": self._v("the guard still holds here",
                                         "alpha proof")},
            "gpt-5.3-codex": {"u": self._v("here the guard holds still",
                                           "beta proof")},
        }
        with pytest.raises(BlockingError) as e:
            self._gate(by_model, ["gpt-5.3-codex"])
        assert "similarity_bp" in str(e.value)

    def test_a_high_overlap_paraphrase_above_threshold_blocks(self):
        by_model = {
            "gpt-5.6-sol": {"u": self._v(
                "no untrusted value reaches a sensitive sink here",
                "alpha proof")},
            "gpt-5.3-codex": {"u": self._v(
                "no untrusted value reaches a sensitive sink",
                "beta proof")},
        }
        with pytest.raises(BlockingError):
            self._gate(by_model, ["gpt-5.3-codex"])

    def test_genuinely_lens_distinct_reasons_pass(self):
        by_model = {
            "gpt-5.6-sol": {"u": self._v(
                "no untrusted value reaches a sensitive sink",
                "traced every write and boundary crossing")},
            "gpt-5.3-codex": {"u": self._v(
                "the changed branch preserves its precondition",
                "walked each altered statement and its error path")},
            "gpt-4.1-mini": {"u": self._v(
                "the denominator and fail-closed default are unchanged",
                "recomputed the coverage arithmetic against the guard")},
        }
        result = self._gate(by_model, ["gpt-5.3-codex", "gpt-4.1-mini"])
        assert result["distinct_reasoning_count"] == 2
        assert result["similarity_threshold_bp"] == (
            verdicts.DEFAULT_SIMILARITY_THRESHOLD_BP)

    def test_identical_refutations_remain_allowed(self):
        same = "the null check was removed"
        by_model = {
            "gpt-5.6-sol": {"u": self._v("sol approves after tracing",
                                         "alpha proof")},
            "gpt-5.3-codex": {"u": self._v(same, same, refuted=True)},
            "gpt-4.1-mini": {"u": self._v(same, same, refuted=True)},
        }
        self._gate(by_model, ["gpt-5.3-codex", "gpt-4.1-mini"])

    def test_the_threshold_is_carried_in_the_policy_record(self):
        policy = reviewpolicy.policy_record(
            MODELS, required_approver="gpt-5.6-sol",
            minimum_other_approvers=1, max_output_tokens=8_000)
        assert policy["anti_canned_similarity_threshold_bp"] == 8500
        assert policy["anti_canned_gate_semantics"] == verdicts.GATE_SEMANTICS
        assert policy["reason_charset_policy"] == (
            verdicts.REASON_CHARSET_POLICY)

    def test_similarity_is_order_insensitive(self):
        assert verdicts.similarity_bp("a b c", "c b a") == 10_000
        assert verdicts.similarity_bp("a b c", "x y z") == 0


# =============================================== R13: honest ceiling ========


class TestR13CeilingIsNotCalledOperatorAuthorized:
    def test_the_ceiling_declares_its_own_basis(self):
        assert counting.INPUT_COUNT_CEILING_BASIS == (
            "IMPLEMENTATION_SANITY_NOT_OPERATOR_AUTHORIZED")

    def test_no_comment_calls_it_operator_authorized(self):
        import inspect
        source = inspect.getsource(counting)
        window = source[source.index("MAX_SANE_INPUT_TOKENS") - 700:
                        source.index("MAX_SANE_INPUT_TOKENS") + 200]
        assert "operator-authorized sanity" not in window

    def test_it_is_not_a_pin(self):
        # No operator PIN binds this number, which is exactly why calling it
        # operator-authorized was wrong.
        from verifier import pins as pinsmod
        assert not any("SANE" in name for name in pinsmod._INT_PINS)
        assert not any("CEILING" in name for name in pinsmod._INT_PINS)


# =============================================== R10/R14: engine identity ===


class TestR10EngineIdentityIsRequiredAndAbsent:
    def test_the_engine_identity_package_names_every_mandated_field(self):
        for field in ("source_commit", "code_cutoff_sha", "source_tree_sha256",
                      "artifact_sha256", "dependency_lock_sha256",
                      "sbom_sha256", "runner_image_digest",
                      "build_workflow_run_id", "build_workflow_run_attempt",
                      "signer_identity", "signature", "provenance",
                      "independent_approval_record"):
            assert field in trustedlane.ENGINE_IDENTITY_FIELDS, field

    def test_the_candidate_head_is_not_an_approved_engine(self):
        with pytest.raises(BlockingError) as e:
            trustedlane.assert_engine_approved(
                trustedlane.engine_identity_template())
        assert "engine_not_approved" in str(e.value)

    def test_a_fully_populated_identity_is_still_unverified_here(self):
        record = {f: "x" for f in trustedlane.ENGINE_IDENTITY_FIELDS}
        result = trustedlane.validate_engine_identity_shape(record)
        assert result["authenticated"] is False
        with pytest.raises(BlockingError):
            trustedlane.assert_engine_approved(record)

    def test_loading_the_candidate_checkout_as_the_engine_is_refused(self):
        with pytest.raises(BlockingError) as e:
            trustedlane.assert_engine_is_not_candidate_checkout(
                str(ROOT / "scripts" / "verifier"))
        assert "engine_from_candidate_checkout" in str(e.value)

    def test_the_normalization_contract_targets_the_real_envelope(self):
        assert trustedlane.NORMALIZATION_CONTRACT[
            "produces_envelope_version"] == executor.RESPONSE_ENVELOPE_VERSION
        assert trustedlane.normalization_record()[
            "adapter_digest_verified"] is False


# =============================================== R15: mock config binding ===


class TestR15MockTransportConfigurationIsBound:
    def test_the_report_binds_the_exact_mock_transport_configuration(
            self, plan_record, skeleton, clone):
        report = executor.execute_mock(
            plan_record, transport=executor.MockGenerationTransport(),
            skeleton=skeleton, cwd=clone, operator_pins=good_pins(),
            authorizations=proposed_authorizations(skeleton, clone))
        config = report["mock_transport_configuration"]
        assert config["source"] == counting.SOURCE_MOCK
        assert config["refute"] == {}
        assert config["identical_reasons"] is False
        assert config["usage_input_tokens"] is None
        assert len(config["mock_transport_configuration_sha256"]) == 64

    def test_a_differently_configured_mock_produces_a_different_report(
            self, plan_record, skeleton, clone):
        # MC4-R15: reconstruction ran the executor again with a DEFAULT mock,
        # so a report produced by a differently configured stand-in could be
        # "reproduced" by a run that behaved differently. The configuration is
        # now part of the report, so the two cannot agree.
        units = set(plan_record["batches"][0]["unit_sha256_in_order"])
        blocked = executor.execute_mock(
            plan_record,
            transport=executor.MockGenerationTransport(
                refute={"gpt-4.1-mini": units}),
            skeleton=skeleton, cwd=clone, operator_pins=good_pins(),
            authorizations=proposed_authorizations(skeleton, clone))
        assert blocked["mock_transport_configuration"]["refute"] != {}
        with pytest.raises(BlockingError) as e:
            executor.validate_mock_execution_strict(
                blocked, mock_finalization_report=plan_record,
                skeleton=skeleton, cwd=clone, operator_pins=good_pins(),
                authorizations=proposed_authorizations(skeleton, clone))
        assert "not_reproducible" in str(e.value)

    def test_reconstruction_with_the_recorded_configuration_succeeds(
            self, plan_record, skeleton, clone):
        units = set(plan_record["batches"][0]["unit_sha256_in_order"])
        transport = executor.MockGenerationTransport(
            refute={"gpt-4.1-mini": units})
        blocked = executor.execute_mock(
            plan_record, transport=transport, skeleton=skeleton, cwd=clone,
            operator_pins=good_pins(),
            authorizations=proposed_authorizations(skeleton, clone))
        result = executor.validate_mock_execution_strict(
            blocked, mock_finalization_report=plan_record, skeleton=skeleton,
            cwd=clone, operator_pins=good_pins(),
            authorizations=proposed_authorizations(skeleton, clone),
            transport=executor.MockGenerationTransport(
                refute={"gpt-4.1-mini": units}))
        assert result["reconstructed"] is True


def test_the_preflight_scan_never_reports_a_bare_category_without_a_set():
    # Every collapsed finding carries a set; a consumer reading only
    # `category` would be reading one member of it.
    findings = preflight.distinct_occurrences(
        preflight.scan_text('k = "sk-proj-abcdef1234567890abcdef"'))  # pragma: allowlist secret
    for finding in findings:
        assert finding["categories"]
        assert finding["category"] in finding["categories"]


def test_secret_preflight_failure_is_the_shared_block_code():
    # Sanity: the new origin refusals use the code the pipeline already
    # treats as a preflight block, so a caller's error handling is unchanged.
    origin_map = origin.OriginMap("execution")
    with pytest.raises(BlockingError) as e:
        origin_map.validate("x")
    assert e.value.code == SECRET_PREFLIGHT_FAILED
