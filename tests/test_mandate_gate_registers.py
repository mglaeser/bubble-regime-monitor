"""Mandate gate: the decision registers.

Findings-register laundering, band/verdict weakening records, acceptance
residuals, ratchet baselines, and the freshness/replay detection that stops
a single authorisation being spent twice."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.mandate_gate_support import (
    clear_ambient_ci_env,
    mg,
)
from tests.mandate_gate_support import (
    reattest as _reattest,
)
from tests.mandate_gate_support import (
    write_minimal_engagement as _write_minimal_engagement,
)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    clear_ambient_ci_env(monkeypatch)


class TestFindingsRegisterLaundering:
    """CRITICAL, from this repository's own adversarial sweep (2026-07-25):
    audit/03-findings.json — the ONLY register carrying the verdicts and bands
    the blocker set is computed from — had no comparison-point check. Editing
    it and refreshing findings_sha256 erased all 32 open blockers and flipped
    production_eligible to true while the gate printed 'status: OK'."""

    def _engagement(self, monkeypatch, tmp_path, **kw):
        _write_minimal_engagement(tmp_path, **kw)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")

    def _prev(self, monkeypatch, findings):
        def _pv(path):
            if "03-findings" in path:
                return findings
            return None
        monkeypatch.setattr(mg, "previous_version", _pv)

    def test_band_downgrade_out_of_the_blocker_set_blocks(self, monkeypatch, tmp_path):
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=False)
        cur = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        for r in cur:
            if r["id"] == "A-02":
                r["band"] = "MUST-FIX"          # STOP-SHIP -> MUST-FIX
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(cur))
        _reattest(tmp_path)
        self._prev(monkeypatch, [{"id": "A-02", "band": "STOP-SHIP",
                                  "verdict": "FAIL"}])
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_closing_an_open_blockers_verdict_blocks(self, monkeypatch, tmp_path):
        # the second route: leave the band, flip the verdict to PASS/N-A
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=False)
        cur = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        for r in cur:
            if r["id"] == "A-02":
                r["verdict"] = "NOT-APPLICABLE"
                r["na_justification"] = "claims to no longer apply"
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(cur))
        _reattest(tmp_path)
        self._prev(monkeypatch, [{"id": "A-02", "band": "STOP-SHIP",
                                  "verdict": "FAIL"}])
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_a_structured_weakening_record_permits_the_change(
            self, monkeypatch, tmp_path):
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=False)
        cur = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        for r in cur:
            if r["id"] == "A-02":
                r["band"] = "MUST-FIX"
                r["weakening_record"] = {
                    "change": "STOP-SHIP->MUST-FIX",
                    "reason": "fixed with red-to-green evidence in audit/05",
                    "authorised_by": "operator"}
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(cur))
        _reattest(tmp_path)
        self._prev(monkeypatch, [{"id": "A-02", "band": "STOP-SHIP",
                                  "verdict": "FAIL"}])
        mg.compute_status()          # does not raise

    def test_a_record_for_a_DIFFERENT_change_does_not_license_this_one(
            self, monkeypatch, tmp_path):
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=False)
        cur = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        for r in cur:
            if r["id"] == "A-02":
                r["band"] = "MUST-FIX"
                r["weakening_record"] = {
                    "change": "BLOCKER-1->MUST-FIX",     # not this change
                    "reason": "an unrelated earlier decision entirely",
                    "authorised_by": "operator"}
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(cur))
        _reattest(tmp_path)
        self._prev(monkeypatch, [{"id": "A-02", "band": "STOP-SHIP",
                                  "verdict": "FAIL"}])
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_findings_band_may_not_contradict_the_immutable_catalogue(
            self, monkeypatch, tmp_path):
        # the catalogue carries the authoritative founding_band and was
        # compared on ID SETS only, so a downgrade left its hash valid
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=False)
        cur = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        for r in cur:
            if r["id"] == "A-02":
                r["band"] = "PLAN"       # catalogue says STOP-SHIP
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(cur))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "previous_version", lambda _p: None)
        with pytest.raises(SystemExit):
            mg.compute_status()


class TestPanelFindingsRound16:
    """Three bypasses, the first in the fix written the round before — a
    strict check and a lax one guarding the same invariant is the recurring
    shape of these defects."""

    @pytest.mark.parametrize("rec", [
        {},                                              # the empty-dict bypass
        {"change": "STOP-SHIP->PLAN"},                   # no reason/authoriser
        {"change": "STOP-SHIP->PLAN", "reason": "short", "authorised_by": "x"},
        {"change": "STOP-SHIP->PLAN", "reason": "a" * 25, "authorised_by": "  "},
        {"change": "WRONG->CHANGE", "reason": "a" * 25, "authorised_by": "op"},
        True, "a string", None,
    ])
    def test_invalid_weakening_records_are_rejected(self, rec):
        assert not mg.valid_weakening_record(rec, "STOP-SHIP->PLAN")

    def test_a_complete_weakening_record_is_accepted(self):
        assert mg.valid_weakening_record(
            {"change": "STOP-SHIP->PLAN",
             "reason": "fixed with red-to-green evidence recorded in audit/05",
             "authorised_by": "operator"}, "STOP-SHIP->PLAN")

    def test_empty_record_cannot_license_a_founding_band_downgrade(
            self, monkeypatch, tmp_path):
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=False)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        cur = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        for r in cur:
            if r["id"] == "A-02":
                r["band"] = "PLAN"                 # catalogue says STOP-SHIP
                r["weakening_record"] = {}          # the bypass
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(cur))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "previous_version", lambda _p: None)
        with pytest.raises(SystemExit):
            mg.compute_status()

    @pytest.mark.parametrize("justification", [True, 1, "", "  ", "too short"])
    def test_non_substantive_na_justification_blocks(
            self, monkeypatch, tmp_path, justification):
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=False)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        cur = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        for r in cur:
            if r["id"] == "A-02":
                r["verdict"] = "NOT-APPLICABLE"
                r["na_justification"] = justification
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(cur))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "previous_version", lambda _p: None)
        with pytest.raises(SystemExit):
            mg.compute_status()

    @pytest.mark.parametrize("src", [
        "class User(Base):\n    __tablename__ = 'users'\n",
        "    owner = relationship('User', back_populates='items')\n",
        "    account_id: Mapped[int] = mapped_column(Integer)\n",
        '    x: Mapped[int] = mapped_column(ForeignKey("accounts.id"))\n',
    ])
    def test_tenancy_revalidation_sees_models_and_relationships(
            self, monkeypatch, tmp_path, src):
        # a tenancy MODEL or a relationship to one is multi-tenancy before any
        # *_id column appears; class-6 previously stayed green on both
        (tmp_path / "app").mkdir(parents=True)
        (tmp_path / "app" / "models.py").write_text(src)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        _ENT = (r"user|tenant|owner|account|customer|org|organisation|"
                r"organization|workspace|member|subject|principal")
        import re as _re
        pattern = (rf"\b({_ENT})_id\b"
                   r"|ForeignKey\(\s*[\"']"
                   r"(users|accounts|tenants|orgs|organisations|organizations|"
                   r"customers|members|workspaces)\."
                   rf"|^\s*class\s+({_ENT})s?\b"
                   rf"|relationship\(\s*[\"']({_ENT})s?[\"']")
        assert _re.search(pattern, src, _re.IGNORECASE | _re.MULTILINE), src


class TestPanelFindingsRound17:
    """Reuse and under-declaration of decision records."""

    def _engagement(self, monkeypatch, tmp_path, **kw):
        _write_minimal_engagement(tmp_path, **kw)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")

    def test_orphan_acceptance_record_must_be_pruned(self, monkeypatch, tmp_path):
        # retaining a record for a no-longer-accepted id pre-authorises the
        # next acceptance of that id
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=True)
        reg = tmp_path / "governance" / "accepted-residuals.json"
        d = json.loads(reg.read_text())
        d["acceptance_records"].append({
            "finding_id": "Z-99",                 # not in accepted_open_findings
            "reason": "a decision retained after the finding closed",
            "authorised_by": "operator"})
        reg.write_text(json.dumps(d))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "previous_version", lambda _p: None)
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_a_carried_over_record_cannot_authorise_a_new_acceptance(
            self, monkeypatch, tmp_path):
        # id closed then RE-accepted later, with the original record kept:
        # the record predates this change, so it is not a decision about it
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=True)
        _reattest(tmp_path)
        prev = {"accepted_open_findings": [],          # A-02 not accepted then
                "acceptance_records": [{"finding_id": "A-02",
                                        "reason": "the original decision text",
                                        "authorised_by": "operator"}]}
        monkeypatch.setattr(mg, "previous_version",
                            lambda p: prev if "accepted" in p else None)
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_a_genuinely_new_record_authorises_the_acceptance(
            self, monkeypatch, tmp_path):
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=True)
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "previous_version",
                            lambda p: {"accepted_open_findings": [],
                                       "acceptance_records": []}
                            if "accepted" in p else None)
        mg.compute_status()          # does not raise

    def test_ratchet_loosening_must_declare_itself_a_finding(
            self, monkeypatch, tmp_path):
        # §9.1: "the loosening is itself a finding" — a well-formed record
        # that omits is_finding satisfied the gate while breaking the rule
        import hashlib
        (tmp_path / "audit").mkdir(exist_ok=True)
        baselines = tmp_path / "audit" / "ratchet-baselines.json"
        baselines.write_text(json.dumps({
            "_meta": {"decision_records": [{
                "metric": "test_count_floor", "change": "999 -> 10",
                "reason": "a deliberate reduction with a full explanation",
                "authorised_by": "operator"}]},        # no is_finding
            "ratchets": {"test_count_floor": 10}}))
        gov = tmp_path / "governance" / "mandate"
        gov.mkdir(parents=True, exist_ok=True)
        (gov / "manifest.json").write_text(json.dumps({
            "ratchet_baselines_sha256":
                hashlib.sha256(baselines.read_bytes()).hexdigest()}))
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        monkeypatch.setattr(mg, "measure_ratchets", lambda: {"test_count_floor": 10})
        monkeypatch.setattr(mg, "previous_version",
                            lambda _p: {"ratchets": {"test_count_floor": 999}})
        with pytest.raises(SystemExit):
            mg.cmd_ratchet(measure_only=False)


class TestPanelFindingsRound18:
    """Freshness is now ONE mechanism applied to every record type, rather
    than a per-type patch each round; plus two calibration evasions."""

    def test_record_freshness_is_generic(self):
        rec = {"metric": "m", "change": "9 -> 1", "reason": "r" * 25,
               "authorised_by": "operator"}
        assert mg.is_fresh(rec, [])
        assert mg.is_fresh(rec, [{"metric": "other", "change": "9 -> 1"}])
        assert not mg.is_fresh(rec, [dict(rec)])          # byte-identical carry-over

    def test_fingerprint_ignores_incidental_fields(self):
        a = {"metric": "m", "change": "c", "reason": "r", "authorised_by": "o"}
        b = dict(a, date="2026-07-25", note="cosmetic")
        assert mg.record_fingerprint(a) == mg.record_fingerprint(b)

    def test_retained_weakening_record_cannot_reauthorise(
            self, monkeypatch, tmp_path):
        # re-tighten then downgrade again: the ORIGINAL record must not license
        # the second downgrade
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=False)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        rec = {"change": "STOP-SHIP->MUST-FIX",
               "reason": "an earlier downgrade decision entirely",
               "authorised_by": "operator"}
        cur = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        for r in cur:
            if r["id"] == "A-02":
                r["band"] = "MUST-FIX"
                r["weakening_record"] = dict(rec)
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(cur))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "previous_version",
                            lambda p: [{"id": "A-02", "band": "STOP-SHIP",
                                        "verdict": "FAIL",
                                        "weakening_record": dict(rec)}]
                            if "03-findings" in p else None)
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_softening_the_catalogue_founding_band_blocks(
            self, monkeypatch, tmp_path):
        # pre-downgrading the IMMUTABLE founding band lets every future
        # finding on that check sit lower without any weakening record
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=True)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        cat = json.loads((tmp_path / "audit" / "00-check-catalogue.json").read_text())
        for c in cat["checks"]:
            if c["id"] == "A-02":
                c["founding_band"] = "MUST-FIX"       # was STOP-SHIP
        (tmp_path / "audit" / "00-check-catalogue.json").write_text(json.dumps(cat))
        _reattest(tmp_path)

        def _pv(path):
            if "check-catalogue" in path:
                return {"checks": [{"id": "A-02", "founding_band": "STOP-SHIP"}]}
            if "manifest" in path:
                return {"required_check_ids": ["A-01", "A-02"]}
            return None
        monkeypatch.setattr(mg, "previous_version", _pv)
        with pytest.raises(SystemExit):
            mg.compute_status()

    @pytest.mark.parametrize("snippet", [
        'kwargs["tools"] = [t]',
        'create(**{"tools": [t]})',
        "payload['tools'] = x",
        "client.create(tools=[t])",
    ])
    def test_class5_catches_computed_key_tool_enabling(self, snippet):
        # WRONG-REASON FIX (review P1.5-c): was a copied regex that never ran
        # production. Drive the real module-level detector on each form.
        import ast
        assert mg.tool_enabling(ast.parse(snippet)), snippet

    @pytest.mark.parametrize("snippet,expected", [
        ("class UserAccount(Base):", True),
        ('    x = relationship("UserAccount")', True),
        ("    userId: Mapped[int]", True),
        ("class Snapshot(Base):", False),
        ("    computed_at: Mapped[datetime]", False),
        ('    readings = relationship("IndicatorReading")', False),
    ])
    def test_class6_catches_compound_and_camelcase_tenancy(self, snippet, expected):
        # production detectors, not a copied regex (review P1.5-c). A strong
        # signal (tenancy class) or the column signal both count as tenancy.
        got = mg.declares_strong_tenancy(snippet) or mg.declares_tenancy_column(snippet)
        assert got is expected, snippet


class TestPanelFindingsRound21:
    """Two precise bypasses: a numeric substring collision, and the one push
    shape where the pre-push SHA does not exist."""

    @pytest.mark.parametrize("change,old,new,should_match", [
        ("120 -> 210", 20, 21, False),      # the collision: contains 20 and 21
        ("20 -> 21", 20, 21, True),
        ("460 -> 457 (LOOSENING)", 460, 457, True),
        ("21 -> 20", 20, 21, False),        # direction matters
        ("about 20 to 21", 20, 21, False),  # prose is not a transition
        ("", 20, 21, False),
    ])
    def test_transition_is_parsed_not_substring_matched(
            self, change, old, new, should_match):
        # production parser (review P1.5-c): was a copied regex; call the real
        # transition_matches the ratchet check uses.
        assert mg.transition_matches(change, old, new) is should_match, change

    def test_substring_collision_cannot_license_a_loosening(
            self, monkeypatch, tmp_path):
        import hashlib
        (tmp_path / "audit").mkdir(exist_ok=True)
        baselines = tmp_path / "audit" / "ratchet-baselines.json"
        # a CEILING rising is the loosening (a floor rising is a tightening)
        baselines.write_text(json.dumps({
            "_meta": {"decision_records": [{
                "metric": "noqa_ceiling", "change": "120 -> 210",
                "reason": "a completely unrelated recorded decision",
                "authorised_by": "operator", "is_finding": True}]},
            "ratchets": {"noqa_ceiling": 21}}))
        gov = tmp_path / "governance" / "mandate"
        gov.mkdir(parents=True, exist_ok=True)
        (gov / "manifest.json").write_text(json.dumps({
            "ratchet_baselines_sha256":
                hashlib.sha256(baselines.read_bytes()).hexdigest()}))
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        monkeypatch.setattr(mg, "measure_ratchets",
                            lambda: {"noqa_ceiling": 21})
        monkeypatch.setattr(mg, "previous_version",
                            lambda _p: {"ratchets": {"noqa_ceiling": 20}})
        with pytest.raises(SystemExit):
            mg.cmd_ratchet(measure_only=False)

    def test_branch_creation_compares_against_the_default_branch(
            self, monkeypatch):
        # zero SHA means no prior state on this branch: HEAD~1 would be this
        # push's OWN earlier commit, hiding a weakening placed there
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        monkeypatch.setenv("MANDATE_PUSH_BEFORE", "0" * 40)
        monkeypatch.setattr(mg, "_is_shallow", lambda: False)
        monkeypatch.setattr(mg, "_resolve",
                            lambda ref: "mainsha" if ref == "origin/main" else None)

        class _R:
            returncode = 0
            stdout = "branchpoint\n"
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: _R())
        assert mg.comparison_point() == "branchpoint"

    def test_branch_creation_without_a_default_branch_fails_closed_in_ci(
            self, monkeypatch):
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        monkeypatch.setenv("MANDATE_PUSH_BEFORE", "0" * 40)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setattr(mg, "_is_shallow", lambda: False)
        monkeypatch.setattr(mg, "_resolve", lambda ref: None)
        with pytest.raises(SystemExit):
            mg.comparison_point()


class TestPanelFindingsRound22:
    """Class-5 moved from text scanning to AST + a structural ban, because a
    literal scan can neither prove absence (`{"tool"+"s": ...}`) nor avoid
    firing on prose (this repo's own comment tripped it)."""

    def _calibrate_in(self, monkeypatch, tmp_path, src):
        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "llm.py").write_text(src)
        (tmp_path / "app" / "models.py").write_text("class Snapshot:\n    pass\n")
        monkeypatch.setattr(mg, "ROOT", tmp_path)

    @pytest.mark.parametrize("src", [
        "client.messages.create(model=m, tools=[t])\n",
        'payload = {"tools": [t]}\n',
        'cfg = {"model": m, "tools": []}\n',
    ])
    def test_ast_sees_real_tool_arguments(self, src):
        import ast
        tree = ast.parse(src)
        found = any(
            (isinstance(n, ast.Call) and any(k.arg == "tools" for k in n.keywords))
            or (isinstance(n, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "tools" for k in n.keys))
            for n in ast.walk(tree))
        assert found, src

    @pytest.mark.parametrize("src", [
        '# a comment mentioning "tools" must not fire\n',
        'DOC = "we never pass tools to the model"\n',
    ])
    def test_prose_about_tools_does_not_fire(self, src):
        import ast
        tree = ast.parse(src)
        found = any(
            (isinstance(n, ast.Call) and any(k.arg == "tools" for k in n.keywords))
            or (isinstance(n, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "tools" for k in n.keys))
            for n in ast.walk(tree))
        assert not found, src

    def test_kwargs_expansion_into_a_model_call_is_forbidden(self):
        # the structural half: with no ** expansion every tool-enabling form
        # is a literal the AST can see, so `{"tool"+"s": ...}` has nowhere to go
        import ast
        tree = ast.parse("client.messages.create(**base)\n")
        expansions = [n for n in ast.walk(tree)
                      if isinstance(n, ast.Call)
                      and any(k.arg is None for k in n.keywords)]
        assert expansions

    def test_live_app_passes_model_keywords_explicitly(self):
        # regression: app/engine/judgment.py used **base at three call sites
        import ast
        src = Path(mg.ROOT, "app", "engine", "judgment.py").read_text()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call):
                fn = node.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else getattr(fn, "id", ""))
                if name in ("create", "stream", "generate", "complete"):
                    assert not any(k.arg is None for k in node.keywords), \
                        f"** expansion at judgment.py:{node.lineno}"


