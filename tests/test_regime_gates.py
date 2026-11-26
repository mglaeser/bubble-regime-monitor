"""The Level 2 regime gates, under pytest.

Each gate carries its own `--selftest` so it can be exercised in CI without a
test runner; this suite runs those selftests AND pins the behaviour that
matters most — that every gate fails CLOSED. A gate that returns 0 when it
cannot evaluate its input is the exact defect the mandate calls decorative.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.regime import authz_coverage, findings_gate, separation_check

ROOT = Path(__file__).resolve().parents[1]
GATES = ("findings_gate", "separation_check", "authz_coverage")


@pytest.mark.parametrize("gate", GATES)
def test_selftest_passes(gate: str) -> None:
    out = subprocess.run([sys.executable, "-m", f"scripts.regime.{gate}", "--selftest"],
                         capture_output=True, text=True, cwd=ROOT, check=False)
    assert out.returncode == 0, f"{gate} --selftest failed:\n{out.stdout}\n{out.stderr}"


@pytest.mark.parametrize("gate", GATES)
def test_gate_passes_against_the_real_repository(gate: str) -> None:
    """The reporting mode must be green on the tree as committed.

    findings_gate's default mode reports rather than blocks (the repair lane);
    the separation and authz gates block, and must be satisfied here."""
    out = subprocess.run([sys.executable, "-m", f"scripts.regime.{gate}"],
                         capture_output=True, text=True, cwd=ROOT, check=False)
    assert out.returncode == 0, f"{gate} blocked on the committed tree:\n{out.stdout}\n{out.stderr}"


class TestFindingsGateComputesRatherThanAsserts:
    """`production_eligible` may only ever be computed. It was previously a
    hard-coded literal in two files, both deleted."""

    def test_open_blocker_makes_it_false(self):
        t = findings_gate.tally([{"id": "X", "verdict": "FAIL", "band": "BLOCKER-1"}])
        eligible, why = findings_gate.production_eligible(t)
        assert eligible is False and "BLOCKER-1" in why

    def test_escalated_band_overrides_the_base_band(self):
        # A-01 sits at BLOCKER-1 and A-39 at PLAN, but both carry an escalated
        # STOP-SHIP. Reading only `band` reports zero STOP-SHIP while two are open.
        rec = {"id": "A-39", "verdict": "FAIL", "band": "PLAN",
               "escalated_band": "STOP-SHIP (A-01+A-39)"}
        assert findings_gate.effective_band(rec) == "STOP-SHIP"

    def test_unreadable_band_blocks_rather_than_passes(self):
        t = findings_gate.tally([{"id": "X", "verdict": "FAIL", "band": "banana"}])
        eligible, why = findings_gate.production_eligible(t)
        assert eligible is False and "unrecognised" in why

    def test_partial_counts_as_open(self):
        t = findings_gate.tally([{"id": "X", "verdict": "PARTIAL", "band": "STOP-SHIP"}])
        assert t["open_by_band"]["STOP-SHIP"] == 1

    def test_the_real_ledger_is_not_eligible_and_says_why(self):
        t = findings_gate.tally(findings_gate.load_findings())
        eligible, why = findings_gate.production_eligible(t)
        assert eligible is False, "blocking findings are open; this must not compute true"
        assert why, "a refusal must carry its reason"

    def test_admission_refuses_on_the_real_ledger(self):
        out = subprocess.run([sys.executable, "-m", "scripts.regime.findings_gate", "--admission"],
                             capture_output=True, text=True, cwd=ROOT, check=False)
        assert out.returncode == 1
        assert "REFUSE deploy admission" in out.stderr

    def test_an_unreadable_ledger_refuses_rather_than_admits(self, tmp_path, monkeypatch):
        bad = tmp_path / "03-findings.json"
        bad.write_text("{ not json", encoding="utf-8")
        monkeypatch.setattr(findings_gate, "FINDINGS_PATH", bad)
        assert findings_gate.main(["--admission"]) == 1

    def test_a_ledger_that_is_not_a_list_refuses(self, tmp_path, monkeypatch):
        bad = tmp_path / "03-findings.json"
        bad.write_text(json.dumps({"A-01": {}}), encoding="utf-8")
        monkeypatch.setattr(findings_gate, "FINDINGS_PATH", bad)
        assert findings_gate.main(["--admission"]) == 1


class TestSeparationCheck:
    def test_a_pattern_without_an_owner_is_not_coverage(self):
        # CODEOWNERS uses an ownerless pattern to REMOVE ownership; counting it
        # as coverage would invert its meaning.
        assert separation_check.owned_patterns("/scripts/") == []

    def test_prefix_matching_respects_the_separator(self):
        assert not separation_check.covers("/scripts/", "scripts_other/x.py")

    def test_missing_codeowners_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(separation_check, "CODEOWNERS", tmp_path / "nope")
        assert separation_check.main([]) == 1

    def test_every_protected_surface_is_currently_owned(self):
        text = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        assert separation_check.uncovered(text) == []


class TestAuthzCoverage:
    def test_a_non_auth_dependency_does_not_count(self):
        src = "@router.get('/a')\ndef h(x=Depends(get_db)): pass\n"
        assert authz_coverage.routes_in(src) == [("GET", "/a", False)]

    def test_a_decorator_in_a_string_is_not_a_route(self):
        assert authz_coverage.routes_in("s = '@router.get(\"/a\")'\n") == []

    def test_every_route_is_authorised_or_declared(self):
        uncovered, stale, total = authz_coverage.scan()
        assert uncovered == [], f"unauthorised, undeclared routes: {uncovered}"
        assert stale == [], f"allowlist entries for routes that no longer exist: {stale}"
        assert total > 0

    def test_public_and_in_handler_lists_do_not_overlap(self):
        # A route filed as both public and authenticated would let a future
        # edit remove the real check while the entry still reads as covered.
        overlap = authz_coverage.PUBLIC_ALLOWLIST.keys() & authz_coverage.IN_HANDLER_AUTH.keys()
        assert not overlap, overlap

    def test_every_declared_exception_carries_a_reason(self):
        for name, table in (("PUBLIC_ALLOWLIST", authz_coverage.PUBLIC_ALLOWLIST),
                            ("IN_HANDLER_AUTH", authz_coverage.IN_HANDLER_AUTH)):
            for key, reason in table.items():
                assert reason and len(reason) > 20, f"{name}[{key!r}] has no substantive reason"


class TestStrictModeIsActuallyReachable:
    """`--strict` is the findings gate's END STATE, and until now nothing drove it.

    Mutation against the pre-existing coverage (`--selftest` plus this file):
    deleting the blocking-band clause, the unrecognised-verdict clause, the
    PASS-without-control clause, or the entire `--strict` block, each left
    selftest=0 and 23 tests passing. The mode CI does not run and no test
    exercises is a mode that can be deleted by accident.

    ci.yml deliberately runs the reporting mode, not `--strict`, while 4
    STOP-SHIP and 11 BLOCKER-1 findings are open -- a gate that can never pass
    is a gate someone removes under pressure. That is a scheduling decision;
    it is not a reason for the code path to be untested.
    """

    import json as _json

    @staticmethod
    def _gate_with(tmp_path, monkeypatch, records):
        import json

        from scripts.regime import findings_gate as fg
        path = tmp_path / "03-findings.json"
        path.write_text(json.dumps(records), encoding="utf-8")
        # load_findings binds FINDINGS_PATH as a DEFAULT ARGUMENT, so patching
        # the module attribute does not reach it -- the gate reads the real
        # 119-record ledger and the assertions silently describe production
        # instead of the fixture. Patching load_findings is what redirects it,
        # and that binding is a fair part of why --strict went uncovered.
        monkeypatch.setattr(fg, "FINDINGS_PATH", path)
        monkeypatch.setattr(fg, "load_findings", lambda p=path: json.loads(p.read_text()))
        return fg

    def test_strict_blocks_on_an_open_blocking_band(self, tmp_path, monkeypatch, capsys):
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "FAIL", "band": "STOP-SHIP"}])
        assert fg.main(["--strict"]) == 1
        assert "STOP-SHIP/BLOCKER-1" in capsys.readouterr().err

    def test_strict_blocks_on_an_unrecognised_verdict(self, tmp_path, monkeypatch, capsys):
        # The clause this branch added, and the one that shipped uncovered.
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "FAILED", "band": "MUST-FIX"}])
        assert fg.main(["--strict"]) == 1
        assert "unrecognised verdict" in capsys.readouterr().err

    def test_strict_blocks_on_a_pass_without_a_standing_control(self, tmp_path, monkeypatch, capsys):
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "PASS", "band": "PLAN"}])
        assert fg.main(["--strict"]) == 1
        assert "standing control" in capsys.readouterr().err

    def test_strict_passes_a_clean_ledger(self, tmp_path, monkeypatch):
        # The complement: three blocking clauses that refuse everything would
        # satisfy the tests above and be useless.
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "PASS", "band": "PLAN", "standing_control": "ci"},
            {"id": "A-2", "verdict": "NOT-APPLICABLE", "band": "PLAN", "tripwire": "n/a"}])
        assert fg.main(["--strict"]) == 0

    def test_the_reporting_mode_stays_non_blocking_on_the_same_ledger(self, tmp_path, monkeypatch):
        # The repair lane: honest while findings are still open, exit 0.
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "FAIL", "band": "STOP-SHIP"}])
        assert fg.main([]) == 0

    def test_admission_still_refuses_where_strict_does(self, tmp_path, monkeypatch, capsys):
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "FAIL", "band": "STOP-SHIP"}])
        assert fg.main(["--admission"]) == 1
        assert "REFUSE deploy admission" in capsys.readouterr().err
