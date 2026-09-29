"""S4 v4 candidate: an asymmetric contested rule.

The real (CPI-deflated) index SHADOW that used to live beside it is gone (owner
decision D4, 2026-09-28). These tests pin that production behaviour is unchanged
by the rule's existence, and that it does what it claims when enabled in code.
"""

from __future__ import annotations

import math

from app.config import Settings
from app.indicators.s4_gsadf import sub_score

# NOTE: the two triples below come from DIFFERENT instruments and are not two
# readings of one series. Both are real, both are non-rejections, and the tests
# below only need a non-rejecting scored pair — but do not read them as a
# matched pair.
#
# The live GSADF sup and its critical values, from the deployed service. Since
# v4.0-s4-endpoint this pair is REPORTED, not scored.
LIVE_STAT, LIVE_CV90, LIVE_CV95 = 1.579, 1.9359, 2.2215

# The SCORED pair, on the SCORED INSTRUMENT. compute.py fetches the QQQ proxy and
# takes logs -- there is no deflation on the scored path -- so the endpoint must
# come from the NOMINAL series. Measured with exuber 1.1.0 (lag=0, nrep=2000,
# seed 20260711) on nominal native Nasdaq-100 monthly log levels, T=331.
#
# It is a non-rejection, but only just: 1.1315 against cv90 1.1393 is 0.7% of the
# critical value. An earlier version of this file used the CPI-DEFLATED endpoint
# (0.7562, a 34% margin) here and labelled it "the SCORED pair". That is the
# SHADOW instrument, and the mislabel propagated into the frozen artifact's
# record of the live reading before it was caught.
LIVE_BSADF, LIVE_BSADF_CV90, LIVE_BSADF_CV95 = 1.1315, 1.1393, 1.378

def _raw_with_live_gsadf():
    from tests.conftest import make_golden_raw_inputs
    raw = make_golden_raw_inputs()
    raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = LIVE_STAT, LIVE_CV90, LIVE_CV95
    raw.bsadf_stat, raw.bsadf_cv90, raw.bsadf_cv95 = (
        LIVE_BSADF, LIVE_BSADF_CV90, LIVE_BSADF_CV95)
    raw.gsadf_as_of = "2026-08"
    return raw


class TestDefaultsAreProductionBehaviour:
    """The asymmetric rule is now SHIPPED (v4.1, gsadf.contested_rule), but it is
    still not reachable by configuration — it moved by ceremony, which is the
    property these tests defend."""

    def test_there_is_no_runtime_switch_for_the_scored_rule(self):
        assert "gsadf_contested_asymmetric" not in Settings.model_fields, (
            "the asymmetric rule is a code change under the v4 ceremony, never a flag")

    def test_contested_still_defaults_on(self):
        assert Settings.model_fields["gsadf_contested"].default is True

    def test_the_rule_is_a_frozen_constant_not_a_setting(self):
        from app import methodology as _M
        assert _M.get_path("gsadf", "contested_rule") == "asymmetric"
        assert not [f for f in Settings.model_fields if "contested_rule" in f.lower()]

    def test_the_mapping_itself_is_unchanged(self):
        # sub_score's own default is still the cap; v4.1 changed what the CALL
        # SITE passes, not the function's behaviour.
        assert sub_score(LIVE_STAT, LIVE_CV90, LIVE_CV95, contested=True) == 0.25


class TestAsymmetricContested:
    """The over-rejection critique bounds FALSE POSITIVES; it says nothing about
    a non-rejection. The asymmetric rule lets exactly that case through."""

    def test_a_non_rejection_passes_when_enabled(self):
        assert sub_score(LIVE_STAT, LIVE_CV90, LIVE_CV95,
                         contested=True, asymmetric=True) == 0.05

    def test_a_rejection_is_still_capped(self):
        # Where the critique DOES bite, the cap stays.
        assert sub_score(2.5, LIVE_CV90, LIVE_CV95, contested=True, asymmetric=True) == 0.25
        assert sub_score(2.0, LIVE_CV90, LIVE_CV95, contested=True, asymmetric=True) == 0.25

    def test_the_boundary_is_cv90_not_cv95(self):
        just_under = LIVE_CV90 - 1e-9
        just_over = LIVE_CV90 + 1e-9
        assert sub_score(just_under, LIVE_CV90, LIVE_CV95, contested=True, asymmetric=True) == 0.05
        assert sub_score(just_over, LIVE_CV90, LIVE_CV95, contested=True, asymmetric=True) == 0.25

    def test_stale_still_caps_regardless(self):
        # Staleness is about data age, not the test's size properties.
        assert sub_score(LIVE_STAT, LIVE_CV90, LIVE_CV95,
                         contested=True, stale=True, asymmetric=True) == 0.25

    def test_degenerate_inputs_still_floor(self):
        assert sub_score(None, None, None, contested=True, asymmetric=True) == 0.25
        assert sub_score(math.nan, LIVE_CV90, LIVE_CV95, contested=True, asymmetric=True) == 0.25
        # cv90 >= cv95 is a degenerate simulation and must never reach a comparison.
        assert sub_score(LIVE_STAT, 2.5, 2.0, contested=True, asymmetric=True) == 0.25

    def test_the_uncontested_ladder_is_untouched(self):
        assert sub_score(2.5, LIVE_CV90, LIVE_CV95, contested=False) == 1.0
        assert sub_score(2.0, LIVE_CV90, LIVE_CV95, contested=False) == 0.5
        assert sub_score(LIVE_STAT, LIVE_CV90, LIVE_CV95, contested=False) == 0.05


