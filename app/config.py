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
    polygon_api_key: str = ""       # PRIMARY breadth (grouped-daily: 1 call/day, whole US market)
    stooq_enabled: bool = False     # experimental PoW-solver path; see ToS caveat
    twelve_data_indices: bool = False  # true ONLY on Twelve Data Grow ($29/mo)
    fmp_api_key: str = ""           # optional SEC fundamentals fallback

    # Admin / security
    admin_api_key: str = "change-me-to-a-long-random-string"
    read_endpoints_public: bool = True

    # Auto-deploy webhook (v3.5.0, docs/AUTO_DEPLOY.md). The endpoint is
    # FAIL-CLOSED: it returns 503 unless BOTH values are set. The secret is the
    # GitHub webhook HMAC secret (X-Hub-Signature-256), verified constant-time —
    # never an API key in the URL. The app only WRITES a trigger file on /data;
    # the host-side systemd watchdog runs deploy.sh (a container cannot and
    # must not replace itself).
    github_webhook_secret: str = ""
    deploy_branch: str = ""          # e.g. claude/bubblegauge-build-spec-fzthju
    deploy_trigger_dir: str = "/data/deploy-trigger"

    # SEC EDGAR etiquette (MANDATORY, format: "Name email").
    # SEC_EDGAR_UA is the v3.1 name; SEC_USER_AGENT remains accepted.
    sec_user_agent: str = "bubblegauge-monitor admin@example.com"
    sec_edgar_ua: str = ""

    @property
    def effective_sec_ua(self) -> str:
        return self.sec_edgar_ua or self.sec_user_agent

    # Daily SMS digest via the sipgate REST API v2 (POST /sessions/sms).
    # Auth is a Personal Access Token (token ID + token, scope
    # sessions:sms:write). Sent once a day at SMS_DAILY_HOUR:SMS_DAILY_MINUTE
    # UTC; disabled unless SMS_ENABLED=true and credentials are present.
    sms_enabled: bool = False
    sipgate_token_id: str = ""       # Personal Access Token ID (Basic-auth username)
    sipgate_token: str = ""          # Personal Access Token (Basic-auth password)
    sipgate_sms_id: str = "s0"       # Web SMS extension: 's' + number
    sipgate_recipient: str = ""      # recipient in E.164 format, e.g. +49151...
    sms_daily_hour: int = 8          # UTC hour for the daily digest
    sms_daily_minute: int = 0
    sms_max_len: int = 160           # single-SMS GSM-7 ceiling (hard cap)

    # Runtime
    tz: str = "UTC"
    mc_samples: int = 100_000
    mc_seed: int = 20260711
    db_url: str = "sqlite:////data/bubble.db"
    log_level: str = "INFO"
    gsadf_contested: bool = True
    lppls_timeout_s: int = 1500  # generous headroom for the Atom N2800 (background recompute; API serves the last snapshot meanwhile)
    gsadf_timeout_s: int = 1800

    service_version: str = "3.5.0"

    @property
    def sms_configured(self) -> bool:
        return bool(self.sms_enabled and self.sipgate_token_id
                    and self.sipgate_token and self.sipgate_recipient)


@lru_cache
def get_settings() -> Settings:
    return Settings()
