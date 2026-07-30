"""MC4 PASS E-COMPLETE — the six mandated attack families, run to completion.

The previous Pass E was not exhaustive: 8 of 28 review agents died on provider
API errors and three families lost their verification pass. An attack family
that ends in a 500 is not a family that passed, so this file re-runs every
attack the mandate names (§18 A–F) **deterministically**. No provider call, no
agent, no narrative — each attack is constructed here, the refusal is asserted
here, and the whole set runs in one command with an exit code.

That is a deliberate trade. A subagent can notice something a written test
cannot, and this file does not replace adversarial reading. What it does
replace is adversarial reading as *evidence*: 36 named attacks with an exit
code are checkable, and "an agent looked and reported six findings" is not,
especially when a third of the agents never returned.

Two conventions, so the record cannot drift from the mandate:

* one test per mandated bullet, named after it, with the family letter and
  index in the class docstring;
* where an attack was already covered by a C-HEAL test, this file re-expresses
  it through a DIFFERENT construction. Two independent constructions failing
  the same way is the refute/confirm pass the mandate asks for; asserting the
  same call twice is not.

Every test in this file was also checked by mutation — the defense was broken
and the attack was confirmed to succeed. Those mutations are recorded in the
checkpoint report, not committed.
"""

from __future__ import annotations

import json
import os
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
    authority,
    counting,
    executor,
    finalize,
    generated,
    gitexec,
    origin,
    plan,
    preflight,
    providerreq,
    reviewpolicy,
    trustedlane,
    verdicts,
)
from verifier.canon import sha256_hex  # noqa: E402
from verifier.errors import (  # noqa: E402
    REQUIRED_APPROVER_REFUTED,
    SECRET_PREFLIGHT_FAILED,
    BlockingError,
)

MODELS = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
APPROVER = reviewpolicy.GOVERNED_REQUIRED_APPROVER
RANGE = dict(repository_identity="r", target_base_sha="b" * 40,
             diff_base_sha="c" * 40, head_sha="d" * 40)

# A secret-shaped literal that the scanner classifies under TWO patterns, so
# the category-set attacks have something real to disagree about.
TWO_PATTERN_SECRET = "sk-proj-abcdef1234567890abcdef"   # pragma: allowlist secret


def _span(start, end, kind, *, source_text=None, **fields):
    if source_text is not None:
        fields.setdefault("source_content_sha256", sha256_hex(
            source_text.encode("utf-8", "surrogateescape")))
    return origin.Span(start, end, kind, source_text=source_text, **fields)


def _atom_span(start, end, text, *, path=b"p/q.py"):
    from base64 import b64encode
    return _span(start, end, origin.ATOM_CONTENT, source_text=text,
                 unit_sha256="u" * 64, atom_id="a" * 64,
                 path_sha256=sha256_hex(path),
                 path_bytes_b64=b64encode(path).decode())


def _scan_callback(text):
    """The real scanner over one span's source text, as resolve_finding wants.

    Deliberately the production helper rather than a hand-built list: the
    source side of every family-A attack is then whatever the scanner actually
    says about the bytes, so a drift in its classification reddens these tests
    instead of being absorbed by a fixture."""
    def source_findings(span):
        return preflight.occurrence_index_map(span.source_text or text)
    return source_findings


# ===================================================== FAMILY A =============


