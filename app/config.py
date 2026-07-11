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

    # Admin / security
    admin_api_key: str = "change-me-to-a-long-random-string"
    read_endpoints_public: bool = True

    # SEC EDGAR etiquette (MANDATORY, format: "Name email")
    sec_user_agent: str = "bubblegauge-monitor admin@example.com"

    # Runtime
    tz: str = "UTC"
    mc_samples: int = 100_000
    mc_seed: int = 20260711
    db_url: str = "sqlite:////data/bubble.db"
    log_level: str = "INFO"
    gsadf_contested: bool = True
    lppls_timeout_s: int = 1800

    service_version: str = "3.0.0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
