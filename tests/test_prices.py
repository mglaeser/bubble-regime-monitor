"""v3.1 price-layer tests: provider parsers, symbol mapping."""

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
    def test_indices_use_etf_proxies(self):
        assert resolve_symbol("NDX") == ("QQQ", True)
        assert resolve_symbol("SPX") == ("SPY", True)
        assert resolve_symbol("SPY") == ("SPY", False)


class TestProviderChainNever500:
    def test_all_providers_missing_keys_serves_cache_or_raises(self, isolated_db, monkeypatch):
        for var in ("TIINGO_API_KEY", "TWELVE_DATA_API_KEY", "ALPHAVANTAGE_API_KEY"):
            monkeypatch.setenv(var, "")
        from app.config import get_settings

        get_settings.cache_clear()
        # No cache, no keys -> SourceError (caught by the compute layer's
        # _track, never a 500).
        from app.sources import SourceError

        with pytest.raises(SourceError):
            prices.get_daily_closes("SPY")
        get_settings.cache_clear()


class TestProviderHealthScoring:
    def test_not_configured_does_not_penalize_health(self, isolated_db, monkeypatch):
        # All providers unconfigured -> ProviderNotConfigured -> no provider
        # should be put on cooldown (it's a config state, not ill-health).
        for var in ("TIINGO_API_KEY", "TWELVE_DATA_API_KEY", "ALPHAVANTAGE_API_KEY"):
            monkeypatch.setenv(var, "")
        from app.config import get_settings

        get_settings.cache_clear()
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import ProviderHealth
        from app.sources import SourceError

        for _ in range(4):  # more than FAIL_THRESHOLD
            with pytest.raises(SourceError):
                prices.get_daily_closes("SPY")
        with session_scope() as session:
            rows = session.execute(select(ProviderHealth)).scalars().all()
        # No health rows written at all (nothing counted as a failure).
        assert all(r.cooldown_until is None for r in rows)
        get_settings.cache_clear()

    def test_real_failure_records_and_cools_down(self, isolated_db, monkeypatch):
        # A genuine ProviderError (bad body) after 3 tries -> 6h cooldown.
        monkeypatch.setenv("TIINGO_API_KEY", "tok")
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setattr(prices, "fetch_tiingo",
                            lambda c: (_ for _ in ()).throw(prices.ProviderError("tiingo: boom")))
        # keep the rest unconfigured so the chain exhausts
        for var in ("TWELVE_DATA_API_KEY", "ALPHAVANTAGE_API_KEY"):
            monkeypatch.setenv(var, "")
        get_settings.cache_clear()

        from sqlalchemy import select

        from app.db import session_scope
        from app.models import ProviderHealth
        from app.sources import SourceError

        for _ in range(3):
            with pytest.raises(SourceError):
                prices.get_daily_closes("SPY")
        with session_scope() as session:
            row = session.execute(
                select(ProviderHealth).where(ProviderHealth.provider == "tiingo")
            ).scalars().first()
        assert row is not None and row.consecutive_failures >= 3
        assert row.cooldown_until is not None  # on cooldown after 3 real failures
        get_settings.cache_clear()


