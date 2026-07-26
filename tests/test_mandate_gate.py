"""Mandate gate: the detectors and the fail-closed status invariants.

Each detector must fire on its defect AND stay quiet on legitimate input;
each status invariant must actually refuse (Article I/VI). Register
weakening lives in test_mandate_gate_registers.py, the S12 calibration and
the audit surface in test_mandate_gate_calibration.py."""

from __future__ import annotations

import json

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


class TestVolumeCompleteTextPresence:
    """P0.1 (branch review): a volume is COMPLETE only when its mandate SOURCE
    TEXT is present + attested, not merely when its findings are evidenced."""

    def test_part2_is_in_progress_when_text_absent(self):
        # the live repo: Part 2 text is not in the repo (manifest path null),
        # so part2_status must read IN_PROGRESS even though every C finding is
        # evidenced; Part 1 (text present + attested) stays COMPLETE.
        status = mg.compute_status()
        assert status["part1_status"] == "COMPLETE"
        assert status["part2_status"] == "IN_PROGRESS"

    def test_part1_flips_to_in_progress_if_its_text_is_tampered(
            self, monkeypatch, tmp_path):
        # both sides: if Part 1's committed text no longer matches its attested
        # sha, its volume is no longer COMPLETE either.
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=True)
        # give the fixture a manifest part for tracks A/B pointing at part1.md,
        # then corrupt the file so sha mismatches
        import hashlib
        gov = tmp_path / "governance" / "mandate"
        man = json.loads((gov / "manifest.json").read_text())
        (gov / "part1.md").write_text("real text\n")
        man["parts"] = [{"name": "part1", "path": "governance/mandate/part1.md",
                         "sha256": hashlib.sha256(b"real text\n").hexdigest(),
                         "tracks": ["A", "B"]}]
        (gov / "manifest.json").write_text(json.dumps(man))
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        assert mg.compute_status()["part1_status"] == "COMPLETE"
        (gov / "part1.md").write_text("TAMPERED\n")   # sha no longer matches
        assert mg.compute_status()["part1_status"] == "IN_PROGRESS"


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
        'auth = "4ed251b58a9c4b3e9f217c6d5e4a3b21"',     # hyphen-less uuid; pragma: allowlist secret
        'bearer = "dGhpcyBpc2FzZWNyZXR0b2tlbmFiY2RlZg=="',  # base64; pragma: allowlist secret
        'pat = "ghp_0123456789abcdefABCDEF"',            # provider PAT; pragma: allowlist secret
        'cookie = "sessionid_abcdefghijklmnop"',       # pragma: allowlist secret
        'sipgate_token = "4ed251b5-8a9c-4b3e-9f21-7c6d5e4a3b21"',  # pragma: allowlist secret
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
        # WRONG-REASON FIX (review P1.5-c): this asserted a hand-rolled regex
        # and never touched production, so a regression in the shipped detector
        # would not fail it. Drive the real module-level detector instead.
        import ast
        assert mg.tool_enabling(ast.parse(snippet)), snippet

    def test_class5_tool_enabling_is_quiet_on_benign_code(self):
        # both sides: the production detector must NOT fire on tool-free calls
        import ast
        assert not mg.tool_enabling(ast.parse(
            "client.messages.create(model=m, max_tokens=8, messages=x)"))

    @pytest.mark.parametrize("col", [
        "user_id", "tenant_id", "owner_id",          # original three
        "account_id", "customer_id", "workspace_id",  # the bypasses
        "organisation_id", "member_id",
    ])
    def test_class6_detects_tenancy_columns_beyond_the_original_three(self, col):
        src = f"    {col}: Mapped[int] = mapped_column(Integer)"
        assert mg.declares_tenancy_column(src), col      # production detector

    def test_class6_detects_tenancy_foreign_keys(self):
        src = 'x: Mapped[int] = mapped_column(ForeignKey("accounts.id"))'
        assert mg.declares_tenancy_column(src)           # production detector

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
            {"metric": "test_count_floor", "change": "460 -> 457 (LOOSENING)",
             "reason": "an unrelated earlier decision, not this change",
             "authorised_by": "operator"}])
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
            {"metric": "test_count_floor", "change": "999 -> 10 (LOOSENING)",
             "reason": "suite intentionally reduced for this scenario",
             "authorised_by": "operator", "is_finding": True}])
        monkeypatch.setattr(mg, "previous_version",
                            lambda _p: {"ratchets": {"test_count_floor": 999}})
        mg.cmd_ratchet(measure_only=False)      # does not raise

    def test_newly_accepted_blocker_needs_a_decision_record(self, monkeypatch, tmp_path):
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=True,
                                  with_acceptance_record=False)
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


