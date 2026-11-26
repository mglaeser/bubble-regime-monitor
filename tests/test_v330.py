"""v3.3.0: rescale-then-aggregate + governance-hygiene regression tests."""

from __future__ import annotations

import pytest


class TestRescaleAggregation:
    def test_rescale_maps_to_floor_range(self):
        from app.engine.aggregate import RESCALE_FLOOR, rescale

        assert rescale(0.0) == pytest.approx(RESCALE_FLOOR)
        assert rescale(1.0) == pytest.approx(1.0)

    def test_zero_never_annihilates_block(self):
        # Defect-2 acceptance: exactly one input at 0 must never collapse the
        # block to 0 — it yields L^{w} of the all-else-equal value.
        from app.engine.aggregate import RESCALE_FLOOR, geometric_block

        w = {"a": 0.5, "b": 0.5}
        one_zero = geometric_block({"a": 0.0, "b": 1.0}, w)
        assert one_zero == pytest.approx(RESCALE_FLOOR**0.5, abs=1e-9)  # ~0.316, not 0
        all_zero = geometric_block({"a": 0.0, "b": 0.0}, w)
        assert all_zero == pytest.approx(RESCALE_FLOOR, abs=1e-9)       # block floor, not 0


class TestJudgmentCompletionGuard:
    def test_fragment_gets_terminal_punctuation(self):
        from app.engine.judgment import _clean_completion

        out = _clean_completion("Valuations are stretched but breadth is holding, leaving the band at")
        assert out[-1] in ".!?"

    def test_clean_sentence_passes_through(self):
        from app.engine.judgment import _clean_completion

        s = "Shares look pricey; broad participation is the main calming factor."
        assert _clean_completion(s) == s

    def test_multi_sentence_trims_to_boundary(self):
        from app.engine.judgment import _clean_completion

        long = ("A" * 250) + ". " + ("B" * 100) + " tail without a period"
        out = _clean_completion(long, 300)
        assert out[-1] in ".!?"
        assert len(out) <= 300


class TestPolygonBreadth:
    def test_parse_grouped_maps_ticker_close_and_dots(self):
        from app.sources.prices import parse_polygon_grouped

        payload = {"status": "OK", "results": [{"T": "AAPL", "c": 210.5},
                                               {"T": "BRK.B", "c": 470.0},
                                               {"T": "BAD"}]}  # missing close -> skipped
        out = parse_polygon_grouped(payload)
        assert out["AAPL"] == 210.5
        assert out["BRK-B"] == 470.0     # dot -> dash
        assert "BAD" not in out

    def test_parse_grouped_rejects_not_authorized(self):
        import pytest

        from app.sources.prices import NotOnPlan, parse_polygon_grouped

        with pytest.raises(NotOnPlan):
            parse_polygon_grouped({"status": "NOT_AUTHORIZED", "error": "upgrade plan"})

    def test_market_closed_day_is_empty_not_error(self):
        from app.sources.prices import parse_polygon_grouped

        # both shapes a closed day can take: empty results, and no results key
        assert parse_polygon_grouped({"status": "OK", "resultsCount": 0, "results": []}) == {}
        assert parse_polygon_grouped({"status": "OK", "resultsCount": 0}) == {}

    def test_breadth_from_daily_close_full_universe(self, isolated_db):
        from datetime import UTC, date, datetime, timedelta

        from app.db import session_scope
        from app.models import DailyClose
        from app.sources.breadth import _breadth_from_daily_close

        now = datetime.now(UTC)
        base = date(2026, 1, 1)
        # v3.7.7/§2.2b requires >=25 USABLE symbols to select a common date, so use
        # 13 rising (above SMA200) + 12 falling (below) = 25 usable, plus CCC (<200).
        with session_scope() as s:
            for n in range(13):
                for i in range(200):
                    s.merge(DailyClose(symbol=f"UP{n:02d}", date=base + timedelta(days=i),
                                       close=100.0 + i, provider="polygon", fetched_at=now))
            for n in range(12):
                for i in range(200):
                    s.merge(DailyClose(symbol=f"DN{n:02d}", date=base + timedelta(days=i),
                                       close=200.0 - i, provider="polygon", fetched_at=now))
            for i in range(50):
                s.merge(DailyClose(symbol="CCC", date=base + timedelta(days=i),
                                   close=100.0, provider="polygon", fetched_at=now))
        syms = [f"UP{n:02d}" for n in range(13)] + [f"DN{n:02d}" for n in range(12)] + ["CCC"]
        above, counted, obs = _breadth_from_daily_close(syms)
        assert counted == 25         # CCC excluded (<200 closes); 25 usable counted
        assert above == 13           # the 13 rising symbols are above their 200-DMA
        assert obs == base + timedelta(days=199)  # newest cached date (v3.7.3/B-02)

    def test_identification_bounds_widen_with_missing(self):
        # v3.7.8/B-05: the binomial CI is gone (missingness is not random).
        # Worst-case full-universe identification bounds instead: at full coverage
        # they collapse to the point value; more missing -> wider.
        from app.sources.breadth import _breadth_identification_bounds_pct

        full = _breadth_identification_bounds_pct(above=300, counted=503, universe=503)
        assert full == (pytest.approx(300 / 503 * 100), pytest.approx(300 / 503 * 100))
        lo, hi = _breadth_identification_bounds_pct(above=80, counted=137, universe=503)
        assert lo == pytest.approx(80 / 503 * 100)              # all missing below
        assert hi == pytest.approx((80 + 366) / 503 * 100)      # all missing above
        assert (hi - lo) > 0.0
        with pytest.raises(ValueError):
            _breadth_identification_bounds_pct(above=10, counted=5, universe=503)  # above>counted


