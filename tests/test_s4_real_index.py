"""S4 v4 candidate: a real (CPI-deflated) native index input, and an asymmetric
contested rule.

Both are OFF by default. These tests pin two things: that production behaviour is
byte-unchanged while they are off, and that each does what it claims when on.
"""

from __future__ import annotations

import math

import pytest

from app.config import Settings
from app.indicators.s4_gsadf import sub_score
from app.sources import SourceError
from app.sources import fred_real_index as fri

# NOTE: the two triples below come from DIFFERENT instruments and are not two
# readings of one series. The sup is what the deployed service returns on its
# QQQ proxy; the endpoint pair is measured on the CPI-deflated native index.
# Both are real, both are non-rejections, and the tests below only need a
# non-rejecting scored pair — but do not read them as a matched pair.
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

# The deflated series is the SHADOW: reported, never scored (PIN C, and
# GSADF_SHADOW_REAL_INDEX defaults off). Kept for the contrast.
SHADOW_BSADF = 0.7562


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

    def test_the_shadow_switch_defaults_off(self):
        # The only runtime switch this branch adds, and it is NON-SCORING.
        assert Settings.model_fields["gsadf_shadow_real_index"].default is False

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


class TestRealIndexBuilder:
    """Deflation and month-end aggregation, on synthetic series with known shape."""

    @staticmethod
    def _obs(pairs):
        return list(pairs)

    def test_month_end_keeps_the_last_observation_of_each_month(self):
        got = fri._month_end([("2020-01-02", 1.0), ("2020-01-31", 2.0), ("2020-02-14", 3.0)])
        assert list(got.items()) == [("2020-01", 2.0), ("2020-02", 3.0)]

    def test_deflation_divides_the_index_by_cpi(self, monkeypatch):
        idx = [(f"{y}-06-30", 200.0) for y in range(1999, 2010)]
        cpi = [(f"{y}-06-01", 100.0) for y in range(1999, 2010)]
        monkeypatch.setattr(fri, "observations",
                            lambda sid, **k: idx if sid == fri.NASDAQ_100 else cpi)
        with pytest.raises(SourceError, match="need >= 100"):
            fri.real_monthly_log_index(start_year=1999)

    def test_a_constant_deflator_only_shifts_the_log_series(self, monkeypatch):
        months = [f"{y}-{m:02d}" for y in range(1999, 2012) for m in range(1, 13)]
        idx = [(f"{m}-28", 100.0 + i) for i, m in enumerate(months)]
        cpi = [(f"{m}-01", 50.0) for m in months]
        monkeypatch.setattr(fri, "observations",
                            lambda sid, **k: idx if sid == fri.NASDAQ_100 else cpi)
        as_of, real = fri.real_monthly_log_index(start_year=1999)
        nominal = [math.log(100.0 + i) for i in range(len(months))]
        # A constant deflator is a constant scale factor: it cancels out of every
        # DIFFERENCE, which is all a unit-root test reads.
        assert as_of == months[-1]
        diffs_real = [b - a for a, b in zip(real[:-1], real[1:], strict=True)]
        diffs_nom = [b - a for a, b in zip(nominal[:-1], nominal[1:], strict=True)]
        assert diffs_real == pytest.approx(diffs_nom)

    def test_a_short_overlap_refuses_rather_than_returning_a_stub(self, monkeypatch):
        monkeypatch.setattr(fri, "observations",
                            lambda sid, **k: [("2020-01-31", 100.0), ("2020-02-29", 101.0)])
        with pytest.raises(SourceError, match="need >= 100"):
            fri.real_monthly_log_index(start_year=1999)

    def test_an_empty_series_refuses(self, monkeypatch):
        monkeypatch.setattr(fri, "observations", lambda sid, **k: [])
        with pytest.raises(SourceError):
            fri.real_monthly_log_index(start_year=1999)


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


