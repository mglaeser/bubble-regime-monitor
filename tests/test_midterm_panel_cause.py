"""The panel says WHY, on both paths.

Two defects, one per path, both in the lane and neither in the engine.

BLOCKED. The engine decides every unit and records `unit_decisions` and
`unit_blocks` — which unit, which model vetoed, how many corroborated, and for
the anti-copy gate the similarity score against its threshold. `panel.execute`
returned eight keys and neither of those was among them, so a blocked run
published `decision: blocked`, a `votes` COUNT, and a stderr line naming the
GATE. Nothing said which of ninety-nine units failed.

REFUSED. A refusal inside `execute_batch` unwinds past every write in
`panelcli.perform`. Both panel runs on this pull request ended that way: three
real generation calls spent, `panel-evidence.json` never written, its upload
step red for a missing file. The adapter had already built a structural record
per attempt — http status, response bytes, requested and returned model — and
it is read only from `execute`'s return value, which a refusal never produces.

The privacy question is answered field by field rather than asserted: every
value retained here is engine- or lane-authored and structural. The
write-separated D2 lane carries the same two structures and declines the block
`reason`, saying it "can quote the payload"; that judgement is right about the
field type and over-broad for the four messages reachable here, so the reason
is carried and DEFENDED — bounded, control characters refused, scanned — and
degrades to the code and category if a future engine ever changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PANEL_WORKFLOW = ROOT / ".github" / "workflows" / "midterm-panel-review.yml"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from midtermpanel import (  # noqa: E402
    PANEL_EVIDENCE_CLASS,
    PANEL_REFUSAL_EVIDENCE_CLASS,
    panel,
    panelcli,
)
from midtermpanel.errors import PanelRefusal  # noqa: E402
from trustedlane.errors import LaneRefusal  # noqa: E402

UNIT_A = "a" * 64
UNIT_B = "b" * 64


def _batch(batch_id="batch-0000", *, decisions=None, blocks=None):
    return {"batch_id": batch_id,
            "unit_decisions": decisions or {},
            "unit_blocks": blocks or []}


def _decision(**overrides):
    record = {"unit_sha256": UNIT_A, "approved": False,
              "approver_confidence": "high",
              "refuted_by": ["gpt-5.6-sol"],
              "distinct_other_approvers": 1,
              "distinct_reasoning": {"unit_sha256": UNIT_A,
                                     "distinct_reasoning_count": 1},
              "block_code": "REQUIRED_APPROVER_REFUTED"}
    record.update(overrides)
    return record


# ------------------------------------------- the blocked path: per unit -----


class TestTheBlockedPathSaysWhichUnitAndWhichModel:

    def test_decisions_and_blocks_are_flattened_and_keyed_by_unit(self):
        """A reader looks up the unit the merge gate named, rather than
        searching eight batch objects for it."""
        got = panel.per_unit_evidence([
            _batch("batch-0000", decisions={UNIT_A: _decision()}),
            _batch("batch-0001",
                   decisions={UNIT_B: _decision(unit_sha256=UNIT_B,
                                                approved=True)}),
        ])

        assert set(got["unit_decisions"]) == {UNIT_A, UNIT_B}
        assert got["unit_decisions"][UNIT_A]["batch_id"] == "batch-0000"
        assert got["unit_decisions"][UNIT_B]["batch_id"] == "batch-0001"

    def test_the_anti_copy_numbers_survive_because_they_are_the_finding(self):
        """`similarity_bp=9100 threshold_bp=8500` is precisely the number an
        operator acts on. Dropping it to honour a caution written about a
        different message would be cargo-culting the caution."""
        reason = ("category=canned_identical_approval unit=aaaaaaaaaaaaaaaa "
                  "model=gpt-5.6-sol matches=gpt-4.1-mini similarity_bp=9100 "
                  "threshold_bp=8500")
        got = panel.per_unit_evidence([_batch(blocks=[
            {"unit_sha256": UNIT_A, "code": "PROVIDER_RESPONSE_INVALID",
             "reason": reason}])])

        block = got["unit_blocks"][0]
        assert block["category"] == "canned_identical_approval"
        assert "similarity_bp=9100" in block["reason"]
        assert "threshold_bp=8500" in block["reason"]
        assert block["batch_id"] == "batch-0000"

    def test_the_decision_fields_are_an_allowlist_not_a_copy(self):
        """A future engine that adds a prose field must not start publishing
        it by existing. Named fields, so growth is a decision."""
        got = panel.per_unit_evidence([_batch(decisions={
            UNIT_A: _decision(model_wrote="a sentence the provider chose")})])

        retained = got["unit_decisions"][UNIT_A]
        assert "model_wrote" not in retained
        assert set(retained) == set(panel.DECISION_FIELDS) | {"batch_id"}

    @pytest.mark.parametrize("reason,why", [
        ("category=x_y " + "z" * panel.MAX_BLOCK_REASON_CHARS, "oversized"),
        ("category=x_y \x07 bell", "control character"),
        (None, "absent"),
        (12345, "not a string"),
        ("", "empty"),
    ])
    def test_a_reason_that_fails_the_checks_is_withheld_not_dropped(
            self, reason, why):
        """The block still lands, with its unit and its code. A reason that
        cannot be shown is not a reason to hide the block — that would let a
        malformed message erase the finding it describes."""
        got = panel.per_unit_evidence([_batch(blocks=[
            {"unit_sha256": UNIT_A, "code": "SOME_CODE", "reason": reason}])])

        block = got["unit_blocks"][0]
        assert block["unit_sha256"] == UNIT_A, why
        assert block["code"] == "SOME_CODE", why
        assert block["reason"] == panel.BLOCK_REASON_WITHHELD, why

    def test_a_reason_the_engine_scanner_dislikes_is_withheld(self):
        """The defence against an engine that one day puts something in this
        field that is not structural. Uses the scanner, not a guess."""
        seen = {}

        def scan(text):
            seen["text"] = text
            return [{"category": "openai_key"}]

        got = panel.per_unit_evidence(
            [_batch(blocks=[{"unit_sha256": UNIT_A, "code": "C",
                             "reason": "category=x_y something"}])],
            scan=scan)

        assert seen["text"] == "category=x_y something"
        assert got["unit_blocks"][0]["reason"] == panel.BLOCK_REASON_WITHHELD
        # ...and the category still survives, because it is not the risky part.
        assert got["unit_blocks"][0]["category"] == "x_y"

    def test_the_category_rule_is_the_lanes_one_rule_not_a_second_copy(self):
        """`engine_category_of` must delegate. Two copies of an anchored
        identifier regex is how the two come to disagree."""
        from trustedlane import enginebridge

        assert "engine_category" in (
            panel.engine_category_of.__code__.co_names)
        for message, expected in (
                ("category=required_approver_veto approver=x",
                 "required_approver_veto"),
                ("refused: category=not_at_the_start", None),
                ("category=has/a/path", None),
                (None, None)):
            assert panel.engine_category_of(message) == expected
            carrier = type("C", (), {"message": message})()
            assert enginebridge.engine_category(carrier) == expected


class TestNoProviderProseReachesTheEvidence:
    """The privacy claim, checked rather than asserted.

    Every value retained is engine- or lane-authored: hashes, a bool, a
    confidence enum, governed model ids, integers, and `distinct_reasoning`,
    which is itself only counts and model ids."""

    PROSE = "the handler silently swallows the timeout and returns success"

    def test_a_models_reason_cannot_arrive_through_the_decision_fields(self):
        got = panel.per_unit_evidence([_batch(decisions={
            UNIT_A: _decision(reason=self.PROSE, proof_of_check=self.PROSE,
                              distinct_reasoning={"unit_sha256": UNIT_A,
                                                  "reason": self.PROSE})})])

        serialized = json.dumps(got["unit_decisions"])
        assert self.PROSE not in serialized, (
            "an allowlist that copies a nested dict wholesale is not an "
            "allowlist")

    def test_the_nested_distinct_reasoning_is_the_one_hole_to_watch(self):
        """`distinct_reasoning` is retained whole because the engine's own
        return is `{unit_sha256, gate_semantics, similarity_threshold_bp,
        distinct_reasoning_models, distinct_reasoning_count}` — verified field
        by field. This test is the alarm if that ever stops being true."""
        import inspect

        from verifier import verdicts as engine_verdicts

        source = inspect.getsource(engine_verdicts.assert_distinct_reasoning)
        tail = source[source.rindex("return {"):]
        for prose_field in ("reason", "proof", "text", "message"):
            assert f'"{prose_field}"' not in tail, (
                f"assert_distinct_reasoning now returns {prose_field!r}; "
                "`distinct_reasoning` is copied whole into the evidence and "
                "must stop being, or that field must be excluded")


# ------------------------------------------- the refused path: no verdict ---


class TestARefusedRunStillSaysWhatItKnew:

    def _refusal(self):
        return PanelRefusal(
            "MIDTERM_PANEL_REFUSED",
            "category=trusted_lane_refusal trusted_code=TRUSTED_LANE_REFUSED "
            "trusted_reason=category=engine_refused where=execute_batch "
            "code=PROVIDER_RESPONSE_INVALID")

    def _environ(self, tmp_path):
        (tmp_path / "midterm").mkdir(parents=True, exist_ok=True)
        return {"RUNNER_TEMP": str(tmp_path)}

    def _retain(self, tmp_path, *, refusal=None, transport=None):
        return panelcli.retain_refusal_evidence(
            self._environ(tmp_path), refusal=refusal or self._refusal(),
            plan={"plan_sha256": "p" * 64, "batches": [{}, {}],
                  "final_units": [{}, {}, {}]},
            head="h" * 40, base="b" * 40, engine_digest="e" * 64,
            policy_digest="d" * 64, run_id=7, run_attempt=1,
            count_evidence_sha256="c" * 64, transport=transport)

    def test_it_writes_a_record_naming_what_the_run_reached(self, tmp_path):
        record = self._retain(tmp_path)
        body = record["body"]

        assert body["outcome"] == "REFUSED_BEFORE_A_VERDICT"
        assert body["batches_planned"] == 2
        assert body["units_planned"] == 3
        assert "code=PROVIDER_RESPONSE_INVALID" in body["refusal_reason"]
        assert Path(panelcli.refusal_evidence_path(str(tmp_path))).exists()

    def test_it_is_not_a_verdict_and_cannot_be_read_as_one(self, tmp_path):
        """Its own class and its own file. A loader expecting a verdict must
        refuse it rather than find a decision in a run that reached none."""
        record = self._retain(tmp_path)

        assert record["evidence_class"] == PANEL_REFUSAL_EVIDENCE_CLASS
        assert record["evidence_class"] != PANEL_EVIDENCE_CLASS
        assert "decision" not in record["body"]
        assert "no reader may treat an absent refutation as an approval" in (
            record["body"]["honest_scope"])
        assert panelcli.refusal_evidence_path("/t") != (
            "/t/midterm/panel-evidence.json")

    def test_the_transports_own_record_survives_the_refusal(self, tmp_path):
        """The point of the whole change. These are the fields that would have
        answered the question this pull request spent an afternoon on."""
        class Transport:
            def record(self):
                return {"normalization_records": [
                    {"requested_model": "gpt-4.1-mini", "attempt": 3,
                     "http_status": 200, "raw_response_bytes": 20481,
                     "raw_response_sha256": "f" * 64,
                     "response_model": "gpt-4.1-mini-2025-04-14"}],
                    "normalization_version": 2,
                    "normalization_records_sha256": "n" * 64}

        body = self._retain(tmp_path, transport=Transport())["body"]

        assert body["normalization"]["normalized"] is True
        assert body["normalization"]["normalizations"] == 1
        per_model = body["normalization"]["per_model"][0]
        assert per_model["model"] == "gpt-4.1-mini"
        assert per_model["attempt"] == 3

    def test_a_reason_the_redaction_dislikes_leaves_the_record_standing(
            self, tmp_path):
        """`sanitized_trusted_reason` fails closed on the WHOLE string. The
        record must still be written, saying the reason was not printable —
        losing the file because one field could not be shown is how a
        diagnostic deletes the incident it documents."""
        refusal = PanelRefusal("MIDTERM_PANEL_REFUSED",
                               "category=x Authorization: Bearer something")
        body = self._retain(tmp_path, refusal=refusal)["body"]

        assert body["refusal_reason"] == ""
        assert body["refusal_reason_printable"] is False
        assert body["outcome"] == "REFUSED_BEFORE_A_VERDICT"

    @pytest.mark.parametrize("transport", [
        None,
        type("Broken", (), {"record": lambda self: 1 / 0})(),
        type("Odd", (), {"record": lambda self: "not a dict"})(),
    ])
    def test_it_never_raises_whatever_the_transport_does(self, tmp_path,
                                                         transport):
        """A diagnostic that can replace the exit reason with its own failure
        hides the thing it was written to explain. That happened once on this
        branch already."""
        record = self._retain(tmp_path, transport=transport)
        assert record["body"]["outcome"] == "REFUSED_BEFORE_A_VERDICT"

    def test_it_returns_empty_rather_than_raising_on_an_unwritable_temp(self):
        assert panelcli.retain_refusal_evidence(
            {}, refusal=self._refusal(), plan={}, head="h" * 40,
            base="b" * 40, engine_digest="e" * 64, policy_digest="d" * 64,
            run_id=7, run_attempt=1, count_evidence_sha256=None) == {}


class TestPerformWritesItAndStillFails:
    """Driven through the real `panelcli.perform`."""

    def _run(self, tmp_path, exc):
        from test_midterm_readable_review import _run_perform

        class Transport:
            def record(self):
                return {"normalization_records": [
                    {"requested_model": "gpt-4.1-mini", "attempt": 3,
                     "http_status": 200, "raw_response_bytes": 20481,
                     "raw_response_sha256": "f" * 64,
                     "response_model": "gpt-4.1-mini"}],
                    "normalization_version": 2,
                    "normalization_records_sha256": "n" * 64}

        return _run_perform(tmp_path, decision="blocked",
                            execute_raises=exc, transport=Transport())

    @pytest.mark.parametrize("exc", [
        PanelRefusal("MIDTERM_PANEL_REFUSED", "category=engine_refused x"),
        LaneRefusal("TRUSTED_LANE_REFUSED", "category=engine_refused y"),
    ])
    def test_the_refusal_record_is_written_for_both_refusal_types(
            self, tmp_path, exc):
        outcome = self._run(tmp_path, exc)
        assert outcome["refusal_evidence"].exists()
        body = json.loads(
            outcome["refusal_evidence"].read_text(encoding="utf-8"))["body"]
        assert body["normalization"]["per_model"][0]["attempt"] == 3

    def test_the_run_still_fails_and_writes_no_verdict(self, tmp_path):
        """Not a way to soften a refusal. The process must still exit
        nonzero, and no verdict evidence may appear."""
        outcome = self._run(
            tmp_path,
            PanelRefusal("MIDTERM_PANEL_REFUSED", "category=engine_refused z"))

        assert outcome["raised"] is not None
        assert not outcome["evidence"].exists()
        assert not outcome["markdown"].exists()

    def test_an_ordinary_run_writes_no_refusal_record(self, tmp_path):
        """Otherwise every green run leaves a file named for a failure."""
        from test_midterm_readable_review import _run_perform

        outcome = _run_perform(tmp_path, decision="approved")
        assert not outcome["refusal_evidence"].exists()
        assert outcome["evidence"].exists()


class TestTheEvidenceBodyCarriesTheCause:

    def test_a_blocked_run_records_which_gate_and_which_unit(self, tmp_path):
        from test_midterm_readable_review import _run_perform

        outcome = _run_perform(tmp_path, decision="blocked")
        body = json.loads(
            outcome["evidence"].read_text(encoding="utf-8"))["body"]

        for field in ("engine_gate", "strict_gate", "unit_decisions",
                      "unit_blocks"):
            assert field in body, field
        assert body["decision"] == "blocked"
        # WHICH gate blocked, and by how much — the thing `decision: blocked`
        # beside a vote COUNT could never say. This fixture blocks on the
        # engine gate (its votes carry no `refuted_count`, so the strict gate
        # sees no refuters), and the record says so rather than leaving a
        # reader to infer it.
        assert body["engine_gate"]["block"] is True
        assert body["engine_gate"]["refuted_unit_count"] == 1
        assert body["strict_gate"]["block"] is False
        assert body["strict_gate"]["refuting_models"] == []
        assert body["engine_gate"]["reason"]


class TestTheWorkflowRetainsIt:

    def _document(self):
        import yaml
        with open(PANEL_WORKFLOW, encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    def test_the_refusal_artifact_is_uploaded_and_never_reddens_a_run(self):
        """`ignore`, not `error`: on every ordinary path this file does not
        exist, and a missing diagnostic must not fail a completed run. The
        verdict evidence keeps `error`, because a run that DID reach a verdict
        and produced nothing is a real fault."""
        steps = self._document()["jobs"]["panel"]["steps"]
        by_path = {str(s.get("with", {}).get("path", "")): s
                   for s in steps if s.get("uses", "").startswith(
                       "actions/upload-artifact")}

        refusal = next(s for path, s in by_path.items()
                       if "panel-refusal.json" in path)
        verdict = next(s for path, s in by_path.items()
                       if "panel-evidence.json" in path)

        assert refusal["if"] == "always()"
        assert refusal["with"]["if-no-files-found"] == "ignore"
        assert verdict["with"]["if-no-files-found"] == "error"

    def test_the_two_artifacts_do_not_share_a_name(self):
        """Same name, `overwrite: false`, and the second upload fails."""
        steps = self._document()["jobs"]["panel"]["steps"]
        names = [s["with"]["name"] for s in steps
                 if s.get("uses", "").startswith("actions/upload-artifact")]
        assert len(names) == len(set(names)), names
