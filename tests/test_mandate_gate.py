"""The mandate gate gates the repo, so its own guards are tested both ways:
each detector must fire on its defect AND stay quiet on legitimate input,
and each fail-closed status invariant must actually refuse (Article I/VI)."""

from __future__ import annotations

import importlib.util
import json
import re
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
         "accepted_open_findings": (["A-02"] if blocker_accepted else [])}))
    (root / "governance" / "constitution.md").write_text("law\n")
    (root / "governance" / "mandate.md").write_text("combined\n")
    (gov / "part1.md").write_text("mandate\n")

    _reattest(root, required=["A-01", "A-02"])


def _reattest(root: Path, required=None):
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
        _reattest(tmp_path)
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


class TestVerdictAndDenominatorGuards:
    """Adversarial audit 2026-07-25: the gate trusted its inputs' domain."""

    def _patched(self, monkeypatch, tmp_path, **kw):
        _write_minimal_engagement(tmp_path, **kw)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")

    @pytest.mark.parametrize("bad", ["Fail", "fail", "FAILED", "WAIVED", None])
    def test_non_canonical_verdict_refused(self, monkeypatch, tmp_path, bad):
        # CRITICAL fail-open: a non-canonical verdict fell out of the open-
        # blocker loop, the PASS-control check AND the N/A check at once, so
        # a STOP-SHIP became invisible to every gate.
        self._patched(monkeypatch, tmp_path,
                      pass_has_control=True, blocker_accepted=True)
        f = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        f[1]["verdict"] = bad
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(f))
        _reattest(tmp_path)
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_tampered_findings_file_refused(self, monkeypatch, tmp_path):
        # The findings file is now attested: flipping a verdict and
        # regenerating status no longer launders the edit.
        self._patched(monkeypatch, tmp_path,
                      pass_has_control=True, blocker_accepted=True)
        f = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        f[1]["verdict"] = "PASS"
        f[1]["standing_control"] = {"mechanism": "a" * 20,
                                    "demonstrated": "b" * 20}
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(f))
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_shrunk_denominator_refused(self, monkeypatch, tmp_path):
        # Deleting a check from catalogue AND findings as a matched pair no
        # longer shrinks the audit universe: the manifest pins it.
        self._patched(monkeypatch, tmp_path,
                      pass_has_control=True, blocker_accepted=False)
        cat = json.loads((tmp_path / "audit" / "00-check-catalogue.json").read_text())
        cat["checks"] = [c for c in cat["checks"] if c["id"] != "A-02"]
        cat["registered_check_count"] = 1
        (tmp_path / "audit" / "00-check-catalogue.json").write_text(json.dumps(cat))
        f = [r for r in json.loads(
            (tmp_path / "audit" / "03-findings.json").read_text())
            if r["id"] != "A-02"]
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(f))
        _reattest(tmp_path)          # hashes fresh; pinned universe unchanged
        with pytest.raises(SystemExit):
            mg.compute_status()

    @pytest.mark.parametrize("vacuous", [
        {"mechanism": "   ", "demonstrated": "   "},
        {"mechanism": True, "demonstrated": True},
        {"mechanism": 1, "demonstrated": [1]},
        {"mechanism": "ci", "demonstrated": "ok"},        # too thin to mean anything
    ])
    def test_vacuous_standing_control_refused(self, monkeypatch, tmp_path,
                                              vacuous):
        self._patched(monkeypatch, tmp_path,
                      pass_has_control=True, blocker_accepted=True)
        f = json.loads((tmp_path / "audit" / "03-findings.json").read_text())
        f[0]["standing_control"] = vacuous
        (tmp_path / "audit" / "03-findings.json").write_text(json.dumps(f))
        _reattest(tmp_path)
        with pytest.raises(SystemExit):
            mg.compute_status()


