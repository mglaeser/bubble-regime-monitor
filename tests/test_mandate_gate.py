"""The mandate gate gates the repo, so its own guards are tested both ways:
each detector must fire on its defect AND stay quiet on legitimate input,
and each fail-closed status invariant must actually refuse (Article I/VI)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "mandate_gate", Path(__file__).resolve().parents[1] / "scripts" / "mandate_gate.py")
mg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mg)


class TestCredentialShapeScanner:
    def test_catches_uuid_token_and_named_assignment(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text(
            'sipgate_token = "4ed251b5-8a9c-4b3e-9f21-7c6d5e4a3b21"\n'  # pragma: allowlist secret
            'api_key = "supersecretvalue123"\n')  # pragma: allowlist secret
        hits = mg.scan_credential_shapes([bad])
        assert len(hits) == 2

    def test_quiet_on_pragma_and_embedded_uuid_url(self, tmp_path):
        ok = tmp_path / "ok.py"
        ok.write_text(
            'token = "4ed251b5-8a9c-4b3e-9f21-7c6d5e4a3b21"  # pragma: allowlist secret\n'
            'URL = "https://img1.example/blobby/go/e5e77e0b-59d1-44d9-ab25-4763ac982e53/x.xls"\n'
            'name = "short"\n')
        assert mg.scan_credential_shapes([ok]) == []


class TestVacuousAssertScanner:
    def test_catches_constant_asserts(self, tmp_path):
        t = tmp_path / "test_x.py"
        t.write_text("def test_a():\n    assert True\n"
                     "def test_b():\n    assert 1\n")
        assert len(mg.find_vacuous_test_asserts([t])) == 2

    def test_quiet_on_real_asserts(self, tmp_path):
        t = tmp_path / "test_y.py"
        t.write_text("def test_a():\n    assert compute() == 3\n"
                     "def test_b():\n    assert x, 'message'\n")
        assert mg.find_vacuous_test_asserts([t]) == []


class TestImportResolution:
    def test_flags_hallucinated_and_passes_real(self):
        assert mg.unresolvable_imports(["reqwests_http"]) == ["reqwests_http"]
        assert mg.unresolvable_imports(["json", "fastapi"]) == []


def _write_minimal_engagement(root: Path, *, pass_has_control: bool,
                              blocker_accepted: bool,
                              structured_control: bool = True,
                              escalated_band: str | None = None):
    import hashlib
    (root / "audit").mkdir()
    gov = root / "governance" / "mandate"
    gov.mkdir(parents=True)
    checks = [{"id": "A-01", "track": "A", "title": "gate", "priority": 9,
               "founding_band": "BLOCKER-1", "status": "active"},
              {"id": "A-02", "track": "A", "title": "suite", "priority": 10,
               "founding_band": "STOP-SHIP", "status": "active"}]
    (root / "audit" / "00-check-catalogue.json").write_text(json.dumps(
        {"catalogue_version": "t", "registered_check_count": 2, "checks": checks}))
    control = None
    if pass_has_control:
        control = ({"mechanism": "ci", "demonstrated": "blocked seeded defect"}
                   if structured_control else "ci")
    a02 = {"id": "A-02", "band": "STOP-SHIP", "priority": 10, "verdict": "FAIL"}
    if escalated_band is not None:
        a02["escalated_band"] = escalated_band
    findings = [
        {"id": "A-01", "band": "BLOCKER-1", "priority": 9, "verdict": "PASS",
         "standing_control": control},
        a02,
    ]
    (root / "audit" / "03-findings.json").write_text(json.dumps(findings))
    (root / "audit" / "ratchet-baselines.json").write_text(
        json.dumps({"ratchets": {}}))
    (root / "governance" / "accepted-residuals.json").write_text(json.dumps(
        {"constitution_state": "IN_FORCE_PROVISIONAL",
         "accepted_open_findings": (["A-02"] if blocker_accepted else [])}))
    (root / "governance" / "constitution.md").write_text("law\n")
    (root / "governance" / "mandate.md").write_text("combined\n")
    (gov / "part1.md").write_text("mandate\n")

    def _sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    (gov / "manifest.json").write_text(json.dumps({
        "part1_sha256": _sha(gov / "part1.md"),
        "constitution_sha256": _sha(root / "governance" / "constitution.md"),
        "combined_mandate_sha256": _sha(root / "governance" / "mandate.md"),
        "ratchet_baselines_sha256": _sha(root / "audit" / "ratchet-baselines.json"),
        "accepted_residuals_sha256": _sha(
            root / "governance" / "accepted-residuals.json")}))


class TestStatusInvariants:
    def _patched(self, monkeypatch, tmp_path, **kw):
        _write_minimal_engagement(tmp_path, **kw)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")

    def test_consistent_engagement_computes(self, monkeypatch, tmp_path):
        self._patched(monkeypatch, tmp_path,
                      pass_has_control=True, blocker_accepted=True)
        status = mg.compute_status()
        assert status["open_stop_ship_count"] == 1
        assert status["production_eligible"] is False   # computed, not asserted

    def test_pass_without_standing_control_refused(self, monkeypatch, tmp_path):
        self._patched(monkeypatch, tmp_path,
                      pass_has_control=False, blocker_accepted=True)
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_unaccepted_open_blocker_refused(self, monkeypatch, tmp_path):
        self._patched(monkeypatch, tmp_path,
                      pass_has_control=True, blocker_accepted=False)
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_tampered_constitution_refused(self, monkeypatch, tmp_path):
        self._patched(monkeypatch, tmp_path,
                      pass_has_control=True, blocker_accepted=True)
        (tmp_path / "governance" / "constitution.md").write_text("weakened law\n")
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_unstructured_pass_control_refused(self, monkeypatch, tmp_path):
        # 2026-07-25 re-audit finding 3: a one-word string satisfied the old
        # truthiness check; §5 demands mechanism + demonstrated.
        self._patched(monkeypatch, tmp_path, pass_has_control=True,
                      blocker_accepted=True, structured_control=False)
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_freetext_escalated_band_fails_closed(self, monkeypatch, tmp_path):
        # 2026-07-25 re-audit finding 1: 'STOP-SHIP (A-01+A-39 both fail)'
        # matched no band constant and the record silently ESCAPED the
        # blocker gate. A parseable prefix must band normally; garbage must
        # fail the build, never skip the record.
        self._patched(monkeypatch, tmp_path, pass_has_control=True,
                      blocker_accepted=True,
                      escalated_band="STOP-SHIP (paired escalation)")
        status = mg.compute_status()          # prefix parses -> still gated
        assert status["open_stop_ship_count"] == 1
        self._patched2 = None
        # unparseable band: fail closed
        f = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        f[1]["escalated_band"] = "SEVERE-ISH"
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(f))
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_tampered_ratchet_baselines_refused(self, monkeypatch, tmp_path):
        # Tamper-test fail-open (2026-07-25): a hand-loosened baseline passed
        # silently. The baseline file is now in the attested hash set.
        self._patched(monkeypatch, tmp_path,
                      pass_has_control=True, blocker_accepted=True)
        (tmp_path / "audit" / "ratchet-baselines.json").write_text(
            json.dumps({"ratchets": {"test_count_floor": 1}}))
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_tampered_accepted_residuals_refused(self, monkeypatch, tmp_path):
        # Same class: silently ADDING an acceptance would legalize a new
        # blocker without a decision record.
        self._patched(monkeypatch, tmp_path,
                      pass_has_control=True, blocker_accepted=True)
        reg = tmp_path / "governance" / "accepted-residuals.json"
        d = json.loads(reg.read_text())
        d["accepted_open_findings"].append("A-99")
        reg.write_text(json.dumps(d))
        with pytest.raises(SystemExit):
            mg.compute_status()


class TestLiveEngagementConsistency:
    def test_live_gate_passes_end_to_end(self):
        # The committed artifacts must satisfy the gate exactly as CI runs it —
        # this is the same check, executed from the suite, so a drifted
        # artifact fails the test job even if the gate step were removed.
        status = mg.compute_status()
        assert status["present_check_count"] == 119
        assert status["production_eligible"] is False   # honest until ratified
