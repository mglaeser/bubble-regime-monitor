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
        ch = "abc123def456"
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
        ch = "abc123def456"
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