class TestBothSwitchesAreActuallyWired:
    """combo/SOTA-A refuted the first version of this branch with "both S4
    switches are unwired; enabled values cannot affect production". It was right.

    The FIRST fix asserted the wiring by inspecting source text. That did not
    hold either: an adversarial review inserted
    `s4_sub = max(s4_sub, SUB_CONTESTED_OR_STALE)` immediately after the call
    site -- killing the switch while leaving the asserted substring intact -- and
    the whole suite stayed at 1305 passed. These tests drive compute_snapshot
    instead, so behaviour is what is pinned.

    No R and no network are needed: R only supplies the statistic/CV triples,
    which a test can hand over directly."""

    LIVE = (LIVE_STAT, LIVE_CV90, LIVE_CV95)

    @staticmethod
    def _raw_with_live_gsadf():
        from tests.conftest import make_golden_raw_inputs
        raw = make_golden_raw_inputs()
        raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = LIVE_STAT, LIVE_CV90, LIVE_CV95
        raw.gsadf_as_of = "2026-08"
        return raw

    @staticmethod
    def _snapshot(monkeypatch, *, asymmetric: bool):
        """Drives compute_snapshot through the real call site. `asymmetric` is a
        PARAMETER of sub_score, not a setting -- activation is a code change under
        the v4 ceremony -- so the on-case is exercised by patching the call the
        way that code change would make it."""
        from app.indicators import s4_gsadf
        from app.services import compute

        if asymmetric:
            real = s4_gsadf.sub_score
            monkeypatch.setattr(
                compute.s4_gsadf, "sub_score",
                lambda *a, **k: real(*a, **{**k, "asymmetric": True}))
        return compute.compute_snapshot(_raw_with_live_gsadf())

    def test_reverting_the_rule_restores_the_cap(self, monkeypatch):
        """The constant is what moves it: flipping it back must restore 0.25, so
        a silent revert of the artifact cannot pass unnoticed. Patched on the
        module now — the artifact is read at import, so a mid-process rewrite is
        no longer a thing that can happen."""
        from app.indicators import s4_gsadf as _s4
        monkeypatch.setattr(_s4, "CONTESTED_RULE", "symmetric")
        monkeypatch.setattr(_s4, "ASYMMETRIC_CONTESTED", False)
        snap = self._snapshot(monkeypatch, asymmetric=False)
        assert snap.indicators["s4"].sub_score == 0.25

    def test_the_rule_lowers_the_headline_at_the_live_reading(self, monkeypatch):
        from app.indicators import s4_gsadf as _s4
        on = self._snapshot(monkeypatch, asymmetric=False).point_score
        monkeypatch.setattr(_s4, "CONTESTED_RULE", "symmetric")
        monkeypatch.setattr(_s4, "ASYMMETRIC_CONTESTED", False)
        off = self._snapshot(monkeypatch, asymmetric=False).point_score
        # The contested floor sat ABOVE what the test returned, so releasing a
        # non-rejection LOWERS the headline. Measured: 53.30 -> 51.82.
        assert on < off, (on, off)
        assert round(on - off, 2) == -1.48, (on, off)

    def test_the_shipped_rule_scores_what_the_test_returned(self, monkeypatch):
        # v4.1: no patching. This is the production path.
        snap = self._snapshot(monkeypatch, asymmetric=False)
        assert snap.indicators["s4"].sub_score == 0.05

    def test_a_rejection_is_still_capped_end_to_end(self, monkeypatch):
        from app.indicators import s4_gsadf
        from app.services import compute
        real = s4_gsadf.sub_score
        monkeypatch.setattr(compute.s4_gsadf, "sub_score",
                            lambda *a, **k: real(*a, **{**k, "asymmetric": True}))
        raw = _raw_with_live_gsadf()
        raw.bsadf_stat = 2.5              # above the endpoint cv95: a rejection
        snap = compute.compute_snapshot(raw)
        assert snap.indicators["s4"].sub_score == 0.25


class TestTheEnvBindingIsLive:
    """A mutation that renamed the field's env alias left the suite green while
    GSADF_CONTESTED_ASYMMETRIC=true stopped reaching the setting. No test built
    Settings() from the environment -- model_fields reads the declaration and
    model_copy skips validators, so neither sees an alias change."""

    def test_no_env_var_can_move_a_scored_value(self, monkeypatch):
        """The panel refuted the runtime switch: "runtime flag changes frozen S4
        scoring without required v4 metadata/golden ceremony". Reproduced --
        GSADF_CONTESTED_ASYMMETRIC=true moved s4 0.25 -> 0.05 with the frozen
        SHA, methodology_version and golden all unchanged. The switch is gone;
        this pins that it stays gone."""
        from app.config import Settings
        from app.services import compute
        from tests.conftest import make_golden_raw_inputs

        monkeypatch.setenv("GSADF_CONTESTED_ASYMMETRIC", "true")
        settings = Settings()
        assert not hasattr(settings, "gsadf_contested_asymmetric"), (
            "a setting that moves a frozen scored value defeats the freeze")
        monkeypatch.setattr(compute, "get_settings", lambda: settings)
        raw = make_golden_raw_inputs()
        raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = LIVE_STAT, LIVE_CV90, LIVE_CV95
        assert compute.compute_snapshot(raw).indicators["s4"].sub_score == 0.25