class TestTheChainIsThreeProvidersAndTheCache:
    """The price chain is Tiingo -> Twelve Data -> Alpha Vantage -> SQLite cache.

    Stooq (the proof-of-work path), yfinance (an optional extra) and the Twelve
    Data Grow-plan index symbols went on 2026-10-03 under AGENTS.md's "delete
    before you add": on leaf, the only deployment, STOOQ_ENABLED and
    TWELVE_DATA_INDICES were false and yfinance is in neither lock, so none of
    the three ever ran. NDX and SPX stay the ETF proxies QQQ and SPY that
    production reads today, so no input changes."""

    def test_the_chain_asks_three_providers_then_serves_the_cache(self, isolated_db, monkeypatch):
        asked: list[str] = []

        def fetcher(name):
            def fetch(canonical):
                asked.append(name)
                raise prices.ProviderNotConfigured(f"{name}: no key")
            return fetch

        monkeypatch.setattr(prices, "_fetcher", fetcher)
        stale = [(f"2020-01-{d:02d}", 100.0 + d) for d in range(1, 29)]
        prices._cache_put("SPY", stale, "tiingo:SPY")
        result = prices.get_daily_closes("SPY")
        assert asked == ["tiingo", "twelvedata", "alphavantage"]
        assert result.value == stale
        assert result.provenance.fallback_used is True

    @pytest.mark.parametrize("fresh", [True, False])
    def test_a_cached_native_index_is_never_served(self, isolated_db, monkeypatch, fresh):
        """#155 round 2, SOTA-A: a host that once read native NDX/SPX (the paid
        Twelve Data path, yfinance) keeps that series in the cache under the
        canonical symbol, and the cache served it - at once while fresh, and
        when every provider failed. The cache answers only for the instrument
        the chain reads now: a row cached from another one is passed over, and
        the next fetch replaces it. Production's cache holds the proxies only
        (read-only, 2026-10-03: NDX is tiingo:QQQ)."""
        from datetime import UTC, datetime, timedelta

        last = datetime.now(UTC).date() if fresh else datetime(2020, 1, 28, tzinfo=UTC).date()
        native = [((last - timedelta(days=27 - i)).isoformat(), 20_000.0 + i) for i in range(28)]
        proxy = [(day, close / 40) for day, close in native]
        prices._cache_put("NDX", native, "twelvedata:NDX")

        def failing(name):
            def fetch(canonical):
                raise prices.ProviderNotConfigured(f"{name}: no key")
            return fetch

        monkeypatch.setattr(prices, "_fetcher", failing)
        with pytest.raises(prices.SourceError):
            prices.get_daily_closes("NDX")

        def answering(name):
            def fetch(canonical):
                return proxy, "QQQ", True
            return fetch

        monkeypatch.setattr(prices, "_fetcher", answering)
        result = prices.get_daily_closes("NDX")
        assert result.value == proxy and result.provenance.source == "tiingo:QQQ"
        assert prices._cache_get("NDX")[2] == "tiingo:QQQ"

    def test_settings_have_no_switch_for_a_deleted_tier(self):
        from app.config import Settings

        assert {"stooq_enabled", "twelve_data_indices"}.isdisjoint(Settings.model_fields)

    def test_an_old_env_line_for_a_deleted_tier_changes_nothing(self, monkeypatch):
        from app.config import get_settings, near_miss_env_keys

        old = {"STOOQ_ENABLED": "true", "TWELVE_DATA_INDICES": "true"}
        for key, value in old.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        try:
            assert resolve_symbol("NDX") == ("QQQ", True)
            assert resolve_symbol("SPX") == ("SPY", True)
            assert near_miss_env_keys(old) == []
        finally:
            get_settings.cache_clear()

    def test_the_deleted_tiers_leave_no_code(self):
        import importlib.util
        import tomllib
        from pathlib import Path

        assert importlib.util.find_spec("app.sources.stooq") is None
        for name in ("fetch_stooq", "fetch_yfinance",
                     "TWELVE_DATA_INDEX_SYMBOLS", "YFINANCE_INDEX_SYMBOLS"):
            assert not hasattr(prices, name), name
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        assert "yfinance" not in project["optional-dependencies"]


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


def test_a_host_that_still_sets_a_removed_price_setting_is_told():
    """#155 round 1, SOTA-A: a host still setting TWELVE_DATA_INDICES=true would
    have it ignored without a word and read the ETF proxies. The two removed
    settings are retired keys (app/config.py RETIRED_ENV_KEYS, as D2c retired
    DAILY_SMS_ENABLED): named at boot, in the alerts preflight and in alert
    health, with what the service reads instead."""
    from app.config import retired_env_keys

    named = dict(retired_env_keys({"TWELVE_DATA_INDICES": "true", "stooq_enabled": "true"}))
    assert set(named) == {"TWELVE_DATA_INDICES", "STOOQ_ENABLED"}
    assert "QQQ" in named["TWELVE_DATA_INDICES"] and "SPY" in named["TWELVE_DATA_INDICES"]