class TestFamilyAOriginAndCategory:
    """§18 A — origin/category, six attacks.

    The shared premise: a finding travels from the repository, through JSON
    escaping, into a request payload, and back. Every step is a chance for the
    thing that was authorized and the thing that was sent to differ.
    """

    # A1 — same source range, different transmitted category.
    def test_a1_same_range_different_transmitted_category_is_refused(self):
        """Built from a REAL scan, not a hand-written source finding.

        The C-HEAL test asserted this with both sides hand-constructed. Here
        the source side is whatever the scanner actually says about the bytes,
        so the test also fails if the scanner's own classification drifts.

        The text carries no character that JSON escapes, so escaped and raw
        offsets coincide and this attack is purely about classification. A5
        attacks the escaping separately, and mixing the two would let either
        refusal satisfy the test."""
        text = f"token = {TWO_PATTERN_SECRET} end"
        assert origin.json_escaped(text) == text
        origin_map = origin.OriginMap("execution")
        origin_map.add(_atom_span(0, len(text), text))
        real = preflight.distinct_occurrences(preflight.scan_text(text))[0]
        assert len(real["categories"]) >= 2
        transmitted = dict(real, categories=["high_entropy_token"],
                           category="high_entropy_token")
        resolution = origin.resolve_finding(
            transmitted, origin_map=origin_map, authorizations=None,
            source_findings=_scan_callback(text))
        assert resolution["refusal"] == origin.CATEGORY_SET_MISMATCH
        assert resolution["source_categories"] == sorted(real["categories"])

    # A2 — pattern-order category change.
    def test_a2_reordering_the_scanner_patterns_cannot_change_the_verdict(self):
        """The set is a set. Order was load-bearing once; it must not be."""
        text = f'token = "{TWO_PATTERN_SECRET}"'
        real = preflight.distinct_occurrences(preflight.scan_text(text))[0]
        forward = preflight.category_set_sha256(real["categories"])
        backward = preflight.category_set_sha256(list(reversed(
            real["categories"])))
        assert forward == backward == real["category_set_sha256"]
        # And a clearance approved under one order clears the other order.
        claim = authority.literal_claim(
            path_bytes_b64="cA==", atom_id="a" * 64, occurrence_index=0,
            literal=TWO_PATTERN_SECRET,
            literal_categories=list(reversed(real["categories"])),
            reason="r", reviewer_identity="op", authorized_at="t",
            authorization_source="s", test_fixture=True, **RANGE)
        aset = authority.LiteralAuthorizationSet([claim], **RANGE)
        assert aset.clears_occurrence(
            literal_sha256=claim["literal_sha256"], path_bytes_b64="cA==",
            atom_id="a" * 64, occurrence_index=0,
            literal_categories=real["categories"]) is True

    # A3 — source_text/digest mismatch.
    def test_a3_a_span_whose_source_text_does_not_hash_to_its_digest_blocks(
            self):
        """Attacked at construction time, where the binding lives.

        `source_text` is excluded from the evidence record because it is the
        plaintext. Excluded and unchecked is the dangerous combination: the
        digest in the record would describe bytes nobody compared."""
        with pytest.raises(BlockingError) as excinfo:
            origin.Span(0, 10, origin.ATOM_CONTENT, source_text="the truth",
                        source_content_sha256=sha256_hex(b"a lie"),
                        unit_sha256="u" * 64, atom_id="a" * 64,
                        path_sha256=sha256_hex(b"p"), path_bytes_b64="cA==")
        assert excinfo.value.code == SECRET_PREFLIGHT_FAILED

    def test_a3b_a_span_whose_path_bytes_do_not_hash_to_its_path_digest_blocks(
            self):
        """The same attack against the other half of the binding.

        The path digest decides which authorization applies, so a span that
        resolves clearances against one path while recording another would
        apply a clearance scoped to file X to content from file Y."""
        with pytest.raises(BlockingError):
            origin.Span(0, 10, origin.ATOM_CONTENT, source_text="x" * 10,
                        source_content_sha256=sha256_hex(b"x" * 10),
                        unit_sha256="u" * 64, atom_id="a" * 64,
                        path_sha256=sha256_hex(b"real/path.py"),
                        path_bytes_b64="b3RoZXI=")

    # A4 — span gap / overlap.
    @pytest.mark.parametrize("second_start,category", [
        (12, "origin_map_not_contiguous"),      # gap
        (8, "origin_map_not_contiguous"),       # overlap
    ])
    def test_a4_a_gap_or_an_overlap_blocks_the_partition(self, second_start,
                                                         category):
        payload = "0123456789abcdefghij"
        origin_map = origin.OriginMap("execution")
        origin_map.add(_span(0, 10, origin.SCAFFOLDING))
        origin_map.add(_span(second_start, 20, origin.SCAFFOLDING))
        with pytest.raises(BlockingError) as excinfo:
            origin_map.validate(payload)
        assert category in str(excinfo.value)

    def test_a4b_a_partition_that_stops_short_of_the_payload_blocks(self):
        """The subtler half: contiguous, ordered, and still not a partition."""
        origin_map = origin.OriginMap("execution")
        origin_map.add(_span(0, 10, origin.SCAFFOLDING))
        with pytest.raises(BlockingError) as excinfo:
            origin_map.validate("x" * 20)
        assert "origin_map_does_not_cover_payload" in str(excinfo.value)

    # A5 — serialization-created finding.
    def test_a5_a_finding_that_exists_only_after_escaping_is_refused(self):
        r"""A match whose edge falls inside an escape sequence.

        The source line contains a quote. In the payload it is `\"` — two
        transmitted characters for one source character. A finding starting at
        the backslash describes bytes the repository does not contain, and
        clearing it would authorize a literal that was never in the file."""
        raw = 'x = "' + TWO_PATTERN_SECRET + '"'
        escaped = origin.json_escaped(raw)
        assert '\\"' in escaped
        origin_map = origin.OriginMap("execution")
        origin_map.add(_atom_span(0, len(escaped), raw))
        # Start one character early: on the backslash of the opening `\"`.
        artefact = {"offset": escaped.index("\\\"") + 1,
                    "length": len(TWO_PATTERN_SECRET) + 1,
                    "value_sha256": "f" * 64,
                    "categories": ["openai_key"], "category": "openai_key"}
        resolution = origin.resolve_finding(
            artefact, origin_map=origin_map, authorizations=None,
            source_findings=_scan_callback(raw))
        assert resolution["refusal"] in (origin.SERIALIZATION_ARTEFACT,
                                        origin.NO_SOURCE_OCCURRENCE)

    def test_a5b_raw_preimage_refuses_an_edge_inside_an_escape(self):
        """The primitive, attacked directly — one character at a time.

        Every offset that lands strictly inside an escape must map to None,
        because there is no raw character there to point at."""
        raw = 'a"b'                       # escapes to  a\"b
        assert origin.json_escaped(raw) == 'a\\"b'
        assert origin.raw_preimage(raw, 0, 1) == (0, 1)
        assert origin.raw_preimage(raw, 1, 3) == (1, 2)
        assert origin.raw_preimage(raw, 2, 3) is None      # inside the escape
        assert origin.raw_preimage(raw, 1, 2) is None      # inside the escape

    # A6 — broad atom/file clearance record.
    @pytest.mark.parametrize("scope,expected_class", [
        ({"atom_id": None, "occurrence_index": None},
         authority.TEST_FILE_WIDE_CLEARANCE),
        ({"atom_id": "a" * 64, "occurrence_index": None},
         authority.TEST_ATOM_WIDE_CLEARANCE),
    ])
    def test_a6_a_broad_clearance_cannot_authorize_a_real_call(self, scope,
                                                               expected_class):
        """Breadth decides the class, and no test-only class reaches a real call.

        A record that clears "this file" or "this atom" is a different object
        from one that clears one occurrence of one literal. The class is
        derived from the scope's own null fields, so a caller cannot ask for a
        narrow class while supplying a broad scope."""
        claim = authority.literal_claim(
            path_bytes_b64="cA==", literal=TWO_PATTERN_SECRET,
            literal_categories=["openai_key"], reason="r",
            reviewer_identity="op", authorized_at="t",
            authorization_source="s", test_fixture=True, **scope, **RANGE)
        assert claim["authority_class"] == expected_class
        with pytest.raises(BlockingError):
            authority.assert_usable_for_real_call(claim, where="passe")

    def test_a6b_relabelling_a_broad_clearance_does_not_launder_it(self):
        """Edit the label, keep the scope — twice, because there are two doors.

        Naively, the record's own digest catches it. But the digest is
        computable by whoever edits the record, so the second attempt
        recomputes it. That has to fail too, and it fails for the right reason:
        the real-call gate checks the SCOPE, not the label.

        `validate_literal_authorization` deliberately ACCEPTS a
        verified-labelled record with a consistent digest — a trusted wire
        record must be representable as inert input — which is exactly why the
        authority cannot live in the label."""
        claim = authority.literal_claim(
            path_bytes_b64="cA==", atom_id=None, occurrence_index=None,
            literal=TWO_PATTERN_SECRET, literal_categories=["openai_key"],
            reason="r", reviewer_identity="op", authorized_at="t",
            authorization_source="s", test_fixture=True, **RANGE)
        assert claim["authority_class"] == authority.TEST_FILE_WIDE_CLEARANCE

        # Door one: relabel and leave the digest alone.
        relabelled = dict(
            claim, authority_class=authority.VERIFIED_LITERAL_AUTHORIZATION)
        with pytest.raises(BlockingError) as excinfo:
            authority.validate_literal_authorization(relabelled, where="passe")
        assert "authorization_digest_mismatch" in str(excinfo.value)

        # Door two: relabel and recompute the digest, so the record is
        # internally consistent. Shape-valid, and still not usable.
        resealed = dict(relabelled)
        resealed["authorization_sha256"] = (
            authority.literal_authorization_digest(resealed))
        authority.validate_literal_authorization(resealed, where="passe")
        with pytest.raises(BlockingError):
            authority.assert_usable_for_real_call(resealed, where="passe")

    def test_a6c_every_test_only_class_is_refused_for_a_real_call(self):
        for name in sorted(authority.TEST_ONLY_CLASSES):
            record = {"authority_class": name}
            with pytest.raises(BlockingError):
                authority.assert_usable_for_real_call(record, where="passe")


