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
                              blocker_accepted: bool):
    (root / "audit").mkdir()
    gov = root / "governance" / "mandate"
    gov.mkdir(parents=True)
    checks = [{"id": "A-01", "track": "A", "title": "gate", "priority": 9,
               "founding_band": "BLOCKER-1", "status": "active"},
              {"id": "A-02", "track": "A", "title": "suite", "priority": 10,
               "founding_band": "STOP-SHIP", "status": "active"}]
    (root / "audit" / "00-check-catalogue.json").write_text(json.dumps(
        {"catalogue_version": "t", "registered_check_count": 2, "checks": checks}))
    findings = [
        {"id": "A-01", "band": "BLOCKER-1", "priority": 9, "verdict": "PASS",
         "standing_control": ("ci" if pass_has_control else None)},
        {"id": "A-02", "band": "STOP-SHIP", "priority": 10, "verdict": "FAIL"},
    ]
    (root / "audit" / "03-findings.json").write_text(json.dumps(findings))
    (root / "governance" / "accepted-residuals.json").write_text(json.dumps(
        {"constitution_state": "IN_FORCE_PROVISIONAL",
         "accepted_open_findings": (["A-02"] if blocker_accepted else [])}))
    (root / "governance" / "constitution.md").write_text("law\n")
    (gov / "part1.md").write_text("mandate\n")
    import hashlib
    (gov / "manifest.json").write_text(json.dumps({
        "part1_sha256": hashlib.sha256(b"mandate\n").hexdigest(),
        "constitution_sha256": hashlib.sha256(b"law\n").hexdigest()}))


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


class TestLiveEngagementConsistency:
    def test_live_gate_passes_end_to_end(self):
        # The committed artifacts must satisfy the gate exactly as CI runs it —
        # this is the same check, executed from the suite, so a drifted
        # artifact fails the test job even if the gate step were removed.
        status = mg.compute_status()
        assert status["present_check_count"] == 119
        assert status["production_eligible"] is False   # honest until ratified