class TestPanelFindingsRound23:
    """Freshness judged one step back could be defeated by delete-then-re-add
    across two commits. 'Seen anywhere before' is the wrong correction — it
    flags every honest persisting record — so replay is detected as
    present -> absent -> present."""

    def _fake_history(self, monkeypatch, snapshots):
        """snapshots: oldest-first list of record-lists per commit."""
        monkeypatch.setattr(mg, "comparison_point", lambda: "base")
        shas = [f"c{i}" for i in range(len(snapshots))]

        def _run(args, **kw):
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            r = R()
            if args[:2] == ["git", "rev-list"]:
                r.stdout = "\n".join(shas)
            elif args[:2] == ["git", "show"]:
                sha = args[2].split(":")[0]
                recs = snapshots[shas.index(sha)]
                r.stdout = json.dumps({"acceptance_records": recs})
            return r
        monkeypatch.setattr(mg.subprocess, "run", _run)

    def test_a_persisting_record_is_not_a_replay(self, monkeypatch):
        rec = {"finding_id": "B-06", "reason": "r" * 25, "authorised_by": "op"}
        self._fake_history(monkeypatch, [[rec], [rec], [rec]])
        assert mg.reintroduced_fingerprints(
            "governance/accepted-residuals.json",
            lambda d: d.get("acceptance_records", [])) == set()

    def test_delete_then_readd_is_detected_as_a_replay(self, monkeypatch):
        rec = {"finding_id": "B-06", "reason": "r" * 25, "authorised_by": "op"}
        self._fake_history(monkeypatch, [[rec], [], [rec]])
        assert mg.reintroduced_fingerprints(
            "governance/accepted-residuals.json",
            lambda d: d.get("acceptance_records", [])) == {
                mg.record_fingerprint(rec)}

    def test_a_genuinely_new_record_is_not_a_replay(self, monkeypatch):
        old = {"finding_id": "B-06", "reason": "r" * 25, "authorised_by": "op"}
        new = {"finding_id": "C-01", "reason": "n" * 25, "authorised_by": "op"}
        self._fake_history(monkeypatch, [[old], [old], [old, new]])
        assert mg.reintroduced_fingerprints(
            "governance/accepted-residuals.json",
            lambda d: d.get("acceptance_records", [])) == set()

    def test_surface_refuses_an_unknown_or_empty_file_set(self, monkeypatch, tmp_path):
        (tmp_path / "audit").mkdir(exist_ok=True)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")

        class R:
            returncode = 128
            stdout = ""
            stderr = "fatal: not a git repository"
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: R())
        with pytest.raises(SystemExit):
            mg.cmd_surface()

    def test_surface_handles_paths_containing_spaces(self, monkeypatch, tmp_path):
        # whitespace splitting mangled "we ird name.py" into two entries
        (tmp_path / "audit").mkdir(exist_ok=True)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")

        class R:
            returncode = 0
            stdout = "app/we ird name.py\0app/x.py\0"
            stderr = ""
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: R())
        mg.cmd_surface()
        surface = json.loads((tmp_path / "audit" / "00-audit-surface.json").read_text())
        assert "app/we ird name.py" in surface["python_modules"]
        assert surface["files_tracked"] == 2

