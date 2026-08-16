"""Application settings via pydantic-settings; reads .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from app import methodology as _M


def _frozen_mc(key: str) -> int:
    """MC default sourced from the canonical frozen artifact (F-01/L-07)."""
    return _M.get_path("monte_carlo", key)


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

    # --- ALERT SYSTEM (docs/ALERT_SYSTEM.md) --------------------------------
    # Two INDEPENDENT switches. Evidence capture may run with alerting fully
    # disabled, and enabling alerts never implies capture. `live` is never
    # reached automatically: it requires promoted rule + phrase artifacts and a
    # deliberate operator edit.
    #
    # Capture defaults ON because that is what rollout Stage 1 IS ("schema,
    # sidecar capture on, alerts disabled, pure evaluator, CAS state, replay").
    # The default-off rule governs the flags that can make the service ACT —
    # `alerts_mode` below, which stays `disabled`. Capture is not one of them:
    # it writes one immutable evidence row per recompute in its own
    # transaction, calls no provider, sends nothing and cannot alter a score or
    # roll back a snapshot. Leaving it off would make Stage 1 inert — no
    # sidecars means nothing to replay — while still claiming to have reached
    # it. The promoted ruleset declares the same thing in `capture.enabled`,
    # and that declaration is honoured (see `alert_integration.capture_armed`),
    # so this is a reviewable artifact decision rather than a bare default.
    alert_input_capture: bool = True
    alerts_mode: Literal["disabled", "shadow", "live"] = "disabled"
    alerts_live_profile: str = "default"

    # Immutable artifacts. The *_lkg_* pair is the last-known-good ruleset used
    # when a candidate fails validation — a fallback NEVER escalates the mode.
    alerts_rules_path: str = "/data/alert_rules.yaml"
    alerts_lkg_path: str = "/data/alert_rules.last_good.yaml"
    alerts_lkg_hash_path: str = "/data/alert_rules.last_good.sha256"
    alerts_phrase_path: str = "/data/alert_phrases.json"
    alerts_calibration_dir: str = "/data/alert-calibration"

    # Separate scopes. A browser never receives the admin key (or the write
    # key); detailed reads go through a server-side proxy or the redacted
    # projection. Empty means "this scope is not configured" -> fail closed.
    alerts_read_api_key: str = ""
    alerts_write_api_key: str = ""
    alerts_public_read: bool = False

    # Volume governance. P1 is exempt from all three.
    alerts_non_p1_target_168h: int = 2
    alerts_non_p1_cap_24h: int = 3
    alerts_non_p1_cap_168h: int = 6

    alerts_dispatch_poll_s: int = 20
    alerts_dispatch_lease_s: int = 120
    alerts_eval_lease_s: int = 300
    alerts_eval_budget_ms: int = 1500
    alerts_unknown_escalate_h: int = 24
    alerts_metadata_retention_days: int = 800
    alerts_message_retention_days: int = 400
    alerts_busy_timeout_ms: int = 5000

    # The model SELECTS reviewed codes; it never writes prose and never writes
    # a number. P1 skips it entirely.
    alerts_llm_enabled: bool = True
    alerts_llm_timeout_s: int = 6
    alerts_llm_render_cap_24h: int = 12
    alerts_llm_test_cap_1h: int = 6
    alerts_llm_retry_max: int = 1
    alerts_llm_shadow_enabled: bool = False

    # Migration-friendly alias for the legacy `sms_enabled`. Until the Stage 4
    # cutover the daily digest keeps running: ALERTS_MODE=live must NOT
    # implicitly disable it. See `effective_daily_sms_enabled`.
    daily_sms_enabled: bool | None = None

    # Runtime. mc_samples / mc_seed DEFAULT to the canonical frozen artifact
    # (F-01/L-07) so the runtime MC seed is causally the frozen value; env vars
    # may still override for operational runs.
    tz: str = "UTC"
    # Set by the test suite (conftest): skips the boot warm-up threads and the
    # scheduler in lifespan. Leaked daemon threads (hy-oas-seed /
    # breadth-backfill) crossing test boundaries corrupted other tests'
    # throwaway sqlite DBs on CI ("file is not a database", intermittent).
    testing: bool = False
    mc_samples: int = _frozen_mc("samples")
    mc_seed: int = _frozen_mc("seed")
    db_url: str = "sqlite:////data/bubble.db"
    log_level: str = "INFO"
    gsadf_contested: bool = True
    lppls_timeout_s: int = 1500  # generous headroom for the Atom N2800 (background recompute; API serves the last snapshot meanwhile)
    gsadf_timeout_s: int = 1800

    service_version: str = "3.8.0"

    @property
    def sms_configured(self) -> bool:
        return bool(self.sms_enabled and self.sipgate_token_id
                    and self.sipgate_token and self.sipgate_recipient)

    @property
    def effective_daily_sms_enabled(self) -> bool:
        """Whether the LEGACY daily digest runs.

        DAILY_SMS_ENABLED wins when explicitly set; otherwise the legacy
        SMS_ENABLED still governs. The alert system never touches this — the
        Stage 4 cutover is an explicit operator action, not a side effect of
        turning alerts on."""
        if self.daily_sms_enabled is not None:
            return self.daily_sms_enabled
        return self.sms_enabled


@lru_cache
def get_settings() -> Settings:
    return Settings()