class TestCredentialScannerBreadth:
    """Payloads the 2026-07-25 adversary used to walk past the scanner."""

    @pytest.mark.parametrize("line", [
        'auth = "4ed251b58a9c4b3e9f217c6d5e4a3b21"',     # hyphen-less uuid
        'bearer = "dGhpcyBpc2FzZWNyZXR0b2tlbmFiY2RlZg=="',  # base64
        'pat = "ghp_0123456789abcdefABCDEF"',            # provider PAT
        'cookie = "sessionid_abcdefghijklmnop"',
        'sipgate_token = "4ed251b5-8a9c-4b3e-9f21-7c6d5e4a3b21"',
    ])
    def test_catches_credential_shapes(self, tmp_path, line):
        p = tmp_path / "c.py"
        p.write_text(line + "\n")
        assert mg.scan_credential_shapes([p])

    @pytest.mark.parametrize("line", [
        'TAGS = ["NetCashProvidedByUsedInOperatingActivities"]',
        'granularity: str = Query("raw", pattern="^(raw|daily)$")',
        'URL = "https://api.stlouisfed.org/fred/series/observations"',
        'path_note = "path=B_constituent_compute"',
    ])
    def test_quiet_on_ordinary_code(self, tmp_path, line):
        p = tmp_path / "q.py"
        p.write_text(line + "\n")
        assert mg.scan_credential_shapes([p]) == []

    def test_import_scan_sees_local_and_app_prefixed(self, tmp_path):
        p = tmp_path / "m.py"
        p.write_text("def g():\n    import nonexistent_pkg\n\n"
                     "import apptools_hallucinated\n")
        found = mg.collect_imports([p])
        assert found == {"nonexistent_pkg", "apptools_hallucinated"}
        assert sorted(mg.unresolvable_imports(sorted(found))) == [
            "apptools_hallucinated", "nonexistent_pkg"]

    def test_import_scan_ignores_docstring_prose(self, tmp_path):
        p = tmp_path / "d.py"
        p.write_text('"""Long price histories from Stooq ^spx or import the '
                     'committed seed CSVs."""\nimport json\n')
        assert mg.collect_imports([p]) == {"json"}


class TestPanelFindingsPR23:
    """Defects the cross-vendor panel found once risk-ordered chunking put
    mandate_gate.py inside the reviewed payload for the first time."""

    def test_collection_failure_blocks_instead_of_measuring(self, monkeypatch):
        # rc was ignored: a suite with import errors still prints
        # "N tests collected", so a broken suite could satisfy the floor.
        import subprocess

        class _Result:
            returncode = 2
            stdout = "!!!! Interrupted: 3 errors during collection !!!!\n" \
                     "500 tests collected\n"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
        with pytest.raises(SystemExit):
            mg.measure_ratchets()

    def test_collection_errors_in_output_block(self, monkeypatch):
        import subprocess

        class _Result:
            returncode = 0        # rc clean but errors reported
            stdout = "ERROR tests/test_x.py\n400 tests collected\n"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
        with pytest.raises(SystemExit):
            mg.measure_ratchets()

    @pytest.mark.parametrize("snippet", [
        "client.messages.create(model=m, tools=[t])",
        'client.messages.create(model=m, **{"tools": [t]})',
        "payload = {'tools': [t]}",
    ])
    def test_class5_detects_every_tools_spelling(self, snippet):
        # only `tools=` was matched; the dict/**kwargs spellings enabled tool
        # use while calibration still reported the class not-applicable
        assert re.search(r"""["']?\btools["']?\s*[=:]""", snippet)

    @pytest.mark.parametrize("col", [
        "user_id", "tenant_id", "owner_id",          # original three
        "account_id", "customer_id", "workspace_id",  # the bypasses
        "organisation_id", "member_id",
    ])
    def test_class6_detects_tenancy_columns_beyond_the_original_three(self, col):
        src = f"    {col}: Mapped[int] = mapped_column(Integer)"
        pattern = (r"\b(user|tenant|owner|account|customer|org|organisation|"
                   r"organization|workspace|member|subject|principal)_id\b")
        assert re.search(pattern, src, re.IGNORECASE), col

    def test_class6_detects_tenancy_foreign_keys(self):
        src = 'x: Mapped[int] = mapped_column(ForeignKey("accounts.id"))'
        pattern = (r"""ForeignKey\(\s*["']"""
                   r"(users|accounts|tenants|orgs|organisations|organizations|"
                   r"customers|members|workspaces)\.")
        assert re.search(pattern, src, re.IGNORECASE)

    def test_live_models_stay_single_tenant(self):
        # the real invariant: this repo must remain single-tenant, so the
        # broadened pattern must NOT fire on the actual models module
        mg.cmd_calibrate()      # exits nonzero via _fail if it does