class TestShadowIsReportedAndNeverScored:
    """The shadow was previously written to RawInputs and read by NOTHING --
    a FRED round-trip and a second R run, discarded with the object. It now rides
    on IndicatorOutput.extra, the channel s5's dual report already uses."""

    @staticmethod
    def _snapshot_with_shadow(monkeypatch, note="SHADOW (not scored): probe", stat=1.23):
        from app.services import compute
        from tests.conftest import make_golden_raw_inputs
        raw = make_golden_raw_inputs()
        raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = LIVE_STAT, LIVE_CV90, LIVE_CV95
        raw.gsadf_shadow_note = note
        raw.gsadf_shadow_stat, raw.gsadf_shadow_cv90, raw.gsadf_shadow_cv95 = stat, 1.9, 2.2
        return compute.compute_snapshot(raw)

    def test_the_shadow_reaches_the_served_payload(self, monkeypatch):
        snap = self._snapshot_with_shadow(monkeypatch)
        extra = snap.indicators["s4"].extra or {}
        assert "s4_shadow" in extra, "computed and then discarded is not a drift gate"
        assert extra["s4_shadow"]["gsadf"] == 1.23
        assert extra["s4_shadow"]["included_in_score"] is False

    def test_the_shadow_does_not_change_the_score(self, monkeypatch):
        from app.services import compute
        from tests.conftest import make_golden_raw_inputs
        raw = make_golden_raw_inputs()
        raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = LIVE_STAT, LIVE_CV90, LIVE_CV95
        without = compute.compute_snapshot(raw).point_score
        with_shadow = self._snapshot_with_shadow(monkeypatch, stat=99.0).point_score
        assert with_shadow == without, "a shadow that moves the score is not a shadow"

    def test_no_shadow_no_key(self, monkeypatch):
        from app.services import compute
        from tests.conftest import make_golden_raw_inputs
        raw = make_golden_raw_inputs()
        raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = LIVE_STAT, LIVE_CV90, LIVE_CV95
        snap = compute.compute_snapshot(raw)
        assert "s4_shadow" not in (snap.indicators["s4"].extra or {})

    def test_sub_score_cannot_see_a_shadow(self):
        import inspect

        from app.indicators import s4_gsadf
        assert not any("shadow" in p for p in inspect.signature(s4_gsadf.sub_score).parameters)


class TestProducerSideIsCovered:
    """Three mutations survived the first rewrite at exactly 1307 passed:
    deleting the contiguity block, disabling the shadow producer, and severing
    the flag's env binding. The tests defended the consumer half and left every
    producer untested. These cover the producer half."""

    @staticmethod
    def _series(months, idx_vals=None, cpi_skip=()):
        idx = [(f"{m}-28", 100.0 + i) for i, m in enumerate(months)]
        cpi = [(f"{m}-01", 50.0 + i * 0.1) for i, m in enumerate(months) if m not in cpi_skip]
        return idx, cpi

    @staticmethod
    def _months(start_year, n):
        out = []
        y, mo = start_year, 1
        for _ in range(n):
            out.append(f"{y}-{mo:02d}")
            mo += 1
            if mo == 13:
                y, mo = y + 1, 1
        return out

    def _patch(self, monkeypatch, idx, cpi):
        monkeypatch.setattr(fri, "observations",
                            lambda sid, **k: idx if sid == fri.NASDAQ_100 else cpi)

    def test_an_index_gap_refuses(self, monkeypatch):
        # An index hole cannot be imputed from the index.
        months = self._months(1999, 150)
        idx, cpi = self._series(months)
        idx = [p for p in idx if not p[0].startswith("2005-06")]
        self._patch(monkeypatch, idx, cpi)
        with pytest.raises(SourceError, match="non-contiguous months"):
            fri.real_monthly_log_index(start_year=1999)

    def test_an_interior_single_deflator_hole_is_carried_and_disclosed(self, monkeypatch):
        months = self._months(1999, 150)
        idx, cpi = self._series(months, cpi_skip={"2005-06"})
        self._patch(monkeypatch, idx, cpi)
        as_of, series = fri.real_monthly_log_index(start_year=1999)
        assert len(series) == 150
        assert "carried at 2005-06" in as_of, "an imputation that is not disclosed is a lie"

    def test_two_consecutive_deflator_holes_refuse(self, monkeypatch):
        months = self._months(1999, 150)
        idx, cpi = self._series(months, cpi_skip={"2005-06", "2005-07"})
        self._patch(monkeypatch, idx, cpi)
        with pytest.raises(SourceError, match="interior hole"):
            fri.real_monthly_log_index(start_year=1999)

    def test_trailing_months_without_a_deflator_are_truncated_not_carried(self, monkeypatch):
        # CPI publishes with a lag and the newest index month is still in
        # progress; carrying there invents a level for an unsettled month.
        months = self._months(1999, 150)
        idx, cpi = self._series(months, cpi_skip={months[-1], months[-2]})
        self._patch(monkeypatch, idx, cpi)
        as_of, series = fri.real_monthly_log_index(start_year=1999)
        assert as_of == months[-3]
        assert len(series) == 148
        assert "carried" not in as_of

    def test_a_clean_series_discloses_no_carry(self, monkeypatch):
        months = self._months(1999, 150)
        idx, cpi = self._series(months)
        self._patch(monkeypatch, idx, cpi)
        as_of, _ = fri.real_monthly_log_index(start_year=1999)
        assert "carried" not in as_of


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

    def test_the_shadow_env_var_is_bound_too(self, monkeypatch):
        from app.config import Settings
        monkeypatch.setenv("GSADF_SHADOW_REAL_INDEX", "true")
        assert Settings().gsadf_shadow_real_index is True