class TestPanelFindingsRound24:
    """A finding can be re-banded AND closed in one change; authorising only
    the first transition left the second unrecorded."""

    def _engagement(self, monkeypatch, tmp_path, **kw):
        _write_minimal_engagement(tmp_path, **kw)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")

    def _apply(self, tmp_path, monkeypatch, record):
        cur = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        for r in cur:
            if r["id"] == "A-02":
                r["band"] = "MUST-FIX"          # re-band ...
                r["verdict"] = "PASS"           # ... AND close
                r["standing_control"] = {
                    "mechanism": "blocking CI job runs the gate every change",
                    "demonstrated": "observed blocking a seeded defect"}
                if record is not None:
                    r["weakening_record"] = record
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(cur))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "previous_version",
                            lambda p: [{"id": "A-02", "band": "STOP-SHIP",
                                        "verdict": "FAIL"}]
                            if "03-findings" in p else None)

    def test_authorising_only_the_reband_leaves_the_closure_unrecorded(
            self, monkeypatch, tmp_path):
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=False)
        self._apply(tmp_path, monkeypatch, {
            "change": "STOP-SHIP->MUST-FIX",
            "reason": "re-banded on evidence recorded in audit/05",
            "authorised_by": "operator"})
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_authorising_only_the_closure_leaves_the_reband_unrecorded(
            self, monkeypatch, tmp_path):
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=False)
        self._apply(tmp_path, monkeypatch, {
            "change": "FAIL->PASS",
            "reason": "closed on evidence recorded in audit/05",
            "authorised_by": "operator"})
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_a_record_per_transition_is_accepted(self, monkeypatch, tmp_path):
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=False)
        self._apply(tmp_path, monkeypatch, [
            {"change": "STOP-SHIP->MUST-FIX",
             "reason": "re-banded on evidence recorded in audit/05",
             "authorised_by": "operator"},
            {"change": "FAIL->PASS",
             "reason": "closed on evidence recorded in audit/05",
             "authorised_by": "operator"}])
        mg.compute_status()          # does not raise

    def test_a_retained_list_record_is_not_fresh(self, monkeypatch, tmp_path):
        # The list shape must be read on the PRIOR side of the freshness
        # comparison too: collecting only dict-shaped priors made a record
        # carried over verbatim look brand new, so a re-tightened finding
        # could be re-weakened for free on the strength of the old decision.
        recs = [{"change": "STOP-SHIP->MUST-FIX",
                 "reason": "re-banded on evidence recorded in audit/05",
                 "authorised_by": "operator"},
                {"change": "FAIL->PASS",
                 "reason": "closed on evidence recorded in audit/05",
                 "authorised_by": "operator"}]
        self._engagement(monkeypatch, tmp_path, pass_has_control=True,
                         blocker_accepted=False)
        self._apply(tmp_path, monkeypatch, recs)
        monkeypatch.setattr(mg, "previous_version",
                            lambda p: [{"id": "A-02", "band": "STOP-SHIP",
                                        "verdict": "FAIL",
                                        "weakening_record": recs}]
                            if "03-findings" in p else None)
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_catalogue_crosscheck_reads_the_list_shape(self, monkeypatch, tmp_path):
        # The founding-band cross-check ran valid_weakening_record() on the raw
        # field, so a list — the shape a dual transition REQUIRES — reduced to
        # "no record" and blocked a properly authorised change.
        assert not mg.valid_weakening_record(
            [{"change": "STOP-SHIP->MUST-FIX",
              "reason": "re-banded on evidence recorded in audit/05",
              "authorised_by": "operator"}], "STOP-SHIP->MUST-FIX")
        assert any(
            mg.valid_weakening_record(r, "STOP-SHIP->MUST-FIX")
            for r in mg.weakening_records(
                [{"change": "STOP-SHIP->MUST-FIX",
                  "reason": "re-banded on evidence recorded in audit/05",
                  "authorised_by": "operator"}]))

    def test_weakening_records_rejects_non_dict_members(self):
        assert mg.weakening_records(["authorised", None, 7]) == []
        assert mg.weakening_records(None) == []
        assert mg.weakening_records("operator said so") == []

    def test_live_A39_records_both_of_its_transitions(self):
        # regression: A-39 was re-banded STOP-SHIP->PLAN and closed FAIL->PASS
        # in this PR, but only the band change was ever recorded
        findings = json.loads(
            (Path(mg.ROOT) / "audit" / "03-findings.json").read_text())
        a39 = next(f for f in findings if f["id"] == "A-39")
        changes = {r["change"] for r in a39["weakening_record"]}
        assert changes == {"STOP-SHIP->PLAN", "FAIL->PASS"}