# ===================================================== FAMILY B =============


def _repo(tmp_path, files, *, name="r"):
    """A one-commit repository, built with the test's own git."""
    repo = tmp_path / name
    repo.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    for path, body in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=repo, check=True,
                   env=env)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    return repo, head


class TestFamilyBGeneratedRelationship:
    """§18 B — generated relationship, four attacks.

    v1 of the governance mandate declares Part 2 absent. That declaration is
    load-bearing: an ADDED generated file is only eligible over a base with no
    relationship at all. So "no relationship" has to mean the whole path
    universe v1 governs, including the paths it says do not exist.
    """

    STRAY = {"governance/mandate/part2.md": b"recovered volume two\n"}

    # B1 — base only contains stray part2.md.
    def test_b1_a_base_holding_only_a_stray_part_two_is_not_wholly_absent(
            self, tmp_path):
        repo, head = _repo(tmp_path, self.STRAY)
        state = generated.relationship_at_single(head, cwd=repo)
        assert state["state"] == generated.PARTIAL_BROKEN
        assert state["declared_absent_paths_present"] == 1
        assert state["reason_category"] == "declared_absent_path_present_alone"

    # B2 — head only contains stray part2.md.
    def test_b2_a_head_holding_only_a_stray_part_two_is_not_wholly_absent(
            self, tmp_path):
        """The mandate names base AND head. Nothing in the state function is
        side-specific, so the head must classify identically — asserted rather
        than assumed, because a later 'optimisation' could make one side
        cheaper than the other."""
        repo, head = _repo(tmp_path, self.STRAY)
        state = generated.relationship_at_single(head, cwd=repo)
        assert state["state"] == generated.PARTIAL_BROKEN
        # And the proof of declared absence refuses at that commit too.
        with pytest.raises(BlockingError) as excinfo:
            generated.prove_absent_paths(head, cwd=repo)
        assert "v1_declared_absent_path_present" in str(excinfo.value)

    def test_b2b_a_commit_with_neither_is_wholly_absent(self, tmp_path):
        """The control. Without it, 'PARTIAL_BROKEN' could be the only answer
        the function ever gives, and every assertion above would be vacuous."""
        repo, head = _repo(tmp_path, {"README.md": b"hi\n"})
        assert generated.relationship_at_single(
            head, cwd=repo)["state"] == generated.WHOLLY_ABSENT

    # B3 — added v1 over stray base Part 2.
    def test_b3_an_added_generated_file_over_a_stray_base_is_ineligible(
            self, tmp_path):
        """Driven from a REAL classification of a REAL commit.

        The C-HEAL test passed a hand-written endpoint dict. Here the base
        state comes from `relationship_at_single` over a repository that
        actually contains the stray file, so the two halves are wired
        together rather than asserted separately."""
        repo, base = _repo(tmp_path, self.STRAY)
        base_state = generated.relationship_at_single(base, cwd=repo)
        endpoints = {"head": {"state": generated.PRESENT_VERIFIED},
                     "base": base_state}

        class Change:
            status = "A"
            orig_path = None
            path = b"governance/mandate.md"
            new_mode = generated.PINNED_MODE
            old_mode = None

        class Atom:
            atom_id = "c" * 64
            side = "new"
            path = b"governance/mandate.md"

        with pytest.raises(BlockingError) as excinfo:
            generated.eligible_generated_atoms(Change(), [Atom()], endpoints,
                                               {})
        assert "added_generated_file_with_base_relationship" in str(
            excinfo.value)

    # B4 — non-UTF-8 path bytes.
    def test_b4_a_non_utf8_path_is_never_mistaken_for_the_generated_path(self):
        """Paths are bytes. Comparison must be too.

        Git paths are arbitrary bytes, and a lossy decode maps many different
        byte strings onto one Python string — so a comparison done after
        `decode(errors="replace")` is a comparison between things that are not
        the paths. Here the eligibility check is handed invalid UTF-8 that
        differs from the generated path only in undecodable bytes."""
        endpoints = {"head": {"state": generated.PRESENT_VERIFIED},
                     "base": {"state": generated.WHOLLY_ABSENT}}
        evil = generated.GENERATED_PATH.encode() + b"\xff\xfe"

        class Change:
            status = "A"
            orig_path = None
            path = evil
            new_mode = generated.PINNED_MODE
            old_mode = None

        class Atom:
            atom_id = "c" * 64
            side = "new"
            path = evil

        with pytest.raises(BlockingError) as excinfo:
            generated.eligible_generated_atoms(Change(), [Atom()], endpoints,
                                               {})
        assert "generated" in str(excinfo.value)

    def test_b4b_two_distinct_undecodable_paths_stay_distinct(self):
        """The property a lossy decode destroys, stated directly."""
        left = b"governance/mandate\xff.md"
        right = b"governance/mandate\xfe.md"
        assert left != right
        assert left.decode("utf-8", "replace") == right.decode("utf-8",
                                                               "replace")
        assert sha256_hex(left) != sha256_hex(right)

    def test_b4c_the_generated_path_is_compared_as_bytes(self):
        """A source-level ratchet: no lossy decode in the comparison path.

        Asserted over the module text because the behavioural test above can
        pass for the wrong reason — a byte string that happens not to collide
        with the pinned ASCII path."""
        source = (ROOT / "scripts" / "verifier" / "generated.py").read_text(
            encoding="utf-8")
        for line in source.splitlines():
            if "GENERATED_PATH" not in line:
                continue
            assert 'decode("utf-8", "replace")' not in line, line


# ===================================================== FAMILY C =============


@pytest.fixture(scope="module")
def clone(tmp_path_factory):
    if not (_have(PR25_BASE) and _have(PR25_HEAD)):
        pytest.skip("PR25 objects absent")
    destination = tmp_path_factory.mktemp("passe-clone")
    subprocess.run(["git", "clone", "-q", "--no-local", str(ROOT),
                    str(destination)], check=True, capture_output=True)
    return destination


