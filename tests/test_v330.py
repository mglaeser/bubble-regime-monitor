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

        assert parse_polygon_grouped({"status": "OK", "resultsCount": 0, "results": []}) == {}

    def test_breadth_from_daily_close_full_universe(self, isolated_db):
        from datetime import UTC, date, datetime, timedelta

        from app.db import session_scope
        from app.models import DailyClose
        from app.sources.breadth import _breadth_from_daily_close

        now = datetime.now(UTC)
        base = date(2026, 1, 1)
        with session_scope() as s:
            # AAA: rising -> above its SMA200; BBB: flat-then-down -> below; CCC: too few closes
            for i in range(200):
                s.merge(DailyClose(symbol="AAA", date=base + timedelta(days=i),
                                   close=100.0 + i, provider="polygon", fetched_at=now))
                s.merge(DailyClose(symbol="BBB", date=base + timedelta(days=i),
                                   close=200.0 - i, provider="polygon", fetched_at=now))
            for i in range(50):
                s.merge(DailyClose(symbol="CCC", date=base + timedelta(days=i),
                                   close=100.0, provider="polygon", fetched_at=now))
        above, counted = _breadth_from_daily_close(["AAA", "BBB", "CCC"])
        assert counted == 2          # CCC excluded (<200 closes)
        assert above == 1            # only AAA above its 200-DMA

    def test_binomial_ci_shrinks_with_n(self):
        from app.sources.breadth import _binomial_ci_pp

        assert _binomial_ci_pp(0.6, 137) > _binomial_ci_pp(0.6, 503)
        assert _binomial_ci_pp(0.6, 503) == 0.0  # full universe -> no sampling error


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