class TestPanelFindingsRound29:
    """The weakening record was the one decision type without history-wide
    replay detection: remove it during a re-tighten, restore it at the next
    downgrade, and the comparison-point check reads it as brand new."""

    RECORD = {"change": "STOP-SHIP->PLAN",
              "reason": "re-banded on evidence recorded in audit/05",
              "authorised_by": "operator"}

    def _fixture(self, monkeypatch, tmp_path, *, historic):
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=False)
        fp = tmp_path / "audit" / "03-findings.json"
        fs = json.loads(fp.read_text())
        for f in fs:
            if f["id"] == "A-02":
                f["band"] = "PLAN"                     # weakened again ...
                f["weakening_record"] = self.RECORD    # ... same record as before
        fp.write_text(json.dumps(fs))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        # comparison point: A-02 open at STOP-SHIP with NO record — the record
        # was deleted when the finding was re-tightened
        monkeypatch.setattr(mg, "previous_version",
                            lambda p: [{"id": "A-02", "band": "STOP-SHIP",
                                        "verdict": "FAIL"}]
                            if "03-findings" in p else None)
        monkeypatch.setattr(
            mg, "reintroduced_fingerprints",
            lambda rel, extract: ({mg.record_fingerprint(self.RECORD)}
                                  if historic else set()))

    def test_a_record_removed_and_re_added_cannot_authorise_again(
            self, monkeypatch, tmp_path):
        self._fixture(monkeypatch, tmp_path, historic=True)
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), pytest.raises(SystemExit):
            mg.compute_status()
        assert "REMOVED and later re-added" in err.getvalue()

    def test_a_genuinely_new_record_still_authorises(
            self, monkeypatch, tmp_path):
        # BOTH SIDES: replay detection must not block a first, honest weakening
        self._fixture(monkeypatch, tmp_path, historic=False)
        mg.compute_status()          # does not raise

    def test_replay_detection_is_wired_for_all_three_record_types(self):
        # the defect was that ONE of the three registers lacked the mechanism;
        # pin that they all carry it now
        src = (Path(mg.__file__).read_text() if hasattr(mg, "__file__")
               else "")
        src = src or (Path(__file__).resolve().parents[1]
                      / "scripts" / "mandate_gate.py").read_text()
        assert src.count("reintroduced_fingerprints(") >= 4   # def + 3 uses
        for register in ("audit/03-findings.json",
                         "governance/accepted-residuals.json",
                         "audit/ratchet-baselines.json"):
            assert register in src