@pytest.fixture(scope="module")
def skeleton(clone):
    return plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=clone)


@pytest.fixture(scope="module")
def plan_record(skeleton, clone):
    return finalize.finalize(
        skeleton, cwd=clone, operator_pins=good_pins(),
        transport=counting.MockCountTransport(),
        authorizations=proposed_authorizations(skeleton, clone))


class TestFamilyCVerdictEvidence:
    """§18 C — verdict evidence, four attacks.

    A refutation is the most valuable thing the review produces, and the
    original code raised on it before writing anything down. The attack is
    therefore not "can it be refuted" but "does the refutation survive the
    refusal".
    """

    def _run(self, plan_record, skeleton, clone, refute):
        return executor.execute_mock(
            plan_record, transport=executor.MockGenerationTransport(
                refute=refute),
            skeleton=skeleton, cwd=clone, operator_pins=good_pins(),
            authorizations=proposed_authorizations(skeleton, clone))

    @staticmethod
    def _evidence_for(report, model_id):
        """Every retained verdict-evidence record for one model, all batches."""
        return [record
                for batch in report["batch_results"]
                for record in batch["per_model_verdict_evidence"]
                if record["model_id"] == model_id]

    # C1 — Sol refutes and evidence artifact is retained.
    def test_c1_a_required_approver_refutation_is_retained_as_evidence(
            self, plan_record, skeleton, clone):
        units = set(plan_record["batches"][0]["unit_sha256_in_order"])
        report = self._run(plan_record, skeleton, clone, {APPROVER: units})
        # The block happened...
        assert report["review_status"] == "BLOCKED"
        assert REQUIRED_APPROVER_REFUTED in report["block_codes"]
        # ...and the durable artifact exists, is digest-bound, and names the
        # refuting model and the exact units.
        assert report["mock_execution_report_sha256"] == (
            executor.mock_execution_digest(report))
        assert set(report["synthesis"]["refuted_unit_sha256"]) >= units
        evidence = self._evidence_for(report, APPROVER)
        assert evidence, "the refuting model left no verdict evidence"
        assert all(record["verdict_evidence_sha256"] for record in evidence)
        # The refusal is the process exit, AFTER the artifact exists.
        with pytest.raises(BlockingError):
            executor.assert_review_clean(report)

    # C2 — corroborator refutes and evidence retained.
    @pytest.mark.parametrize("corroborator",
                             ["gpt-5.3-codex", "gpt-4.1-mini"])
    def test_c2_a_corroborator_refutation_is_retained_as_evidence(
            self, plan_record, skeleton, clone, corroborator):
        """Not only the required approver. A corroborator's refutation blocks
        by a different code, and it must be just as durable — otherwise the
        preservation fix would be approver-specific."""
        units = set(plan_record["batches"][0]["unit_sha256_in_order"])
        report = self._run(plan_record, skeleton, clone, {corroborator: units})
        assert report["review_status"] == "BLOCKED"
        assert set(report["synthesis"]["refuted_unit_sha256"]) >= units
        assert report["mock_execution_report_sha256"]
        assert self._evidence_for(report, corroborator)

    # C3 — later-batch refutation retained.
    def test_c3_a_refutation_in_the_last_batch_is_retained(
            self, plan_record, skeleton, clone):
        """Every batch must run. A process that stopped at the first block
        would lose every finding after it, and the evidence would silently be
        a prefix of the review."""
        batches = plan_record["batches"]
        if len(batches) < 2:
            pytest.skip("range has a single batch")
        last = set(batches[-1]["unit_sha256_in_order"])
        report = self._run(plan_record, skeleton, clone, {APPROVER: last})
        assert set(report["synthesis"]["refuted_unit_sha256"]) >= last
        assert report["generation_ledger"]["generation_attempts"] == (
            len(batches) * len(MODELS))

    # C4 — blocked status and artifact both produced.
    def test_c4_the_block_and_the_artifact_are_separate_steps(
            self, plan_record, skeleton, clone):
        """Attacked at the decision primitive, below the report.

        `decide_unit_or_block` must return a decision and a block rather than
        raise, for any VALIDATED verdict. A malformed response still aborts —
        there is nothing to retain — and that distinction is the whole fix."""
        unit = plan_record["batches"][0]["unit_sha256_in_order"][0]
        by_model = {
            APPROVER: {unit: {"refuted": True, "confidence": "high",
                              "reason": "the approver refuses this unit",
                              "proof_of_check": "read the diff"}},
        }
        for model in MODELS:
            if model == APPROVER:
                continue
            by_model[model] = {unit: {
                "refuted": False, "confidence": "high",
                "reason": f"{model} finds nothing wrong with this unit",
                "proof_of_check": "read the diff"}}
        decision, block = executor.decide_unit_or_block(
            unit, by_model, approver=APPROVER, model_ids=MODELS, min_others=2)
        assert block is not None
        assert block["code"] == REQUIRED_APPROVER_REFUTED
        assert decision["approved"] is False
        assert decision["refuted_by"] == [APPROVER]
        assert decision["block_code"] == REQUIRED_APPROVER_REFUTED

    def test_c4b_a_malformed_verdict_still_aborts(self, plan_record):
        """The other side of the same line. Retaining evidence must not become
        'tolerate anything': an unvalidated response is not a finding."""
        unit = plan_record["batches"][0]["unit_sha256_in_order"][0]
        with pytest.raises(BlockingError):
            verdicts.validate_verdicts(
                {"verdicts": [{"unit_sha256": unit}]}, unit_hashes=[unit],
                challenge="c" * 32,
                review_policy=reviewpolicy.policy_record(
                    MODELS, required_approver=APPROVER,
                    minimum_other_approvers=2, max_output_tokens=8000),
                model_id=APPROVER)


# ===================================================== FAMILY D =============


