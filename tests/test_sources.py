"""Unit tests for source parsers and provenance logic (no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.sources import SourceError
from app.sources.edgar import Fact, annual_yoy, duration_facts, ttm
from app.sources.stooq import StooqLimitError, _parse_csv


def _fact(start: str, end: str, val: float) -> Fact:
    from datetime import date

    return Fact(date.fromisoformat(start), date.fromisoformat(end), val)


class TestEdgarTTM:
    def test_annual_plus_ytd_differencing(self):
        facts = [
            _fact("2024-07-01", "2025-06-30", 100.0),   # FY25 annual (10-K)
            _fact("2024-07-01", "2024-09-30", 20.0),    # FY25 Q1 YTD
            _fact("2025-07-01", "2025-09-30", 30.0),    # FY26 Q1 YTD (post-annual)
        ]
        # TTM = 100 + 30 - 20 = 110, as of the current YTD end
        result = ttm(sorted(facts, key=lambda f: (f.end, f.duration)))
        assert result is not None
        value, as_of = result
        assert value == pytest.approx(110.0)
        assert as_of.isoformat() == "2025-09-30"

    def test_right_after_10k_ttm_equals_annual(self):
        facts = [_fact("2024-01-01", "2024-12-31", 80.0)]
        result = ttm(facts)
        assert result == (80.0, facts[0].end)

    def test_no_prior_ytd_falls_back_to_annual(self):
        facts = [
            _fact("2024-01-01", "2024-12-31", 80.0),
            _fact("2025-01-01", "2025-06-30", 50.0),  # H1 YTD, no prior-year H1
        ]
        value, as_of = ttm(sorted(facts, key=lambda f: (f.end, f.duration)))
        assert value == 80.0  # cannot difference safely; annual is the honest floor

    def test_quarterly_frames_last_resort(self):
        facts = [
            _fact("2025-01-01", "2025-03-31", 10.0),
            _fact("2025-04-01", "2025-06-30", 11.0),
            _fact("2025-07-01", "2025-09-30", 12.0),
            _fact("2025-10-01", "2025-12-31", 13.0),
        ]
        value, as_of = ttm(facts)
        assert value == pytest.approx(46.0)

    def test_restatement_wins(self):
        facts = {
            "facts": {"us-gaap": {"Revenues": {"units": {"USD": [
                {"start": "2024-01-01", "end": "2024-12-31", "val": 100},
                {"start": "2024-01-01", "end": "2024-12-31", "val": 105},  # restated
                {"start": "2023-01-01", "end": "2023-12-31", "val": 90},
            ]}}}}
        }
        fl = duration_facts(facts, ["Revenues"])
        assert len(fl) == 2
        assert annual_yoy(fl) == pytest.approx((105 / 90 - 1) * 100)

    def test_annual_yoy_insufficient(self):
        assert annual_yoy([_fact("2024-01-01", "2024-12-31", 100.0)]) is None


class TestStooqParser:
    CSV = "Date,Open,High,Low,Close,Volume\n" + "\n".join(
        f"2026-01-{d:02d},1,1,1,{100 + d},10" for d in range(1, 29)
    ) + "\n" + "\n".join(
        f"2026-02-{d:02d},1,1,1,{130 + d},10" for d in range(1, 29)
    )

    def test_parses_standard_csv(self):
        rows = _parse_csv(self.CSV, "spy.us")
        assert len(rows) == 56
        assert rows[-1] == ("2026-02-28", 158.0)

    def test_case_insensitive_headers(self):
        text = self.CSV.replace("Date,Open,High,Low,Close,Volume", "DATE,OPEN,HIGH,LOW,CLOSE,VOLUME")
        assert len(_parse_csv(text, "spy.us")) == 56

    def test_daily_limit_detected(self):
        with pytest.raises(StooqLimitError):
            _parse_csv("Exceeded the daily hits limit", "spy.us")
        with pytest.raises(StooqLimitError):
            _parse_csv("Przekroczono dzienny limit wywolan", "spy.us")

    def test_no_data_detected(self):
        with pytest.raises(SourceError, match="no data"):
            _parse_csv("No data", "zzzz.us")

    def test_garbage_body_reported_with_snippet(self):
        with pytest.raises(SourceError, match="body starts"):
            _parse_csv("<html>login wall</html>", "spy.us")


class TestStaleness:
    def test_stale_flag_against_sla(self):
        from app.services.compute import IndicatorOutput

        old = (datetime.now(UTC) - timedelta(days=10)).date().isoformat()
        fresh = datetime.now(UTC).date().isoformat()
        assert IndicatorOutput("d1", 56.0, 0.5, False, "x", False, as_of=old).stale is True  # SLA 3d
        assert IndicatorOutput("d1", 56.0, 0.5, False, "x", False, as_of=fresh).stale is False
        assert IndicatorOutput("d2", 50.0, 0.5, False, "x", False, as_of=old).stale is False  # SLA 45d
        assert IndicatorOutput("d1", 56.0, 0.5, False, "x", False, as_of=None).stale is None

    def test_snapshot_freshness_populated(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        today = datetime.now(UTC).date().isoformat()
        raw.cape_as_of = today
        raw.breadth_as_of = today
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        assert data.freshness.get("s1") == "0d"
        assert data.freshness.get("d1") == "0d"
        payload = data.indicators["s1"].payload()
        assert payload["stale"] is False and payload["age_days"] == 0