class TestPanelFindingsRound11:
    """Four defects the panel found in the gate once full coverage put
    mandate_gate.py in a reviewed part."""

    def test_escalated_band_may_raise_but_never_lower_severity(self):
        # the fail-open: band STOP-SHIP + escalated_band MUST-FIX escaped the
        # blocker gate entirely
        assert mg.effective_band(
            {"id": "X", "band": "STOP-SHIP", "escalated_band": "MUST-FIX"}
        ) == "STOP-SHIP"
        assert mg.effective_band(
            {"id": "X", "band": "MUST-FIX", "escalated_band": "STOP-SHIP"}
        ) == "STOP-SHIP"
        assert mg.effective_band({"id": "X", "band": "BLOCKER-2"}) == "BLOCKER-2"

    def test_unparseable_escalation_fails_closed(self):
        with pytest.raises(SystemExit):
            mg.effective_band({"id": "X", "band": "STOP-SHIP",
                               "escalated_band": "SEVERE-ISH"})

    def test_dependencies_come_from_arrays_not_the_whole_file(self):
        deps = mg.declared_dependencies()
        assert "fastapi" in deps
        # the project's own name and prose must NOT count as declared
        assert "bubblegauge" not in deps
        assert "readme" not in deps

    def _temp_baselines(self, monkeypatch, tmp_path, records):
        """Temp AUDIT+GOV with a CONSISTENT manifest hash, so these tests
        exercise the loosening check and not the attestation check (they
        initially passed for the wrong reason — the hash mismatch raised
        first, which would have kept passing with the check deleted)."""
        import hashlib
        (tmp_path / "audit").mkdir(exist_ok=True)
        baselines = tmp_path / "audit" / "ratchet-baselines.json"
        baselines.write_text(json.dumps(
            {"_meta": {"decision_records": records},
             "ratchets": {"test_count_floor": 10}}))
        gov = tmp_path / "governance" / "mandate"
        gov.mkdir(parents=True, exist_ok=True)
        (gov / "manifest.json").write_text(json.dumps({
            "ratchet_baselines_sha256":
                hashlib.sha256(baselines.read_bytes()).hexdigest()}))
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        monkeypatch.setattr(mg, "measure_ratchets",
                            lambda: {"test_count_floor": 10})

    def test_loosening_a_ratchet_without_a_decision_record_blocks(
            self, monkeypatch, tmp_path):
        self._temp_baselines(monkeypatch, tmp_path, records=[])
        monkeypatch.setattr(mg, "previous_version",
                            lambda _p: {"ratchets": {"test_count_floor": 999}})
        with pytest.raises(SystemExit):
            mg.cmd_ratchet(measure_only=False)

    def test_a_record_for_a_DIFFERENT_change_does_not_license_this_one(
            self, monkeypatch, tmp_path):
        # naming the metric once must not authorise every later loosening
        self._temp_baselines(monkeypatch, tmp_path, records=[
            {"metric": "test_count_floor", "change": "460 -> 457 (LOOSENING)"}])
        monkeypatch.setattr(mg, "previous_version",
                            lambda _p: {"ratchets": {"test_count_floor": 999}})
        with pytest.raises(SystemExit):
            mg.cmd_ratchet(measure_only=False)

    def test_removing_a_ratchet_entirely_blocks(self, monkeypatch):
        monkeypatch.setattr(mg, "previous_version",
                            lambda _p: {"ratchets": {"a_gone_floor": 1}})
        with pytest.raises(SystemExit):
            mg.cmd_ratchet(measure_only=False)

    def test_a_loosening_recorded_with_its_exact_values_is_permitted(
            self, monkeypatch, tmp_path):
        # the honest path stays open when the record matches THIS change
        self._temp_baselines(monkeypatch, tmp_path, records=[
            {"metric": "test_count_floor", "change": "999 -> 10 (LOOSENING)"}])
        monkeypatch.setattr(mg, "previous_version",
                            lambda _p: {"ratchets": {"test_count_floor": 999}})
        mg.cmd_ratchet(measure_only=False)      # does not raise

    def test_newly_accepted_blocker_needs_a_decision_record(self, monkeypatch, tmp_path):
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=True)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        # base branch accepted nothing; A-02 appears with no _meta naming it
        monkeypatch.setattr(mg, "previous_version",
                            lambda p: {"accepted_open_findings": []}
                            if "accepted" in p else None)
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
