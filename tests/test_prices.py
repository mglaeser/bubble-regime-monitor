"""v3.1 price-layer tests: PoW detection, provider parsers, symbol mapping."""

from __future__ import annotations

import pytest

from app.sources import prices
from app.sources.prices import (
    NotOnPlan,
    ProviderError,
    RateLimited,
    parse_alphavantage,
    parse_tiingo,
    parse_twelvedata,
    resolve_symbol,
)
from app.sources.stooq import StooqPoWChallenge, detect_pow, solve_pow

# The operator's verbatim 796-byte-style challenge body (abridged shape).
STOOQ_POW_BODY = (
    '<!DOCTYPE html><html><head></head><body>'
    '<noscript>This site requires JavaScript to verify your browser...</noscript>'
    '<script nonce="x">(async()=>{const c="AAAAAGpSuLcEAnbV8FVT4wjT5XZmTwe_",'
    'd=4,t="0".repeat(d);let n=0;while(1){/* ... */}'
    'const r=await fetch("/__verify",{method:"POST"});})();</script></body></html>'
)


class TestStooqPoW:
    def test_detects_doctype_and_verify(self):
        assert detect_pow(STOOQ_POW_BODY) is True
        assert detect_pow("Date,Open,High,Low,Close,Volume\n2026-01-02,1,1,1,2,3") is False

    def test_parse_raises_pow_challenge(self):
        from app.sources.stooq import _parse_csv

        with pytest.raises(StooqPoWChallenge):
            _parse_csv(STOOQ_POW_BODY, "spy.us")

    def test_solver_finds_valid_nonce(self):
        import hashlib

        n = solve_pow("testchallenge", difficulty=2)
        assert n is not None
        assert hashlib.sha256(f"testchallenge{n}".encode()).hexdigest().startswith("00")


class TestTiingoParser:
    def test_valid_json_list(self):
        payload = [{"date": f"2026-01-{d:02d}T00:00:00Z", "adjClose": 100.0 + d}
                   for d in range(1, 60)]
        rows = parse_tiingo(payload, "SPY")
        assert len(rows) == 59
        assert rows[-1][0] == "2026-01-59"[:10] or rows[-1][1] == pytest.approx(159.0)

    def test_200_ok_error_body_is_provider_error(self):
        # Verbatim symbol-cap message returned with HTTP 200
        body = ("You have run over your 500 symbol look up for this month. "
                "Please upgrade at https://api.tiingo.com/pricing to have your limits increased.")
        with pytest.raises(ProviderError):
            parse_tiingo(body, "SPY")

    def test_dict_error_body_is_provider_error(self):
        with pytest.raises(ProviderError):
            parse_tiingo({"detail": "Error: Not authorized."}, "SPY")


class TestTwelveDataParser:
    def test_valid_values(self):
        payload = {"values": [{"datetime": f"2026-01-{d:02d}", "close": str(100 + d)}
                              for d in range(1, 60)], "status": "ok"}
        rows = parse_twelvedata(payload, "SPY")
        assert len(rows) == 59

    def test_429_credits_exhausted(self):
        payload = {"code": 429, "message": "You have run out of API credits...",
                   "status": "error"}
        with pytest.raises(RateLimited):
            parse_twelvedata(payload, "SPY")

    def test_403_index_not_on_plan(self):
        payload = {"code": 403, "message": "index access is not available on your plan",
                   "status": "error"}
        with pytest.raises(NotOnPlan):
            parse_twelvedata(payload, "GSPC")


class TestAlphaVantageParser:
    def test_valid_series(self):
        series = {f"2026-01-{d:02d}": {"4. close": str(100 + d)} for d in range(1, 60)}
        rows = parse_alphavantage({"Time Series (Daily)": series}, "SPY")
        assert len(rows) == 59

    def test_rate_limit_note(self):
        payload = {"Note": "Thank you for using Alpha Vantage! ... 25 requests per day ..."}
        with pytest.raises(RateLimited):
            parse_alphavantage(payload, "SPY")


class TestSymbolMapping:
    def test_free_tier_uses_etf_proxies(self, monkeypatch):
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("TWELVE_DATA_INDICES", "false")
        get_settings.cache_clear()
        assert resolve_symbol("NDX", "tiingo") == ("QQQ", True)
        assert resolve_symbol("SPX", "tiingo") == ("SPY", True)
        assert resolve_symbol("NDX", "twelvedata") == ("QQQ", True)
        assert resolve_symbol("SPY", "tiingo") == ("SPY", False)
        get_settings.cache_clear()

    def test_grow_plan_uses_raw_index(self, monkeypatch):
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("TWELVE_DATA_INDICES", "true")
        get_settings.cache_clear()
        assert resolve_symbol("SPX", "twelvedata") == ("GSPC", False)
        assert resolve_symbol("NDX", "twelvedata") == ("NDX", False)
        get_settings.cache_clear()

    def test_yfinance_raw_index_symbols(self):
        assert resolve_symbol("NDX", "yfinance") == ("^NDX", False)
        assert resolve_symbol("SPX", "yfinance") == ("^GSPC", False)


class TestProviderChainNever500:
    def test_all_providers_missing_keys_serves_cache_or_raises(self, isolated_db, monkeypatch):
        for var in ("TIINGO_API_KEY", "TWELVE_DATA_API_KEY", "ALPHAVANTAGE_API_KEY"):
            monkeypatch.setenv(var, "")
        monkeypatch.setenv("STOOQ_ENABLED", "false")
        from app.config import get_settings

        get_settings.cache_clear()
        # No cache, no keys, yfinance import will fail -> SourceError (caught
        # by the compute layer's _track, never a 500).
        from app.sources import SourceError

        with pytest.raises(SourceError):
            prices.get_daily_closes("SPY")
        get_settings.cache_clear()


class TestCoverageGate:
    def test_block_degraded_when_third_dropped(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        # Drop D3 (0.32) + D4 (0.20) = 0.52 of 1.00 in Block D -> > 1/3 lost
        raw.hyperscalers = None
        raw.lppls_confidence = None
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        assert data.coverage["D"]["degraded"] is True
        assert data.coverage["degraded"] is True
        assert data.action_band.startswith("suppressed")

    def test_block_ok_when_coverage_sufficient(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        assert data.coverage["S"]["degraded"] is False
        assert data.coverage["D"]["degraded"] is False