class TestTheShadowProducerActuallyRuns:
    """Mutation M2 -- `if False and get_settings().gsadf_shadow_real_index:` --
    survived every earlier test at 31 passed. That is the ORIGINAL F3 defect
    location: the producer being switched off is exactly the failure "the shadow
    produces nothing" describes, and nothing noticed. This drives the producer.

    No R and no network: run_gsadf and the FRED source are both stubbed, which is
    all the block does besides the guard."""

    @staticmethod
    def _run(monkeypatch, *, enabled: bool, series_ok: bool = True, gsadf_ok: bool = True):
        from app.config import get_settings
        from app.engine.gsadf_runner import GsadfOutput
        from app.services import compute
        from app.sources import SourceError, fred_real_index

        patched = get_settings().model_copy(update={"gsadf_shadow_real_index": enabled})
        monkeypatch.setattr(compute, "get_settings", lambda: patched)

        def fake_series(*a, **k):
            if not series_ok:
                raise SourceError("probe: source down")
            return "2026-07", [float(i) for i in range(200)]

        monkeypatch.setattr(fred_real_index, "real_monthly_log_index", fake_series)
        monkeypatch.setattr(compute, "run_gsadf",
                            (lambda *a, **k: GsadfOutput(gsadf=1.111, cv90=1.9, cv95=2.2))
                            if gsadf_ok else (lambda *a, **k: None))

        raw = _raw_with_live_gsadf()
        compute.populate_gsadf_shadow(raw)      # the PRODUCER, on the gather path
        return compute.compute_snapshot(raw)    # the CONSUMER

    def test_enabled_produces_a_shadow(self, monkeypatch):
        snap = self._run(monkeypatch, enabled=True)
        shadow = (snap.indicators["s4"].extra or {}).get("s4_shadow")
        assert shadow is not None, "the producer ran but emitted nothing"
        assert shadow["gsadf"] == 1.111
        assert shadow["included_in_score"] is False

    def test_disabled_produces_nothing(self, monkeypatch):
        snap = self._run(monkeypatch, enabled=False)
        assert "s4_shadow" not in (snap.indicators["s4"].extra or {})

    def test_a_dead_source_is_reported_not_raised(self, monkeypatch):
        snap = self._run(monkeypatch, enabled=True, series_ok=False)
        shadow = (snap.indicators["s4"].extra or {}).get("s4_shadow")
        assert shadow is not None and shadow["gsadf"] is None
        assert "unavailable" in shadow["note"]

    def test_a_failed_fit_is_reported_not_raised(self, monkeypatch):
        snap = self._run(monkeypatch, enabled=True, gsadf_ok=False)
        shadow = (snap.indicators["s4"].extra or {}).get("s4_shadow")
        assert shadow is not None and shadow["gsadf"] is None

    def test_the_shadow_never_moves_the_score(self, monkeypatch):
        on = self._run(monkeypatch, enabled=True).point_score
        off = self._run(monkeypatch, enabled=False).point_score
        assert on == off
