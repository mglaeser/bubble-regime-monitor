"""Application settings via pydantic-settings; reads .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Anthropic (judgment-call generator)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    anthropic_effort: str = "max"
    anthropic_max_tokens: int = 8000

    # FRED
    fred_api_key: str = ""

    # Price layer (v3.1): Stooq's CSV endpoint now fronts a JS proof-of-work
    # anti-bot gate, so a functioning price layer REQUIRES at least one of
    # the two free keys below.
    tiingo_api_key: str = ""        # PRIMARY  (50 req/hr, 1000/day, 500 symbols/mo)
    twelve_data_api_key: str = ""   # SECONDARY (8 req/min, 800 credits/day)
    alphavantage_api_key: str = ""  # TERTIARY, CORE tickers only (25 req/day)
    stooq_enabled: bool = False     # experimental PoW-solver path; see ToS caveat
    twelve_data_indices: bool = False  # true ONLY on Twelve Data Grow ($29/mo)
    fmp_api_key: str = ""           # optional SEC fundamentals fallback

    # Admin / security
    admin_api_key: str = "change-me-to-a-long-random-string"
    read_endpoints_public: bool = True

    # SEC EDGAR etiquette (MANDATORY, format: "Name email").
    # SEC_EDGAR_UA is the v3.1 name; SEC_USER_AGENT remains accepted.
    sec_user_agent: str = "bubblegauge-monitor admin@example.com"
    sec_edgar_ua: str = ""

    @property
    def effective_sec_ua(self) -> str:
        return self.sec_edgar_ua or self.sec_user_agent

    # Runtime
    tz: str = "UTC"
    mc_samples: int = 100_000
    mc_seed: int = 20260711
    db_url: str = "sqlite:////data/bubble.db"
    log_level: str = "INFO"
    gsadf_contested: bool = True
    lppls_timeout_s: int = 600
    gsadf_timeout_s: int = 1800

    service_version: str = "3.1.0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