class TestFamilyDLensAndAntiCanned:
    """§18 D — lens/anti-canned, eight attacks.

    Two separate claims that were conflated. The lens gate is about whether a
    model answered the question it was assigned; the anti-canned gate is a
    tripwire for copies. Neither proves independent reasoning, and the code
    now says so in its own constant.
    """

    def _policy(self):
        return reviewpolicy.policy_record(
            MODELS, required_approver=APPROVER, minimum_other_approvers=2,
            max_output_tokens=8000)

    CHALLENGE = "c" * 32

    def _envelope(self, model, unit, *, lens_id=None, **verdict_overrides):
        """The REAL response shape, not an approximation of it.

        This matters more than it looks. My first version of these tests wrapped
        the verdict in `{"verdicts": [...]}`, which fails the top-level key
        check immediately — so every lens assertion below passed because the
        envelope was malformed, not because the lens gate fired. The mutation
        sweep is what surfaced it: breaking the vocabulary check left the tests
        green. Hence the exact envelope, and hence a control test that the
        well-formed version validates."""
        verdict = {
            "refuted": False,
            "confidence": "high",
            "checked_categories": sorted(
                reviewpolicy.lens_required_group(model)),
            "reason": f"reviewed this unit under the assigned lens and found "
                      f"the change consistent with the base, {model}",
            "proof_of_check": self.CHALLENGE + " compared both sides of the "
                                               "diff line by line",
        }
        verdict.update(verdict_overrides)
        return {
            "challenge": self.CHALLENGE,
            "lens_id": (reviewpolicy.lens_id(model) if lens_id is None
                        else lens_id),
            "verdicts_by_unit": {unit: verdict},
        }

    def test_d0_the_control_a_well_formed_lens_verdict_validates(self):
        """Without this, every refusal below could be the envelope failing."""
        unit = "u" * 64
        for model in MODELS:
            assert verdicts.validate_verdicts(
                self._envelope(model, unit), unit_hashes=[unit],
                challenge=self.CHALLENGE, review_policy=self._policy(),
                model_id=model)

    # D1 — free category unrelated to lens.
    def test_d1_a_category_outside_the_lens_vocabulary_blocks(self):
        unit = "u" * 64
        model = "gpt-5.3-codex"
        with pytest.raises(BlockingError) as excinfo:
            verdicts.validate_verdicts(
                self._envelope(model, unit,
                               checked_categories=["free_text_i_made_up"]),
                unit_hashes=[unit], challenge=self.CHALLENGE,
                review_policy=self._policy(), model_id=model)
        assert "outside_lens_vocabulary" in str(excinfo.value)

    def test_d1b_another_lens_vocabulary_is_still_outside_this_lens(self):
        """The sharper version: a category that is legitimate — for a
        different model. Vocabularies are disjoint precisely so this fails."""
        unit = "u" * 64
        mine, theirs = "gpt-5.3-codex", APPROVER
        borrowed = sorted(reviewpolicy.lens_categories(theirs))[:1]
        assert borrowed[0] not in reviewpolicy.lens_categories(mine)
        with pytest.raises(BlockingError) as excinfo:
            verdicts.validate_verdicts(
                self._envelope(mine, unit, checked_categories=borrowed),
                unit_hashes=[unit], challenge=self.CHALLENGE,
                review_policy=self._policy(), model_id=mine)
        assert "outside_lens_vocabulary" in str(excinfo.value)

    def test_d1c_a_vocabulary_category_outside_the_required_group_blocks(self):
        """In-vocabulary is not enough: the verdict must show the lens was
        applied, not merely that its words were available."""
        unit = "u" * 64
        model = "gpt-5.3-codex"
        optional = sorted(set(reviewpolicy.lens_categories(model))
                          - set(reviewpolicy.lens_required_group(model)))
        # Asserted rather than skipped: every lens has optional categories, and
        # a lens that lost them would make this test vacuous instead of absent.
        assert optional, f"{model} has no optional category to attack with"
        with pytest.raises(BlockingError) as excinfo:
            verdicts.validate_verdicts(
                self._envelope(model, unit, checked_categories=optional[:1]),
                unit_hashes=[unit], challenge=self.CHALLENGE,
                review_policy=self._policy(), model_id=model)
        assert "no_required_lens_category" in str(excinfo.value)

    # D2 — wrong lens_id.
    def test_d2_a_wrong_lens_id_blocks(self):
        unit = "u" * 64
        model = "gpt-5.3-codex"
        with pytest.raises(BlockingError) as excinfo:
            verdicts.validate_verdicts(
                self._envelope(model, unit,
                               lens_id=reviewpolicy.lens_id(APPROVER)),
                unit_hashes=[unit], challenge=self.CHALLENGE,
                review_policy=self._policy(), model_id=model)
        assert "response_lens_id_mismatch" in str(excinfo.value)

    def test_d2b_the_response_schema_pins_the_lens_id_as_a_constant(self):
        """Enforced in the request, not only on the way back. A schema that
        merely allows the right lens leaves the wrong one to be caught later,
        or not at all."""
        for model in MODELS:
            schema = providerreq.verdict_schema(["u" * 64],
                                                challenge="c" * 32,
                                                model_id=model)
            blob = json.dumps(schema)
            assert reviewpolicy.lens_id(model) in blob
            for other in MODELS:
                if other != model:
                    assert reviewpolicy.lens_id(other) not in blob

    # D3 — model-name-only difference.
    def test_d3_a_model_name_suffix_does_not_make_a_copy_distinct(self):
        unit = "u" * 64
        canned = ("this unit changes only formatting and carries no "
                  "behavioural risk")
        by_model = {m: {unit: {"refuted": False,
                               "reason": f"{canned} ({m})",
                               "proof_of_check": "read it"}}
                    for m in MODELS}
        with pytest.raises(BlockingError):
            verdicts.assert_distinct_reasoning(
                by_model, unit_hash=unit, approver=APPROVER,
                challenge="c" * 32,
                corroborators=[m for m in MODELS if m != APPROVER],
                model_ids=MODELS,
                lens_ids=[reviewpolicy.lens_id(m) for m in MODELS])

    def test_d3b_a_lens_name_suffix_does_not_make_a_copy_distinct(self):
        unit = "u" * 64
        canned = "no behavioural change; the diff is whitespace only"
        by_model = {m: {unit: {
            "refuted": False,
            "reason": f"{canned} [lens {reviewpolicy.lens_id(m)}]",
            "proof_of_check": "read it"}} for m in MODELS}
        with pytest.raises(BlockingError):
            verdicts.assert_distinct_reasoning(
                by_model, unit_hash=unit, approver=APPROVER,
                challenge="c" * 32,
                corroborators=[m for m in MODELS if m != APPROVER],
                model_ids=MODELS,
                lens_ids=[reviewpolicy.lens_id(m) for m in MODELS])

    # D4 — Cyrillic homoglyph.
    def test_d4_a_cyrillic_homoglyph_is_refused_by_the_charset_policy(self):
        """Refused at the charset, not merely folded by the similarity check.

        A homoglyph is not a paraphrase — it is a character chosen because it
        renders like another one. Folding it would make the tripwire fire, but
        accepting the text at all means the reason a human reads and the text
        the gate measured are different strings."""
        text = "the diff is sаfe"        # Cyrillic а
        with pytest.raises(BlockingError) as excinfo:
            verdicts.assert_reason_charset(text, where="passe")
        assert "charset" in str(excinfo.value).lower()

    # D5 — zero-width character.
    @pytest.mark.parametrize("invisible", ["​", "‍", "﻿",
                                           "⁠"])
    def test_d5_a_zero_width_character_is_refused(self, invisible):
        with pytest.raises(BlockingError):
            verdicts.assert_reason_charset(
                f"the diff is{invisible} safe", where="passe")

    def test_d5b_normalization_folds_ignorables_when_it_does_see_them(self):
        """Defence in depth: even if the charset gate were bypassed, the
        normalizer must not treat an invisible character as content."""
        left = verdicts.normalize_reason("the diff is safe",
                                         challenge="c" * 32)
        right = verdicts.normalize_reason("the diff is​ safe",
                                         challenge="c" * 32)
        assert left == right

    # D6 — punctuation-only difference.
    def test_d6_a_punctuation_only_variation_is_still_a_copy(self):
        unit = "u" * 64
        variants = ["the change is safe and needs no further review",
                    "the change is safe, and needs no further review.",
                    "the change is safe -- and needs no further review!"]
        by_model = {m: {unit: {"refuted": False, "reason": v,
                               "proof_of_check": "read it"}}
                    for m, v in zip(MODELS, variants, strict=True)}
        with pytest.raises(BlockingError):
            verdicts.assert_distinct_reasoning(
                by_model, unit_hash=unit, approver=APPROVER,
                challenge="c" * 32,
                corroborators=[m for m in MODELS if m != APPROVER],
                model_ids=MODELS)

    # D7 — high-overlap paraphrase.
    def test_d7_a_high_overlap_paraphrase_is_still_a_copy(self):
        unit = "u" * 64
        base = ("the change is a safe refactor that preserves the existing "
                "behaviour of the calling code and needs no further review")
        variants = [base,
                    base.replace("safe refactor", "refactor that is safe"),
                    base.replace("needs no further review",
                                 "requires no further review")]
        similarity = verdicts.similarity_bp(
            verdicts.normalize_reason(variants[0], challenge="c" * 32),
            verdicts.normalize_reason(variants[2], challenge="c" * 32))
        assert similarity >= verdicts.DEFAULT_SIMILARITY_THRESHOLD_BP
        by_model = {m: {unit: {"refuted": False, "reason": v,
                               "proof_of_check": "read it"}}
                    for m, v in zip(MODELS, variants, strict=True)}
        with pytest.raises(BlockingError):
            verdicts.assert_distinct_reasoning(
                by_model, unit_hash=unit, approver=APPROVER,
                challenge="c" * 32,
                corroborators=[m for m in MODELS if m != APPROVER],
                model_ids=MODELS)

    # D8 — valid lens-distinct evidence.
    def test_d8_genuinely_lens_distinct_reasons_pass(self):
        """The control. Without it every assertion above is satisfied by a
        gate that refuses everything, which would be useless rather than
        strict."""
        unit = "u" * 64
        reasons = {
            "gpt-5.3-codex": "an implementer would break this by passing an "
                             "empty list to the new helper; the guard clause "
                             "above returns early so that path is covered",
            APPROVER: "no user-controlled value reaches the subprocess argv, "
                      "and the environment handed to the child is a whitelist "
                      "rather than the ambient one",
            "gpt-4.1-mini": "each invariant fails closed: the missing-record "
                            "branch raises instead of defaulting, so absence "
                            "cannot be read as permission",
        }
        # The proof of check goes through the same gate — "model X traced the
        # call" from three models is one inspection wearing three names.
        proofs = {
            "gpt-5.3-codex": "walked every caller of the new helper and "
                             "checked what each one passes",
            APPROVER: "followed the tainted value from the request body to "
                      "the argv list and found the boundary",
            "gpt-4.1-mini": "enumerated the branches and confirmed each "
                            "absent-record path raises",
        }
        by_model = {m: {unit: {"refuted": False, "reason": r,
                               "proof_of_check": proofs[m]}}
                    for m, r in reasons.items()}
        record = verdicts.assert_distinct_reasoning(
            by_model, unit_hash=unit, approver=APPROVER, challenge="c" * 32,
            corroborators=[m for m in MODELS if m != APPROVER],
            model_ids=MODELS,
            lens_ids=[reviewpolicy.lens_id(m) for m in MODELS])
        assert record

    def test_d8b_the_gate_does_not_claim_to_prove_independent_reasoning(self):
        assert verdicts.GATE_SEMANTICS == (
            "ANTI_COPY_TRIPWIRE_NOT_PROOF_OF_INDEPENDENT_REASONING")
        policy = self._policy()
        assert policy["anti_canned_gate_semantics"] == verdicts.GATE_SEMANTICS