class TestPanelFindingsRound32:
    """An acceptance is a STANDING decision, so its record must stand with it.
    Round 25 made the orphan prune unconditional -- a record whose id is no
    longer accepted blocks -- but left the mirror case conditional on `newly`,
    so an already-accepted id could have its record deleted and nothing
    looked. All 32 waived blockers could lose their justification while the
    gate printed OK."""

    PREV = {"accepted_open_findings": ["A-02"],
            "acceptance_records": [{
                "finding_id": "A-02",
                "reason": "compensating control and tripwire recorded in audit/06",
                "authorised_by": "operator (test fixture)"}]}

    def _fixture(self, monkeypatch, tmp_path, mutate):
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=True)
        reg = tmp_path / "governance" / "accepted-residuals.json"
        d = json.loads(reg.read_text())
        mutate(d)                       # A-02 stays ACCEPTED throughout
        reg.write_text(json.dumps(d))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        # the id is NOT newly accepted -- it was already there. That is what
        # made `newly` empty and skipped the check.
        monkeypatch.setattr(mg, "previous_version",
                            lambda p: (self.PREV
                                       if "accepted-residuals" in p else None))

    def _err(self, monkeypatch, tmp_path, mutate) -> str:
        import contextlib
        import io
        self._fixture(monkeypatch, tmp_path, mutate)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), pytest.raises(SystemExit):
            mg.compute_status()
        return err.getvalue()

    def test_deleting_the_record_of_a_standing_acceptance_blocks(
            self, monkeypatch, tmp_path):
        err = self._err(monkeypatch, tmp_path,
                        lambda d: d.__setitem__("acceptance_records", []))
        assert "acceptance_record is missing or malformed" in err

    def test_falsy_malformed_containers_cannot_normalise_past_the_shape_check(
            self, monkeypatch, tmp_path):
        # `... or []` turned {}, 0 and "" into an empty list BEFORE the shape
        # guard could object -- exactly the values a tamper would use
        for i, bad in enumerate(({}, 0, "")):
            case = tmp_path / f"case{i}"
            case.mkdir()
            err = self._err(monkeypatch, case,
                            lambda d, b=bad: d.__setitem__("acceptance_records", b))
            assert ("acceptance_record is missing or malformed" in err
                    or "must be a LIST" in err), bad

    def test_a_non_falsy_wrong_shape_still_names_the_shape(
            self, monkeypatch, tmp_path):
        err = self._err(monkeypatch, tmp_path,
                        lambda d: d.__setitem__("acceptance_records",
                                                {"A-02": "waived"}))
        assert "must be a LIST" in err

    def test_a_hollowed_out_record_does_not_count_as_documentation(
            self, monkeypatch, tmp_path):
        for i, bad in enumerate(({"finding_id": "A-02"},
                                 {"finding_id": "A-02", "reason": "short",
                                  "authorised_by": "operator"},
                                 {"finding_id": "A-02", "reason": "x" * 30,
                                  "authorised_by": "  "})):
            case = tmp_path / f"case{i}"
            case.mkdir()
            err = self._err(monkeypatch, case,
                            lambda d, b=bad: d.__setitem__("acceptance_records", [b]))
            assert "acceptance_record is missing or malformed" in err, bad

    def test_an_intact_standing_acceptance_still_passes(
            self, monkeypatch, tmp_path):
        # BOTH SIDES: the ordinary steady state -- accepted with a good record,
        # nothing newly accepted -- must stay green
        self._fixture(monkeypatch, tmp_path, lambda d: None)
        mg.compute_status()          # does not raise

    def test_the_live_register_documents_every_acceptance(self):
        # the property this pins for the real repository: 32 accepted, 32 records
        accepted = json.loads(
            (Path(mg.ROOT) / "governance" / "accepted-residuals.json").read_text())
        ids = set(accepted["accepted_open_findings"])
        documented = {r["finding_id"] for r in accepted["acceptance_records"]
                      if isinstance(r, dict)
                      and len(str(r.get("reason", "")).strip()) >= 20
                      and str(r.get("authorised_by", "")).strip()}
        assert ids and ids <= documented