class TestPanelFindingsRound12:
    """Two more gate defects, found in part 2 of the full-coverage review."""

    def test_push_to_main_does_not_compare_state_to_itself(self, monkeypatch):
        # GITHUB_BASE_REF unset (a push) must fall back to the PARENT commit;
        # comparing HEAD to origin/main on main compared new state to itself,
        # silently passing every weakening check
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        monkeypatch.setattr(mg, "_resolve", lambda ref: "parentsha"
                            if ref == "HEAD~1" else None)
        assert mg.comparison_point() == "parentsha"

    def test_pull_request_compares_against_the_merge_base(self, monkeypatch):
        monkeypatch.setenv("GITHUB_BASE_REF", "main")
        monkeypatch.setattr(mg, "_resolve", lambda ref: "tipsha")

        class _R:
            returncode = 0
            stdout = "mergebasesha\n"
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: _R())
        assert mg.comparison_point() == "mergebasesha"

    def test_unresolvable_base_in_ci_fails_closed(self, monkeypatch):
        # previously returned None, which SKIPPED every weakening check
        monkeypatch.setenv("GITHUB_BASE_REF", "main")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setattr(mg, "_resolve", lambda ref: None)
        with pytest.raises(SystemExit):
            mg.comparison_point()

    def test_acceptance_needs_a_structured_record_not_a_substring(
            self, monkeypatch, tmp_path):
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=True,
                                  with_acceptance_record=False)
        reg = tmp_path / "governance" / "accepted-residuals.json"
        d = json.loads(reg.read_text())
        # the old bypass: mention the id in any unrelated _meta field
        d["_meta"] = {"note": "see A-02 in the docs"}
        reg.write_text(json.dumps(d))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        monkeypatch.setattr(mg, "previous_version",
                            lambda p: {"accepted_open_findings": []}
                            if "accepted" in p else None)
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_a_proper_acceptance_record_is_honoured(self, monkeypatch, tmp_path):
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=True)
        reg = tmp_path / "governance" / "accepted-residuals.json"
        d = json.loads(reg.read_text())
        d["acceptance_records"] = [{"finding_id": "A-02",
                                    "reason": "compensating control recorded",
                                    "authorised_by": "operator"}]
        reg.write_text(json.dumps(d))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        monkeypatch.setattr(mg, "previous_version",
                            lambda p: {"accepted_open_findings": []}
                            if "accepted" in p else None)
        mg.compute_status()          # does not raise


