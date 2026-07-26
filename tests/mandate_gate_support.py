"""Shared fixtures for the mandate-gate test modules.

The suite was one 92KB file, which exceeded the cross-vendor panel's
per-part review budget: an oversized CONTROL-BEARING file is omitted
rather than truncated, so the gate's own tests were blocking the run as
unreviewable. Split by concern, with the fixtures that every part needs
living here."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "mandate_gate", Path(__file__).resolve().parents[1] / "scripts" / "mandate_gate.py")
mg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mg)

def clear_ambient_ci_env(monkeypatch):
    """These tests must not depend on ambient CI variables or on the real
    repository's git history (CI failure 2026-07-25: with GITHUB_BASE_REF set,
    temp fixtures were compared against the LIVE repo, so they read as
    brand-new registers and tripped the acceptance check). Tests that need a
    comparison point monkeypatch `previous_version` themselves."""
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


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


def write_minimal_engagement(root: Path, *, pass_has_control: bool,
                              blocker_accepted: bool,
                              structured_control: bool = True,
                              escalated_band: str | None = None,
                              with_acceptance_record: bool = True):
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
        control = ({"mechanism": "blocking CI job runs the gate every change",
                    "demonstrated": "observed blocking a seeded defect"}
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
         "accepted_open_findings": (["A-02"] if blocker_accepted else []),
         "acceptance_records": ([{
             "finding_id": "A-02",
             "reason": "compensating control and tripwire recorded in audit/06",
             "authorised_by": "operator (test fixture)"}]
             if blocker_accepted and with_acceptance_record else [])}))
    (root / "governance" / "constitution.md").write_text("law\n")
    (root / "governance" / "mandate.md").write_text("combined\n")
    (gov / "part1.md").write_text("mandate\n")

    reattest(root, required=["A-01", "A-02"])
    stage_all(root)


def stage_all(root: Path) -> None:
    """Make `root` a git work tree with every fixture file TRACKED.

    The gate's source-attestation checks (R-02 `_part_source_ok`, R-03
    `_combined_mandate_failures`) require the attested files to be git-tracked —
    a stray on-disk file that merely matches a hash is not a repository source.
    A bare `tmp_path` is not a git repo, so these checks would fail closed on
    the fixtures for the wrong reason. Initialise a real repo and stage the
    files so the tracking predicate is exercised HONESTLY (tests that then
    mutate a tracked file keep it tracked — a modified tracked file is still
    tracked, which is exactly the tamper case the sha check must catch)."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True,
                   capture_output=True)


def reattest(root: Path, required=None):
    """Recompute the manifest hashes (and optionally the pinned universe) so a
    test can exercise ONE guard without tripping the attestation first."""
    import hashlib

    def _sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    mpath = root / "governance" / "mandate" / "manifest.json"
    m = json.loads(mpath.read_text()) if mpath.exists() else {}
    if required is not None:
        m["required_check_ids"] = required
    m.update({
        "part1_sha256": _sha(root / "governance" / "mandate" / "part1.md"),
        "constitution_sha256": _sha(root / "governance" / "constitution.md"),
        "combined_mandate_sha256": _sha(root / "governance" / "mandate.md"),
        "ratchet_baselines_sha256": _sha(root / "audit" / "ratchet-baselines.json"),
        "accepted_residuals_sha256": _sha(
            root / "governance" / "accepted-residuals.json"),
        "findings_sha256": _sha(root / "audit" / "03-findings.json"),
        "check_catalogue_sha256": _sha(root / "audit" / "00-check-catalogue.json"),
    })
    mpath.write_text(json.dumps(m))