class TestQualityWeightedCoverage:
    @staticmethod
    def _block_d(d1_quality, d4_dropped=False):
        from datetime import UTC, datetime

        from app.services.compute import IndicatorOutput, _coverage_gate

        # Fresh dates so this isolates the quality-WEIGHTING logic from the
        # v3.7.3/A-01 freshness gate (an undated indicator is now not-fresh).
        t = datetime.now(UTC).date().isoformat()
        inds = {
            "d1": IndicatorOutput("d1", 56.0, 0.6, False, "x", False, quality=d1_quality, as_of=t),
            "d2": IndicatorOutput("d2", 40.0, 0.5, False, "x", False, as_of=t),
            "d3": IndicatorOutput("d3", 1.0, 0.3, False, "x", False, as_of=t),
            "d4": IndicatorOutput("d4", None, None, True, "x", False) if d4_dropped
            else IndicatorOutput("d4", 0.1, 0.1, False, "x", False, as_of=t),
        }
        return _coverage_gate(inds)["D"]

    def test_full_universe_not_degraded(self):
        assert self._block_d(1.0)["degraded"] is False

    def test_partial_breadth_alone_barely_passes(self):
        # d1 from ~33/503 names alone: coverage ~= 0.35*0.066 + 0.65 = 0.673 (per
        # the review) — barely above the 2/3 gate.
        d = self._block_d(33 / 503)
        assert d["coverage"] == pytest.approx(0.673, abs=0.01)
        assert d["degraded"] is False

    def test_partial_breadth_plus_failed_lppls_degrades(self):
        # ...but with d4 also FLOOR (dropped) it falls below 2/3 -> degraded.
        d = self._block_d(33 / 503, d4_dropped=True)
        assert d["coverage"] < 0.667
        assert d["degraded"] is True


class TestS5TMinus2Lag:
    def test_t_minus_2_picks_lagged_or_oldest(self):
        from app.indicators import s5_credit

        # short history (< 2yr) -> fall back to the oldest observation
        assert s5_credit.t_minus_2_value([250.0, 260.0, 270.0]) == 250.0
        # long history -> the value ~2yr (504 obs) back
        series = [float(i) for i in range(600)]
        assert s5_credit.t_minus_2_value(series) == series[-505]

    def test_sub_score_t2_uses_lagged_not_contemporaneous(self):
        from app.indicators import s5_credit

        # today tight (250) but 2yr ago loose (600): fragility must reflect the
        # LOOSE t-2 spread (low), not today's tight spread (which reads high).
        hist = [600.0] + [500.0] * 10 + [250.0]
        t2 = s5_credit.sub_score_t2(hist)
        contemporaneous = s5_credit.sub_score(250.0, hist)
        assert t2 < contemporaneous


class TestS5BaaProxy:
    def test_baa_proxy_preferred_and_t_minus_2(self, isolated_db):
        from app.indicators import s5_credit
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        raw.baa_spread_history_bps = [float(150 + (i % 100)) for i in range(300)]  # 25y monthly
        raw.baa_spread_as_of = "2026-06-01"
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
        s5 = data.indicators["s5"]
        assert s5.data_source == "fred_BAA_DGS10"   # long proxy preferred over HY-OAS
        assert not s5.dropped
        # value shown = the 24-month-lagged spread
        assert s5.value == s5_credit.t_minus_2_value(raw.baa_spread_history_bps, lag_obs=24)


class TestVrpUnits:
    def test_fast_alarm_exposes_units_and_sanity(self):
        from app.engine.legs import fast_alarm

        # ~0.8%/day alternating moves -> realistic ~13% annualized realized vol,
        # so VRP lands in the sane band (a 0-vol series would legitimately flag).
        closes = [100.0]
        for i in range(30):
            closes.append(closes[-1] * (1.008 if i % 2 == 0 else 0.992))
        fa = fast_alarm("contango", 14.0, closes, 128.0).as_dict()
        assert fa["vrp_units"] == "annualized_variance_pts_pct2"
        assert fa["vrp_sane"] is True
