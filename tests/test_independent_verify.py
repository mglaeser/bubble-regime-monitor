"""The independent cross-vendor review panel's decision logic, under pytest.

The pure gate functions (decide / model_matches / require_approvals /
attest_reasons / attest_proof) are the panel's entire merge-blocking logic;
this suite pins their fail-closed semantics so a future edit cannot silently
soften them. The script's own --selftest covers the identical cases at CI
runtime; here the same guarantees ride the normal pytest suite.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "independent_verify", Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
iv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(iv)

MDL = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
A = {"ok": True, "v": {"refuted": False, "reason": "reason long enough a"}}
A2 = {"ok": True, "v": {"refuted": False, "reason": "reason long enough b"}}
RF = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "real bug"}}
ERR = {"ok": False, "reason": "API 500"}


class TestDecide:
    def test_fail_closed_on_unparsable(self):
        assert iv.decide(None)["block"] is True
        assert iv.decide({})["block"] is True
        assert iv.decide({"refuted": "yes"})["block"] is True   # non-bool

    def test_confidence_thresholds(self):
        assert iv.decide({"refuted": True, "confidence": "high"})["block"] is True
        assert iv.decide({"refuted": True, "confidence": "medium"})["block"] is True
        assert iv.decide({"refuted": True, "confidence": "low"})["block"] is False
        assert iv.decide({"refuted": False})["block"] is False


class TestRequiredApproverRole:
    def test_sol_veto_any_confidence(self):
        low = {"ok": True, "v": {"refuted": True, "confidence": "low", "reason": "small doubt"}}
        assert iv.require_approvals([A, low, A], MDL, "gpt-5.6-sol", 1)["block"] is True
        assert iv.require_approvals([A, RF, A], MDL, "gpt-5.6-sol", 1)["block"] is True

    def test_sol_missing_or_fallback_blocks(self):
        no_sol = ["gpt-5.3-codex", "gpt-5.6", "gpt-4.1-mini"]
        assert iv.require_approvals([A, A2, A], no_sol, "gpt-5.6-sol", 1)["block"] is True

    def test_dated_snapshot_counts_variant_does_not(self):
        assert iv.model_matches("gpt-5.6-sol-2026-07-01", "gpt-5.6-sol") is True
        for variant in ("gpt-5.6-sol-mini", "gpt-5.6-sol-codex", "gpt-5.6-sol-preview",
                        "gpt-5.6-solaris", "gpt-5.6"):
            assert iv.model_matches(variant, "gpt-5.6-sol") is False

    def test_independent_corroboration_distinct_models(self):
        assert iv.require_approvals([A, A2, A], MDL, "gpt-5.6-sol", 1)["block"] is False
        assert iv.require_approvals([RF, A, RF], MDL, "gpt-5.6-sol", 1)["block"] is True
        dup = ["gpt-4.1-mini", "gpt-5.6-sol", "gpt-4.1-mini"]
        assert iv.require_approvals([A, A2, A], dup, "gpt-5.6-sol", 2)["block"] is True
        solo = ["gpt-5.6-sol"] * 3
        assert iv.require_approvals([A, A2, A], solo, "gpt-5.6-sol", 1)["block"] is True

    def test_nan_min_others_never_fail_open(self):
        assert iv.require_approvals([RF, A2, RF], MDL, "gpt-5.6-sol", float("nan"))["block"] is True
        assert iv.require_approvals([A, A2, RF], MDL, "gpt-5.6-sol", float("nan"))["block"] is False

    def test_sol_approval_must_be_proven(self):
        ch = "selftest-challenge"
        empty = {"ok": True, "v": {"refuted": False, "reason": "", "proof": f"{ch}-7"}}
        noproof = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol"}}
        good = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol",
                                  "proof": f"{ch}-7"}}
        assert iv.require_approvals([A, empty, A], MDL, "gpt-5.6-sol", 1, ch)["block"] is True
        assert iv.require_approvals([A, noproof, A], MDL, "gpt-5.6-sol", 1, ch)["block"] is True
        assert iv.require_approvals([A, good, A], MDL, "gpt-5.6-sol", 1, ch)["block"] is False


class TestIntegrityGates:
    def test_canned_green_blocks(self):
        r = lambda s, refuted=False: {"ok": True, "v": {"refuted": refuted, "reason": s}}  # noqa: E731
        same = "reason one aaaa"
        assert iv.attest_reasons([r(same), r(same), r(same)], 3)["block"] is True
        assert iv.attest_reasons([r(same), r(same), r("real bug here", True)], 3)["block"] is True
        assert iv.attest_reasons([r("reason a x1"), r("reason b x2"), r("reason c x3")], 3)["block"] is False

    def test_proof_of_check_bounds(self):
        ch = "selftest-challenge"
        pr = lambda p: {"ok": True, "v": {"refuted": False, "reason": "reason long enough", "proof": p}}  # noqa: E731
        assert iv.attest_proof([pr(f"{ch}-1"), pr(f"{ch}-9999"), pr(f"{ch}-500")], ch, 3)["block"] is False
        assert iv.attest_proof([pr(f"{ch}-0"), pr(f"{ch}-0"), pr(f"{ch}-0")], ch, 3)["block"] is True
        assert iv.attest_proof([pr(f"{ch}-10000")] * 3, ch, 3)["block"] is True
        assert iv.attest_proof([pr("wrong-1")] * 3, ch, 3)["block"] is True
        assert iv.attest_proof([pr(f"{ch}-7")] * 3, "", 3)["block"] is True


class TestNoKeyResidualMode:
    def test_selftest_passes_and_no_key_is_green_and_visible(self, monkeypatch):
        script = str(Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
        out = subprocess.run([sys.executable, script, "--selftest"],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and "selftest" in out.stdout
        env = {"PATH": "/usr/bin:/bin"}   # no keys at all
        out2 = subprocess.run([sys.executable, script], capture_output=True, text=True,
                              timeout=60, env=env)
        assert out2.returncode == 0                 # never fake-blocks
        assert "RESIDUAL" in out2.stdout            # never fake-green either: loudly inactive


class TestEmptyEnvVarsAreAbsent:
    def test_empty_base_url_falls_back_to_default(self, monkeypatch):
        # GitHub Actions injects EMPTY strings for unset repo variables; an
        # empty VERIFIER_BASE_URL must behave like an absent one (observed
        # live: BASE="" crashed every request with "unknown url type").
        monkeypatch.setenv("VERIFIER_BASE_URL", "")
        spec = importlib.util.spec_from_file_location(
            "independent_verify_emptyenv",
            Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.BASE == "https://api.openai.com/v1"


class TestPanelFindingsOnItself:
    """Sol's veto on PR #21 raised two findings about the panel's own code;
    both responses are pinned here."""

    def test_privacy_excludes_are_case_insensitive(self):
        # uppercase .PNG/.SVG/.PDF must be excluded exactly like lowercase
        assert all(spec.startswith(":(exclude,icase,glob)") for spec in iv._EXCLUDES)

    def test_strict_mode_blocks_any_high_medium_refutation(self):
        models = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
        ok = {"ok": True, "v": {"refuted": False, "reason": "reason long enough"}}
        rf = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "bug"}}
        low = {"ok": True, "v": {"refuted": True, "confidence": "low", "reason": "doubt"}}
        assert iv.strict_any_refutation([ok, ok, rf], models)["block"] is True
        assert iv.strict_any_refutation([ok, ok, low], models)["block"] is False
        assert iv.strict_any_refutation([ok, ok, ok], models)["block"] is False

    def test_default_mode_stays_reference_identical(self):
        # WITHOUT strict mode the reference semantics hold: Sol + one distinct
        # corroborator green even when a third voice refutes high-confidence.
        models = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
        ok = {"ok": True, "v": {"refuted": False, "reason": "reason long enough x"}}
        ok2 = {"ok": True, "v": {"refuted": False, "reason": "reason long enough y"}}
        rf = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "bug"}}
        assert iv.require_approvals([rf, ok, ok2], models, "gpt-5.6-sol", 1)["block"] is False

    def test_fork_origin_without_key_fails_closed(self):
        # Sol round 2: fork PRs run with secrets withheld; no-key there must
        # BLOCK (exit 1), while same-repo no-key stays green-but-loud.
        script = str(Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
        env = {"PATH": "/usr/bin:/bin", "VERIFIER_REQUIRE_KEY": "true"}
        out = subprocess.run([sys.executable, script], capture_output=True, text=True,
                             timeout=60, env=env)
        assert out.returncode == 1
        assert "fork-origin" in out.stderr

    def test_round3_data_denylist_extended(self):
        for ext in ("csv", "tsv", "sql", "jsonl", "parquet", "sqlite", "dump", "bak"):
            assert ext in iv.EXCLUDE_EXTS
        # .json stays reviewable ON PURPOSE: frozen_methodology.json IS the
        # methodology and must be visible to the panel.
        assert "json" not in iv.EXCLUDE_EXTS

    def test_round3_truncation_is_explicitly_marked(self):
        long = "x" * 100
        marked = iv.truncate_marked(long, 40, "DIFF BODY")
        assert marked.startswith("x" * 40)
        assert "DIFF BODY TRUNCATED — 60 of 100 bytes omitted" in marked
        assert iv.truncate_marked("short", 40, "DIFF BODY") == "short"

    def test_round3_responses_fallback_on_400_and_404(self):
        msg = "This model is only supported in v1/responses"
        assert iv.should_fallback_responses(400, msg) is True
        assert iv.should_fallback_responses(404, msg) is True
        assert iv.should_fallback_responses(400, "bad request: temperature") is False
        assert iv.should_fallback_responses(403, msg) is False
        assert iv.should_fallback_responses(400, None) is False

    def test_round4_base_branch_from_github_base_ref(self, monkeypatch):
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        assert iv.base_branch() == "main"
        monkeypatch.setenv("GITHUB_BASE_REF", "")     # Actions empty-string trap
        assert iv.base_branch() == "main"
        monkeypatch.setenv("GITHUB_BASE_REF", "develop")
        assert iv.base_branch() == "develop"

    def test_round4_file_list_unfiltered_contents_filtered(self):
        cmds = iv.diff_commands("BASE")
        assert not any(a.startswith(":(exclude") for a in cmds["names"])
        assert any(a.startswith(":(exclude") for a in cmds["stat"])
        assert any(a.startswith(":(exclude") for a in cmds["body"])

    def test_round6_sh_failure_blocks_and_bad_utf8_stays_visible(self):
        import pytest as _pytest
        with _pytest.raises(iv.DiffError):
            iv._sh(["git", "rev-parse", "--verify", "no-such-ref-xyz123"], required=True)
        assert iv._sh(["false"]) == ""            # non-required keeps soft behavior
        out = iv._sh([sys.executable, "-c",
                      "import sys; sys.stdout.buffer.write(b'ok\\xff\\xfebad')"])
        assert "ok" in out and "bad" in out       # invalid UTF-8 visible, not vanished
        assert "�" in out

    def test_round6_diff_error_blocks_main(self, monkeypatch):
        monkeypatch.setattr(iv, "KEY", "fake-key-for-test")
        def boom():
            raise iv.DiffError("git exploded")
        monkeypatch.setattr(iv, "build_diff", boom)
        assert iv.main() == 1

    def test_round7_merge_base_failure_blocks_not_narrows(self, monkeypatch):
        import pytest as _pytest
        # a missing base ref must raise DiffError (block), never silently fall
        # back to HEAD~1 (which reviews only the tip commit of a multi-commit PR)
        monkeypatch.setenv("GITHUB_BASE_REF", "definitely-not-a-branch-xyz")
        with _pytest.raises(iv.DiffError):
            iv.build_diff()

    def test_round8_low_refutation_does_not_attest(self):
        ch = "selftest-challenge"
        sol_good = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol",
                                      "proof": f"{ch}-7"}}
        low_ref = {"ok": True, "v": {"refuted": True, "confidence": "low",
                                     "reason": "substantive doubt here", "proof": f"{ch}-9"}}
        canned = {"ok": True, "v": {"refuted": False, "reason": ""}}
        # a low refutation's reason/proof must NOT count toward the green majorities
        assert iv.attest_reasons([sol_good, low_ref, canned], 3)["block"] is True
        assert iv.attest_proof([sol_good, low_ref, canned], ch, 3)["block"] is True

    def test_round8_canned_corroborator_blocked_end_to_end(self):
        # require_approvals alone would green (Sol + codex approve), but the
        # canned codex approval fails attest_reasons now that the dissenting
        # low-refutation no longer attests on its behalf.
        ch = "selftest-challenge"
        models = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
        canned_approve = {"ok": True, "v": {"refuted": False, "reason": "", "proof": f"{ch}-3"}}
        sol_good = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol",
                                      "proof": f"{ch}-7"}}
        low_ref = {"ok": True, "v": {"refuted": True, "confidence": "low",
                                     "reason": "substantive doubt here", "proof": f"{ch}-9"}}
        votes = [canned_approve, sol_good, low_ref]
        assert iv.require_approvals(votes, models, "gpt-5.6-sol", 1, ch)["block"] is False
        assert iv.attest_reasons(votes, 3)["block"] is True   # the conjunctive gate catches it


class TestRiskOrderedReviewPacking:
    """PR #23 finding: `git diff` emits paths ALPHABETICALLY, so bulk
    `audit/*.json` consumed the body budget and the panel approved the mandate
    gate having never seen it ("gate implementation truncated"). Review order
    must be risk-driven, and any omission must stay visible."""

    def test_gate_and_code_outrank_docs_and_data(self):
        # ORDERING invariants, not absolute ranks (ranks get renumbered as
        # classes are inserted; the relations are what the gate depends on)
        r = iv.path_risk
        assert r("scripts/mandate_gate.py") == 0 == r(".github/workflows/ci.yml")
        # the law + attestations outrank app code, which outranks tests
        assert r("governance/mandate/manifest.json") < r("app/services/compute.py")
        assert r("governance/constitution.md") < r("app/services/compute.py")
        assert r("app/services/compute.py") < r("tests/test_x.py")
        assert r("frozen_methodology.json") < r("tests/test_x.py")
        # docs/records rank last — they may be truncated, code may not
        assert r("audit/03-findings.json") > r("app/x.py")
        assert r("README.md") > r("scripts/x.py")
        # governance prose still ranks above ordinary records

    def test_is_code_distinguishes_real_gaps_from_records(self):
        assert iv.is_code("scripts/mandate_gate.py")
        assert iv.is_code("app/db.py")
        assert iv.is_code("tests/test_y.py")
        assert not iv.is_code("audit/03-findings.json")
        assert not iv.is_code("docs/GOVERNANCE_FREEZE_RULE.md")

    def test_high_risk_file_lands_in_first_chunk_despite_alphabetical_order(self):
        # the exact failure: a huge 'audit/...' file sorts BEFORE 'scripts/...'
        files = [("audit/03-findings.json", "A" * 900),
                 ("scripts/mandate_gate.py", "GATEBODY")]
        chunks, omitted = iv.pack_by_risk(files, budget=1000, max_chunks=2)
        assert "GATEBODY" in chunks[0]
        assert omitted == []

    def test_oversized_NON_control_file_is_truncated_with_marker(self):
        files = [("docs/huge.md", "X" * 5000)]
        chunks, omitted = iv.pack_by_risk(files, budget=1000, max_chunks=2)
        assert omitted == []
        assert "TRUNCATED" in chunks[0]          # the cut is visible

    def test_oversized_CONTROL_file_is_reported_omitted_not_truncated(self):
        # found independently by this repo's own sweep and by the panel
        # (round 20): truncating a control file in place left its tail unread
        # while the run greened, because only `omitted` reaches the gate
        files = [("scripts/huge_gate.py", "X" * 5000)]
        chunks, omitted = iv.pack_by_risk(files, budget=1000, max_chunks=2)
        assert omitted == ["scripts/huge_gate.py"]
        assert "X" * 5000 not in "".join(chunks)

    def test_chunk_cap_bounds_cost_and_reports_what_it_dropped(self):
        # cost control: never unbounded fan-out; whatever does not fit is
        # returned as omitted rather than silently discarded
        files = [(f"app/mod{i}.py", "Y" * 800) for i in range(10)]
        chunks, omitted = iv.pack_by_risk(files, budget=1000, max_chunks=2)
        assert len(chunks) == 2
        assert omitted                            # the rest is accounted for
        assert all(p.startswith("app/") for p in omitted)

    def test_everything_fits_stays_a_single_chunk(self):
        # the common case must NOT multiply panel cost
        files = [("app/a.py", "a" * 100), ("audit/b.json", "b" * 100)]
        chunks, omitted = iv.pack_by_risk(files, budget=10_000, max_chunks=2)
        assert len(chunks) == 1
        assert omitted == []


class TestChunkingAdversarialFixes:
    """Findings from the internal adversarial pass on the chunking change
    itself (2026-07-25). Each test pins a hole that pass opened or found."""

    def test_quoted_and_non_ascii_paths_are_parsed(self):
        # CRITICAL: without -z, git C-quotes these and the per-file diff
        # matched nothing — the file was reviewed by nobody, silently.
        raw = "A\0app/café_backdoor.py\0M\0app/normal.py\0"
        assert iv.parse_name_status_z(raw) == ["app/café_backdoor.py",
                                               "app/normal.py"]

    def test_rename_records_yield_the_new_path(self):
        raw = "R089\0scripts/old_gate.py\0scripts/new_gate.py\0M\0app/x.py\0"
        assert iv.parse_name_status_z(raw) == ["scripts/new_gate.py", "app/x.py"]

    def test_truncated_record_does_not_invent_a_path(self):
        assert iv.parse_name_status_z("R100\0only_old_path\0") == []
        assert iv.parse_name_status_z("") == []

    def test_is_code_covers_executable_content_outside_app_and_scripts(self):
        # HIGH: tier-based is_code() called these "not code", so dropping them
        # produced no warning at all — a backdoor in conftest.py runs in CI.
        for path in ("conftest.py", "Makefile", "Dockerfile", "alembic.ini",
                     "docs/harnesses/alfred_vintage_harness.py",
                     "deploy/deploy-watch.sh", "r/gsadf.R",
                     "deploy/systemd/bubblegauge.service", "evil.py"):
            assert iv.is_code(path), path
        for path in ("README.md", "audit/03-findings.json", "docs/NOTES.md"):
            assert not iv.is_code(path), path

    def test_capped_packing_never_admits_lower_risk_over_higher(self):
        # HIGH: the gate file that missed the last chunk was dropped while a
        # small tests/ file that happened to fit was admitted.
        files = [("scripts/a_gate.py", "A" * 49_000),
                 ("scripts/b_gate.py", "B" * 49_000),
                 ("scripts/c_mandate_gate.py", "C" * 49_000),
                 ("app/tiny.py", "d" * 500),
                 ("tests/t.py", "e" * 400)]
        chunks, omitted = iv.pack_by_risk(files, budget=50_000, max_chunks=2)
        body = "\n".join(chunks)
        assert "scripts/c_mandate_gate.py" in omitted
        # once the cap forces an omission, nothing lower-risk sneaks in
        assert "d" * 500 not in body
        assert "e" * 400 not in body
        assert set(omitted) == {"scripts/c_mandate_gate.py", "app/tiny.py",
                                "tests/t.py"}

    def test_excluded_content_only_change_still_gets_a_payload(self):
        # CRITICAL: per-file assembly made an all-excluded PR produce zero
        # payloads -> "No diff to review. Green." with no votes at all.
        payload = iv.header_only("A\tdata/seed.sql\nA\tdata/config.csv\n",
                                 " data/seed.sql | 10 +\n")
        assert "data/seed.sql" in payload
        assert "NONE SHOWN" in payload
        assert "judge the change from the paths" in payload


class TestControlBearingCoverage:
    """Panel findings on the chunking change itself (PR #23, part 1): omitted
    non-code CONTROL files and privacy-excluded code were both discarded with
    no warning and no block."""

    def test_law_and_gate_config_are_control_bearing(self):
        for path in ("governance/constitution.md",
                     "governance/accepted-residuals.json",
                     ".github/CODEOWNERS", ".github/workflows/ci.yml",
                     "scripts/mandate_gate.py", "conftest.py"):
            assert iv.is_control_bearing(path), path

    def test_no_path_is_exempt_from_control_bearing_review(self):
        # the exemption is gone; law is control-bearing whatever its status
        for path in ("governance/mandate.md", "governance/mandate/part1.md"):
            assert iv.is_control_bearing(path, "A"), path
            assert iv.is_control_bearing(path, "M"), path

    def test_ordinary_docs_and_records_are_not_control_bearing(self):
        for path in ("README.md", "audit/03-findings.json", "docs/NOTES.md"):
            assert not iv.is_control_bearing(path), path


class TestNoReviewExemptionsExist:
    """The mandate text exemption was removed entirely: the 556KB spec is no
    longer committed here (manifest mandate_text_status), so every
    control-bearing path is reviewed or blocks, with no carve-outs."""

    def test_module_defines_no_exemption_list(self):
        assert not hasattr(iv, "_IMMUTABLE_TEXT")

    def test_governance_paths_are_control_bearing_at_every_status(self):
        for status in ("A", "M", "D", "R100"):
            assert iv.is_control_bearing("governance/mandate.md", status)
            assert iv.is_control_bearing("governance/constitution.md", status)
            assert iv.is_control_bearing("governance/mandate/part1.md", status)


class TestControlDataFiles:
    """Panel finding (PR #23 round 7): 'it's only JSON, not code' let the
    scored artifact and the gate's own thresholds be omitted from review with
    no deterministic block."""

    def test_scored_artifact_and_thresholds_are_control_bearing(self):
        for path in ("frozen_methodology.json", "audit/ratchet-baselines.json",
                     ".secrets.baseline"):
            assert iv.is_control_bearing(path, "M"), path

    def test_control_data_is_ranked_for_early_review(self):
        # small files that must never be crowded out by bulk records
        for path in ("frozen_methodology.json", "audit/ratchet-baselines.json"):
            assert iv.path_risk(path) < iv.path_risk("app/x.py"), path
            assert iv.path_risk(path) < iv.path_risk("audit/03-findings.json")

    def test_bulk_records_rely_on_the_deterministic_gate_not_the_panel(self):
        # documented scope: these carry no panel-review requirement because
        # mandate_gate validates them semantically on every run (set equality,
        # canonical verdicts, standing controls, computed-status drift)
        for path in ("audit/03-findings.json", "audit/engagement-status.json",
                     "audit/00-check-catalogue.json"):
            assert not iv.is_control_bearing(path, "M"), path

    def test_a_lowered_ratchet_floor_would_be_reviewed(self):
        # the concrete attack: measured >= lowered floor still satisfies the
        # ratchet, so only a reader catches it — hence it must reach the panel
        assert iv.is_control_bearing("audit/ratchet-baselines.json", "M")


class TestChunkCapClamp:
    """The cap is the direct cost lever: each part is a full panel pass."""

    def test_full_coverage_is_reachable_for_a_large_governance_pr(self, tmp_path):
        # operator decision 2026-07-25: the ceiling moved 6 -> 8 because the
        # panel refuses partial coverage, and a PR of this size needs more
        # than 6 parts to show every file.
        files = [(f"app/mod{i}.py", "Y" * 49_000) for i in range(8)]
        chunks, omitted = iv.pack_by_risk(files, budget=50_000, max_chunks=8)
        assert len(chunks) == 8
        assert omitted == []

    def test_cap_still_bounds_cost(self):
        files = [(f"app/mod{i}.py", "Y" * 49_000) for i in range(20)]
        chunks, omitted = iv.pack_by_risk(files, budget=50_000, max_chunks=8)
        assert len(chunks) == 8          # never unbounded fan-out
        assert omitted                    # the rest is accounted, not hidden


class TestRenameLaundering:
    """Panel finding (PR #23 round 10): keeping only the NEW path of a rename
    let a control file be laundered out of its classification, so budget
    eviction dropped the REMOVAL of a gate with no coverage block."""

    def test_rename_origins_are_recovered(self):
        raw = ("R100\0scripts/mandate_gate.py\0docs/old_notes.txt\0"
               "M\0app/x.py\0A\0app/y.py\0")
        assert iv.parse_rename_origins_z(raw) == {
            "docs/old_notes.txt": "scripts/mandate_gate.py"}

    def test_non_rename_records_have_no_origin(self):
        assert iv.parse_rename_origins_z("M\0app/x.py\0A\0app/y.py\0") == {}

    def test_truncated_rename_record_is_ignored(self):
        assert iv.parse_rename_origins_z("R100\0only_old\0") == {}

    def test_gate_renamed_to_docs_is_still_control_bearing(self):
        # the laundering attack: the destination alone looks harmless
        assert not iv.is_control_bearing("docs/old_notes.txt", "R100")
        assert iv.is_control_bearing("scripts/mandate_gate.py", "R100")

    def test_renamed_control_file_is_ranked_by_its_strongest_endpoint(self):
        # a renamed gate must still be reviewed EARLY, not sink to docs rank
        assert iv.path_risk("scripts/mandate_gate.py") < iv.path_risk("docs/old_notes.txt")
        strongest = min(iv.path_risk("docs/old_notes.txt"),
                        iv.path_risk("scripts/mandate_gate.py"))
        assert strongest == iv.path_risk("scripts/mandate_gate.py")

    def test_pack_by_risk_honours_a_custom_rank_function(self):
        files = [("docs/old_notes.txt", "LAUNDERED" * 100),
                 ("audit/bulk.json", "B" * 900)]
        # without the override the docs path sorts by its own (low) risk
        chunks, _ = iv.pack_by_risk(files, budget=1000, max_chunks=2,
                                    risk_of=lambda p: 0 if "old_notes" in p else 9)
        assert "LAUNDERED" in chunks[0]


class TestCodeClassifierBreadth:
    """Panel round 15 + own sweep: the classifier missed extensionless files,
    most common languages, systemd units, and matched suffixes case-sensitively
    while the privacy excludes are icase — so `.SQL` was excluded from the body
    AND from the coverage block simultaneously."""

    @pytest.mark.parametrize("path", [
        "deploy/systemd/bubblegauge-deploy.path",   # arms host auto-deploy
        "deploy/systemd/bubblegauge.service",
        "scripts/entrypoint",                       # extensionless script
        "hooks/pre-commit",
        "app/migrate.SQL",                          # icase
        "web/app.ts", "web/app.js", "svc/main.go", "lib/thing.rb",
        "lib/thing.rs", "src/Main.java", "infra/main.tf", "build.gradle",
    ])
    def test_executable_and_automation_paths_are_code(self, path):
        assert iv.is_code(path), path

    @pytest.mark.parametrize("path", [
        "LICENSE", "README", "CHANGELOG", "NOTICE", "VERSION",
        "docs/NOTES.md", "audit/03-findings.json",
    ])
    def test_text_artifacts_are_not_code(self, path):
        assert not iv.is_code(path), path

    def test_extensionless_defaults_to_code_fail_closed(self):
        # an unknown extensionless file must be treated as code, not as prose
        assert iv.is_code("bin/some-new-entrypoint")
        assert iv.is_control_bearing("bin/some-new-entrypoint", "A")


class TestExecutableManifests:
    """Panel round 19: `.json` reads as data, but an executable manifest runs
    code at install/build time — a package.json lifecycle-script edit could be
    omitted from review with no coverage block."""

    @pytest.mark.parametrize("path", [
        "package.json", "frontend/package.json", "package-lock.json",
        "yarn.lock", "pnpm-lock.yaml", "Gemfile", "Cargo.toml", "go.mod",
        "poetry.lock", "Procfile", "renovate.json", ".npmrc",
    ])
    def test_executable_manifests_are_control_bearing(self, path):
        assert iv.is_code(path), path
        assert iv.is_control_bearing(path, "M"), path

    @pytest.mark.parametrize("path", ["docs/data.json", "audit/03-findings.json"])
    def test_ordinary_json_is_still_data(self, path):
        assert not iv.is_code(path), path


class TestPartBudget:
    """The per-part budget must exceed the LARGEST control file's diff, not
    the average: a control file above it cannot be reviewed in full."""

    def test_default_budget_exceeds_this_repos_largest_control_file(self):
        # regression: at 50k, tests/test_mandate_gate.py (54k) blocked
        assert iv.DEFAULT_PART_BUDGET >= 90_000

    def test_budget_is_env_tunable_within_sane_bounds(self, monkeypatch):
        import importlib
        for raw, expected in (("", iv.DEFAULT_PART_BUDGET),
                              ("nonsense", iv.DEFAULT_PART_BUDGET),
                              ("5", iv.DEFAULT_PART_BUDGET),        # too small
                              ("999999", iv.DEFAULT_PART_BUDGET),   # too large
                              ("120000", 120000)):
            try:
                pb = int(raw)
                got = pb if 20_000 <= pb <= 200_000 else iv.DEFAULT_PART_BUDGET
            except ValueError:
                got = iv.DEFAULT_PART_BUDGET
            assert got == expected, raw
        assert importlib


class TestRenamedOversizeControl:
    """Round 22: the oversized-control check used the NEW path only, so a
    renamed control file above budget was truncated rather than omitted."""

    def test_control_classifier_is_injectable_for_rename_origins(self):
        files = [("docs/old_notes.txt", "X" * 5000)]
        # destination alone looks harmless -> truncated
        chunks, omitted = iv.pack_by_risk(files, budget=1000, max_chunks=2)
        assert omitted == []
        # with origin awareness the same file is a control -> omitted
        chunks, omitted = iv.pack_by_risk(
            files, budget=1000, max_chunks=2,
            control_of=lambda p: p == "docs/old_notes.txt")
        assert omitted == ["docs/old_notes.txt"]
        assert "X" * 5000 not in "".join(chunks)