class TestPanelFindingsRound13:
    """Three instances of one class: every register the gate trusts is written
    by the change it authorises. These raise the cost of laundering; only
    B-35 write separation removes the class (documented in the module)."""

    def _patched(self, monkeypatch, tmp_path, **kw):
        _write_minimal_engagement(tmp_path, **kw)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")

    def test_removing_a_check_from_the_pinned_universe_blocks(
            self, monkeypatch, tmp_path):
        # deleting the same id from manifest + catalogue + findings and
        # refreshing every hash silently shrank the audited universe
        self._patched(monkeypatch, tmp_path, pass_has_control=True,
                      blocker_accepted=True)
        for name in ("00-check-catalogue.json", "03-findings.json"):
            path = tmp_path / "audit" / name
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                data["checks"] = [c for c in data["checks"] if c["id"] != "A-02"]
                data["registered_check_count"] = 1
            else:
                data = [r for r in data if r["id"] != "A-02"]
            path.write_text(json.dumps(data))
        reg = tmp_path / "governance" / "accepted-residuals.json"
        d = json.loads(reg.read_text())
        d["accepted_open_findings"] = []
        d["acceptance_records"] = []
        reg.write_text(json.dumps(d))
        _reattest(tmp_path, required=["A-01"])          # universe shrunk too
        monkeypatch.setattr(mg, "previous_version",
                            lambda p: {"required_check_ids": ["A-01", "A-02"]}
                            if "manifest" in p else None)
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_a_brand_new_register_still_needs_records(self, monkeypatch, tmp_path):
        # prev=None made every entry "new" AND skipped the check entirely
        self._patched(monkeypatch, tmp_path, pass_has_control=True,
                      blocker_accepted=True, with_acceptance_record=False)
        monkeypatch.setattr(mg, "previous_version", lambda _p: None)
        with pytest.raises(SystemExit):
            mg.compute_status()

    def test_a_fieldless_ratchet_record_does_not_license_a_loosening(
            self, monkeypatch, tmp_path):
        import hashlib
        (tmp_path / "audit").mkdir(exist_ok=True)
        baselines = tmp_path / "audit" / "ratchet-baselines.json"
        baselines.write_text(json.dumps({
            "_meta": {"decision_records": [
                {"metric": "test_count_floor", "change": "999 -> 10"}]},
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
        with pytest.raises(SystemExit):      # no reason, no authorised_by
            mg.cmd_ratchet(measure_only=False)


class TestPanelFindingsRound14:
    """Both defects were in code written during rounds 12-13 — hardening a
    control under review keeps introducing the class it is meant to close."""

    def test_push_is_judged_against_the_pre_push_sha(self, monkeypatch):
        # HEAD~1 judges a multi-commit push against its OWN weakened parent:
        # weaken in commit N-2 and the only CI run compares tip to it, passing
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        monkeypatch.setenv("MANDATE_PUSH_BEFORE", "beforesha")
        monkeypatch.setattr(mg, "_is_shallow", lambda: False)
        monkeypatch.setattr(mg, "_resolve",
                            lambda ref: "resolved-before" if ref == "beforesha"
                            else "parent")
        assert mg.comparison_point() == "resolved-before"

    def test_branch_creation_sentinel_falls_back_to_parent(self, monkeypatch):
        # github.event.before is all-zeros when the branch is new
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        monkeypatch.setenv("MANDATE_PUSH_BEFORE", "0" * 40)
        monkeypatch.setattr(mg, "_is_shallow", lambda: False)
        monkeypatch.setattr(mg, "_resolve",
                            lambda ref: "parent" if ref == "HEAD~1" else None)
        assert mg.comparison_point() == "parent"

    def test_missing_pre_push_sha_in_ci_fails_closed(self, monkeypatch):
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("MANDATE_PUSH_BEFORE", "absentsha")
        monkeypatch.setattr(mg, "_is_shallow", lambda: False)
        monkeypatch.setattr(mg, "_resolve", lambda ref: None)
        with pytest.raises(SystemExit):
            mg.comparison_point()

    def test_shallow_checkout_in_ci_fails_closed(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setattr(mg, "_is_shallow", lambda: True)
        with pytest.raises(SystemExit):
            mg.comparison_point()

    def test_unreadable_history_blocks_instead_of_reading_as_absent(
            self, monkeypatch):
        # ANY git failure previously meant "the file did not exist", so a
        # truncated history silently disabled every weakening check
        monkeypatch.setattr(mg, "comparison_point", lambda: "deadbeef")

        class _R:
            returncode = 128
            stdout = ""
            stderr = "fatal: not a tree object"
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: _R())
        with pytest.raises(SystemExit):
            mg.previous_version("audit/ratchet-baselines.json")

    def test_genuinely_absent_artifact_returns_none(self, monkeypatch):
        monkeypatch.setattr(mg, "comparison_point", lambda: "deadbeef")

        class _R:
            returncode = 0
            stdout = ""            # ls-tree lists nothing -> truly absent
            stderr = ""
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: _R())
        assert mg.previous_version("audit/ratchet-baselines.json") is None