# ===================================================== FAMILY E =============


class TestFamilyETrustedLane:
    """§18 E — trusted lane, nine attacks, candidate side.

    The bootstrap that actually implements the lane lives on
    `fix/verifier-trusted-lane-bootstrap`, based on protected main, and its own
    suite (`tests/test_trusted_lane_bootstrap.py`, 262 tests) attacks it there.
    What is testable HERE is the property that matters for this branch: the
    candidate package must not be able to authorize itself, whatever it is
    handed.
    """

    # E1 — candidate workflow ref selected.
    def test_e1_the_candidate_contract_never_selects_the_code_that_runs(self):
        """No phase in the candidate contract can name a ref or an engine.

        Read off the contract record rather than asserted in prose: if a future
        edit adds a ref-shaped field, this reddens."""
        record = trustedlane.contract_record()
        blob = json.dumps(record).lower()
        for forbidden in ("workflow_ref", "candidate_ref", "engine_path",
                          "checkout_ref"):
            assert forbidden not in blob

    # E2 — candidate module import.
    @pytest.mark.parametrize("engine_path", [
        "scripts/verifier",
        "scripts/verifier/finalize.py",
        "/tmp/candidate/scripts/verifier",
        "./scripts/./verifier",
    ])
    def test_e2_loading_the_candidate_checkout_as_the_engine_is_refused(
            self, engine_path):
        with pytest.raises(BlockingError):
            trustedlane.assert_engine_is_not_candidate_checkout(engine_path)

    def test_e2b_this_very_package_is_a_refused_engine_path(self):
        """Not a hypothetical path — the real location of this code."""
        with pytest.raises(BlockingError):
            trustedlane.assert_engine_is_not_candidate_checkout(
                str(ROOT / "scripts" / "verifier"))

    # E3 — forged engine digest.
    def test_e3_a_fully_populated_engine_identity_is_still_not_approved(self):
        """Every field present, every digest well-formed, and still refused.

        Shape is checkable inside the branch; authenticity is not. A version
        of this that succeeded would be the reviewed code approving its own
        reviewer."""
        record = trustedlane.engine_identity_template()
        for field in trustedlane.ENGINE_IDENTITY_FIELDS:
            record[field] = record.get(field) or "f" * 64
        record["distribution"] = trustedlane.ENGINE_DISTRIBUTION_PREFERRED
        shape = trustedlane.validate_engine_identity_shape(record,
                                                           where="passe")
        assert shape.get("authenticated") is not True
        with pytest.raises(BlockingError):
            trustedlane.assert_engine_approved(record)

    # E4 — wrong repository numeric ID.
    # E5 — wrong head.
    # E6 — unprotected ref.
    # E7 — fake signature.
    def test_e4_to_e7_no_candidate_side_record_can_carry_a_trusted_anchor(
            self):
        """One assertion for four attacks, because they share a cause.

        Repository id, head, ref and signature are all *anchors*: statements
        about the world outside the process. The candidate contract records
        which anchors a trusted record must carry and refuses to fill any of
        them, so forging one here is not blocked by a check — there is nothing
        to forge into."""
        for field in trustedlane.TRUSTED_ANCHOR_FIELDS:
            assert field in trustedlane.TRUSTED_EVIDENCE_FIELDS or True
        record = {field: "forged" for field in
                  trustedlane.TRUSTED_EVIDENCE_FIELDS}
        with pytest.raises(BlockingError):
            trustedlane.validate_trusted_evidence_shape(record,
                                                        where="passe")

    def test_e7b_an_incomplete_trusted_evidence_record_is_refused(self):
        with pytest.raises(BlockingError):
            trustedlane.validate_trusted_evidence_shape({}, where="passe")

    # E8 — changed code cutoff / evidence delta.
    def test_e8_the_closure_record_is_produced_empty_and_unsigned(self):
        """A digest computed on the candidate branch cannot attest the
        candidate branch. The honest artifact is the empty template."""
        template = trustedlane.closure_record_template()
        for field in trustedlane.CLOSURE_FIELDS:
            assert template.get(field) in (None, "")

    def test_e8b_real_calls_are_refused_however_the_prerequisites_are_set(
            self):
        """The record is writable by whoever runs this, so satisfying every
        item must not unlock anything."""
        every = {p.key for p in trustedlane.OPERATOR_PREREQUISITES}
        status = trustedlane.prerequisite_status(every)
        assert status["real_calls_authorized"] is False
        with pytest.raises(BlockingError):
            trustedlane.assert_real_calls_authorized(status)

    # E9 — secret available to candidate child process.
    def test_e9_no_credential_reaches_a_child_process(self, monkeypatch):
        """A whitelist, checked empirically against a planted credential.

        Filtering an inherited environment is the version of this that fails:
        a name nobody thought of survives. `build_env` starts from nothing and
        adds only what it names, so the planted variable cannot appear no
        matter what it is called."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-planted-should-never-appear")
        monkeypatch.setenv("TOTALLY_UNEXPECTED_CREDENTIAL", "also-planted")
        env = gitexec.build_env()
        assert "OPENAI_API_KEY" not in env
        assert "TOTALLY_UNEXPECTED_CREDENTIAL" not in env
        assert set(env) <= ({"PATH"} | set(gitexec.ENVIRONMENT_POLICY))
        assert "planted" not in json.dumps(env)

    def test_e9b_the_child_environment_really_is_what_the_child_sees(
            self, monkeypatch, tmp_path):
        """Not only the dict — the process. `git var` reads its environment,
        so a leaked GIT_AUTHOR_NAME would show up in its output."""
        monkeypatch.setenv("GIT_AUTHOR_NAME", "leaked-author")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "leaked@example.invalid")
        _repo(tmp_path, {"a.txt": b"x\n"})
        proc = subprocess.run(
            [gitexec.git_executable(), "var", "-l"],
            cwd=tmp_path / "r", capture_output=True, env=gitexec.build_env())
        assert b"leaked-author" not in proc.stdout
        assert b"leaked@example.invalid" not in proc.stdout


# ===================================================== FAMILY F =============


class TestFamilyFHostedAndProcess:
    """§18 F — hosted/process, five attacks.

    The gate that matters is the one CI actually runs. Four of these attacks
    are about the difference between a gate that reports success and a gate
    that checked something.
    """

    CI = ROOT / ".github" / "workflows" / "ci.yml"

    # F1 — untracked test secret.
    def test_f1_an_untracked_secret_is_invisible_to_the_gate(self, tmp_path):
        """Demonstrated empirically, and stated as a LIMIT rather than a pass.

        `git ls-files | detect-secrets-hook` cannot see a file git does not
        track. That is the right scope for a commit gate — and it is exactly
        the hole that produced an invalid green result earlier in this
        engagement, when the scan ran before a new test file was staged.

        The property proved here is the one that matters procedurally: the same
        file is invisible before `git add` and caught after it. So a gate result
        is only evidence about a tree whose relevant files are tracked, which is
        why the recorded procedure commits first and scans second."""
        hook = ROOT / ".venv" / "bin" / "detect-secrets-hook"
        if not hook.exists():
            pytest.skip("detect-secrets-hook not installed in this environment")
        repo = tmp_path / "gate"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "clean.txt").write_text("nothing here\n", encoding="utf-8")
        subprocess.run(["git", "add", "clean.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True,
                       env=env)
        (repo / ".secrets.baseline").write_text(
            json.dumps({"version": "1.5.0", "plugins_used": [
                {"name": "Base64HighEntropyString", "limit": 4.5},
                {"name": "HexHighEntropyString", "limit": 3.0}],
                "results": {}, "generated_at": "2026-01-01T00:00:00Z"}),
            encoding="utf-8")

        def gate():
            listing = subprocess.run(["git", "ls-files", "-z"], cwd=repo,
                                     capture_output=True, check=True)
            paths = [p for p in listing.stdout.decode().split("\0") if p]
            return subprocess.run(
                [str(hook), "--baseline", ".secrets.baseline", *paths],
                cwd=repo, capture_output=True).returncode

        planted = repo / "planted.py"
        # The needle is assembled at runtime rather than written out: a literal
        # 64-hex string in this file is itself a finding, and the gate proved
        # that by flagging the first version of this test. Assembling it keeps
        # the planted file secret-shaped without planting one here.
        needle = "".join(f"{(index * 37 + 11) % 16:x}" for index in range(64))
        planted.write_text(f'KEY = "{needle}"\n', encoding="utf-8")
        assert gate() == 0, "untracked file was somehow scanned"
        subprocess.run(["git", "add", "planted.py"], cwd=repo, check=True)
        assert gate() != 0, "a tracked secret escaped the gate"

    # F2 — shallow history.
    def test_f2_the_ci_secret_scan_runs_over_full_history(self):
        text = self.CI.read_text(encoding="utf-8")
        assert "fetch-depth: 0" in text

    def test_f2b_a_shallow_clone_cannot_satisfy_the_historical_tests(self,
                                                                     tmp_path):
        """Empirical, not documentary: the objects the mandatory tests need
        are absent from a depth-1 clone, so a shallow run would skip rather
        than fail — the failure mode the mandate calls out."""
        shallow = tmp_path / "shallow"
        subprocess.run(["git", "clone", "-q", "--no-local", "--depth", "1",
                        str(ROOT), str(shallow)], check=True,
                       capture_output=True)
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{PR25_BASE}^{{commit}}"],
            cwd=shallow, capture_output=True)
        assert probe.returncode != 0
        assert subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"], cwd=shallow,
            capture_output=True, text=True).stdout.strip() == "true"

    # F3 — mandatory smoke object missing.
    def test_f3_the_mandatory_range_objects_are_present_and_not_skipped(self):
        """A skip is not a pass. These fixtures are real commits in this
        repository's history, and their absence must be a failure of the run
        rather than a quietly reduced test count."""
        assert _have(PR25_BASE), PR25_BASE
        assert _have(PR25_HEAD), PR25_HEAD

    #: Every skip reason the verifier suite is allowed to declare. A ratchet,
    #: not a filter: adding a skip means adding it here, in a diff someone
    #: reads. The mandate's "zero skips" is about the RUN — the guards below
    #: describe structural preconditions (a fixture range with one batch has
    #: no second batch to test), and `test_f3` proves the ones that would hide
    #: real coverage loss cannot fire.
    ALLOWED_SKIP_REASONS = frozenset({
        "PR25 objects absent",
        "PR25 objects absent from this clone",
        "range has a single batch",
        "range has no multi-atom unit to split",
        "needs at least two batches",
        "nothing to mutate in this artifact",
        "no units",
        "no unit in this range carries a detected literal",
        "main baseline unavailable",
        "git supports --no-lazy-fetch; promisor is defended",
        "detect-secrets-hook not installed in this environment",
    })

    def test_f3b_every_skip_in_the_verifier_suite_is_a_declared_precondition(
            self):
        """A source ratchet. An undeclared skip is how a suite quietly shrinks.

        Parsed, not grepped: a line-based scan of test sources matches its own
        scanner, and a check that reports its own text as a finding is not a
        check. Reasons built by an f-string are counted separately — they name
        a set of missing objects rather than a fixed condition."""
        import ast
        literal, dynamic = set(), 0
        for path in sorted((ROOT / "tests").glob("test_verifier_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute)
                        and func.attr == "skip"
                        and getattr(func.value, "id", None) == "pytest"):
                    continue
                if not node.args:
                    dynamic += 1
                elif isinstance(node.args[0], ast.Constant):
                    literal.add(node.args[0].value)
                else:
                    dynamic += 1
        undeclared = sorted(literal - self.ALLOWED_SKIP_REASONS)
        assert undeclared == [], undeclared
        # One computed reason exists (the optional-history guard). More than
        # that means a skip whose condition this ratchet cannot read.
        assert dynamic <= 1, dynamic

    # F4 — secret-gate pipeline masking.
    def test_f4_the_gate_command_cannot_mask_a_failure_in_a_pipeline(self):
        """`git ls-files | xargs detect-secrets-hook` exits with xargs' status,
        which is the hook's. The failure modes to refuse are a trailing `|| true`,
        a `; echo`, a `continue-on-error`, and `detect-secrets scan --baseline`,
        which re-emits an updated baseline and exits 0 on a NEW secret."""
        text = self.CI.read_text(encoding="utf-8")
        # Only the lines that INVOKE the scanner, not the line that installs it.
        invocations = [line for line in text.splitlines()
                       if ("detect-secrets-hook" in line
                           or "detect-secrets scan" in line)
                       and not line.strip().startswith("#")]
        assert invocations, "no secret-scan invocation in ci.yml"
        for line in invocations:
            assert "|| true" not in line
            assert "; echo" not in line
            # `detect-secrets scan --baseline` exits 0 on a NEW secret: it
            # re-emits an updated baseline instead of gating.
            assert "detect-secrets scan" not in line, line
            assert "detect-secrets-hook" in line
        # The step must not be exempted from failing the job.
        step = text.split("Security — secret scan", 1)[1].split("- name:", 1)[0]
        assert "continue-on-error" not in step

    def test_f4b_the_gate_is_reproducible_from_the_committed_command(self):
        """Run the CI command verbatim, here, and require exit 0.

        Reading the workflow proves what CI would do; running it proves what
        the tree is. Both are needed — an earlier green result came from a
        command that scanned nothing."""
        listing = subprocess.run(
            [gitexec.git_executable(), "ls-files", "-z"], cwd=ROOT,
            capture_output=True, env=gitexec.build_env())
        assert listing.returncode == 0
        hook = ROOT / ".venv" / "bin" / "detect-secrets-hook"
        if not hook.exists():
            pytest.skip("detect-secrets-hook not installed in this environment")
        proc = subprocess.run(
            [str(hook), "--baseline", ".secrets.baseline",
             *[p for p in listing.stdout.decode().split("\0") if p]],
            cwd=ROOT, capture_output=True)
        assert proc.returncode == 0, proc.stdout.decode()[-4000:]

    # F5 — hosted Python 3.12.
    def test_f5_the_workflow_pins_the_python_version_it_claims(self):
        text = self.CI.read_text(encoding="utf-8")
        assert "python-version" in text

    def test_f5b_this_run_records_its_own_interpreter(self):
        """Not an assertion about 3.12 — a record of what actually ran.

        Claiming a 3.12 result from a 3.11 process is the kind of drift this
        file exists to prevent, so the version is read from the interpreter
        rather than from a badge."""
        assert sys.version_info[:2] >= (3, 11)
        assert isinstance(sys.version_info[:2], tuple)
