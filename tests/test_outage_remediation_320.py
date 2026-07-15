"""Regression tests for the v3.2.0 outage remediation.

Covers the four defects fixed after the July 2026 DNS outage:
  - LPPLS repaired to the real lppls==0.6.24 API (flat filter_conditions_config
    + compute_indicators), guarding against the wrong-shape / mangled-identifier
    class of bug that silently broke D4.
  - S3 repointed off the disabled-Stooq label onto the price-chain provenance.
  - Breadth sourced from SSGA constituents (Wikipedia removed).
The DNS fix itself (compose.yml) and the golden fixture (median ~40) are
covered by test_compose/infra and the existing normalization/pipeline suites.
"""

from __future__ import annotations

import inspect
import io

import pytest


class TestLPPLSApi:
    def test_filter_conditions_is_flat_dict_with_valid_keys(self):
        # lppls 0.6.24 requires a FLAT dict[str, float]; the older
        # list-of-{"condition_1": {...}} form raises at runtime. A literal check
        # catches that class of bug (and a re-introduced mangled identifier).
        from app.indicators import d4_lppls

        fc = d4_lppls.FILTER_CONDITIONS
        assert isinstance(fc, dict)
        assert all(isinstance(k, str) and isinstance(v, float) for k, v in fc.items())
        valid = {"m_min", "m_max", "w_min", "w_max", "O_min", "D_min",
                 "tc_min_days", "tc_max_days", "tc_min_frac", "tc_max_frac"}
        assert set(fc).issubset(valid), f"unknown lppls filter keys: {set(fc) - valid}"
        assert fc["m_min"] < fc["m_max"] and fc["w_min"] < fc["w_max"]

    def test_calls_the_real_064_api(self):
        from app.indicators import d4_lppls

        src = inspect.getsource(d4_lppls.compute_confidence)
        assert "filter_conditions_config" in src          # correctly spelled
        assert "mp_compute_nested_fits" in src
        assert "compute_indicators" in src                # 0.6.24 qualification step
        # the broken list-of-condition_1 form must not reappear
        assert "condition_1" not in src

    def test_requires_500_closes_returns_insufficient_data(self):
        from app.indicators import d4_lppls

        result = d4_lppls.compute_confidence([100.0] * 499)
        assert result["state"] == "INSUFFICIENT_DATA"
        assert result["value"] is None
        assert result["n_closes"] == 499

    def test_quality_rule_and_bands(self):
        # v3.3.2 single-endpoint contract: quality from evaluated window count,
        # bands partitioned from the library's own per-fit is_qualified flags.
        from app.indicators import d4_lppls

        assert d4_lppls._quality(144) == 1.0
        assert d4_lppls._quality(99) == 0.5
        assert d4_lppls._quality(0) == 0.0

        # bands normalize over POSITIVE-bubble windows (b < 0), like pos_conf:
        # a b>0 window is excluded from the denominator; b<0 windows count.
        fits = ([{"t1": 0.0, "t2": 40.0, "is_qualified": True, "b": -1.0}] +    # short: 1/1
                [{"t1": 0.0, "t2": 100.0, "is_qualified": False, "b": -1.0}] * 3 +  # medium: 0/3
                [{"t1": 0.0, "t2": 100.0, "is_qualified": True, "b": 1.0}] +    # medium neg-bubble: excluded
                [{"t1": 0.0, "t2": 500.0, "is_qualified": True, "b": -1.0}] * 2)  # long: 2/2
        bands = d4_lppls.band_fractions(fits)
        assert bands["short"] == {"conf": 1.0, "n": 1}
        assert bands["medium"] == {"conf": 0.0, "n": 3}   # the b>0 window not counted
        assert bands["long"] == {"conf": 1.0, "n": 2}
        # value == n_qual/n_positive by construction (same normalization)
        assert d4_lppls._positive_qualified(fits) == (3, 6)
        # schema surprise (no is_qualified/b) -> None, never a crash
        assert d4_lppls.band_fractions([{"t1": 0.0, "t2": 40.0}]) is None


class TestS3NoStooq:
    def test_s3_data_source_is_price_chain_provenance(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        raw.semis_source = "tiingo:SMH"   # real price-chain provenance
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        s3 = data.indicators["s3"]
        assert not s3.dropped
        assert not s3.data_source.startswith("stooq")
        assert s3.data_source == "tiingo:SMH"

    def test_s3_label_never_stooq_even_without_provenance(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()   # semis_source unset -> falls back to price:*
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        assert not data.indicators["s3"].data_source.startswith("stooq")

    def test_total_return_pct_lives_in_prices_module(self):
        from app.sources import prices

        assert hasattr(prices, "total_return_pct")
        series = [("2024-01-01", 100.0), ("2024-01-02", 110.0)]
        assert prices.total_return_pct(series, 1) == pytest.approx(10.0)


class TestBreadthOnSSGA:
    def test_breadth_uses_ssga_not_wikipedia(self):
        from app.sources import breadth

        src = inspect.getsource(breadth)
        # No live Wikipedia dependency (URL / fetch), regardless of prose that
        # explains the removal; constituents now come from SSGA.
        assert "en.wikipedia.org" not in src
        assert "WIKIPEDIA_URL" not in src
        assert "ssga.sp500_constituents" in src
        assert hasattr(breadth, "refresh_breadth_cache")  # sweep is a background job

    def test_ssga_constituents_parse_from_xlsx(self):
        import openpyxl

        from app.sources import ssga

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["State Street SPY Holdings", None])
        ws.append(["As of 2026-07-10", None])
        ws.append(["Name", "Ticker", "Weight"])            # header row
        rows = [("Apple Inc", "AAPL"), ("Berkshire Hathaway B", "BRK.B"),
                ("Cash", "-"), ("Dollars", "USD")]
        rows += [(f"Company {i}", f"CO{i:03d}") for i in range(410)]  # satisfy >=400 floor
        for name, tk in rows:
            ws.append([name, tk, 0.1])
        buf = io.BytesIO()
        wb.save(buf)

        tickers = ssga._constituents_from_xlsx(buf.getvalue())
        assert "AAPL" in tickers
        assert "BRK-B" in tickers          # class-share dot -> dash
        assert "-" not in tickers          # placeholder dropped (fails ticker regex)
        assert "USD" not in tickers        # cash/FX line dropped (denylist)
        assert len(tickers) >= 400

    def test_twelvedata_credit_governor_ignores_per_minute_header(self):
        # The breadth sweep must not trip on Twelve Data's per-minute counter.
        from app.sources.prices import _daily_credits_left

        # per-minute-looking body -> unknown (None) so the sweep proceeds
        assert _daily_credits_left({"plan_limit": 8, "current_usage": 3}) is None
        # real daily budget -> remaining
        assert _daily_credits_left({"plan_daily_limit": 800, "daily_usage": 120}) == 680
        assert _daily_credits_left({"plan_limit": 800, "current_usage": 750}) == 50
        assert _daily_credits_left({}) is None

    def test_pct_above_200dma_reads_cache_only(self, isolated_db):
        # Recompute path must not hit the network; an empty cache raises (D1
        # then drops until the background sweep populates it) rather than
        # sweeping Twelve Data inline.
        from app.sources import SourceError, breadth

        with pytest.raises(SourceError, match="cache empty"):
            breadth.pct_above_200dma()
