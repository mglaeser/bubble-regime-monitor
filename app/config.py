"""Application settings via pydantic-settings; reads .env."""

from __future__ import annotations

from collections.abc import Mapping
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

    # Daily digest over iMessage via an imessage-proxy instance
    # (POST {base}/api/messages). Auth is ONE scoped bearer key holding
    # `messages:send` — never `admin`, which would also grant the key the
    # ability to widen its own recipient allowlist. The destination must ALSO
    # be on the proxy's allowlist, which that scope can neither read nor
    # change, so a stolen key cannot pick a new recipient.
    #
    # The digest sends over exactly ONE transport. When both switches are on
    # iMessage wins and sipgate is NOT called: delivering the same digest twice
    # is a defect, not a fallback, and a silent downgrade to SMS would hide the
    # proxy being down precisely when the operator needs to know.
    #
    # Shares SMS_DAILY_HOUR/MINUTE and SMS_MAX_LEN — the schedule and the body
    # are transport-independent. The proxy accepts 4000 Unicode code points,
    # so the 160-char ASCII cap is now a self-imposed SMS-era limit rather
    # than a physical one; raising it is a product decision, not a migration.
    imessage_enabled: bool = False
    imessage_api_base_url: str = ""  # origin only, e.g. https://messages.example.com
    imessage_api_key: str = ""       # scoped `messages:send` key, `imp_` prefix
    imessage_recipient: str = ""     # allowlisted handle: +E.164 or an Apple-ID email
    imessage_timeout_s: int = 30     # read cap; the proxy's own deadline is longer

    # --- SYSTEM-FAILURE ALERTS ----------------------------------------------
    # "The recompute is failing" over whichever digest transport is configured.
    # Distinct from the ALERT SYSTEM below, which is about the SCORE (a regime
    # crossing) and is off by default: this one is about the SERVICE, and its
    # whole purpose is to fire when the machinery it would otherwise depend on
    # is the broken thing. It shares no state, no ruleset and no outbox with it.
    #
    # ON by default, unlike every other flag that makes the service act. It
    # sends only where a transport is already configured and a recipient the
    # operator chose is already on file, so it can reach nobody new; and the
    # failure it reports is one the operator learned about, last time, twelve
    # days late. A monitor that must be switched on is a monitor that is off.
    failure_alerts_enabled: bool = True
    # How long the SAME failure stays quiet before repeating. A new failure
    # signature always sends immediately; this only throttles repeats, which
    # would otherwise arrive six times a day for as long as the outage lasts.
    failure_alert_repeat_h: int = 24
    # Where the current outage is remembered across a restart. Deploying a fix
    # IS a restart, and that is the usual way an outage ends — without this the
    # all-clear goes missing in the common case. Best-effort: an unwritable
    # path degrades to in-memory state and never fails a send.
    failure_alert_state_path: str = "/data/failure-alert-state.json"
    # How long a recompute may hold the single-flight lock before a skipped slot
    # is reported as a wedged run. Slots are 4h apart and a full gather runs
    # well under an hour, so a run still in flight at the next slot is stuck.
    failure_alert_stuck_after_h: int = 4
    # How many times a CHANGED failure signature may skip the quiet period
    # before the ordinary one applies again. A changed signature is news and
    # sends at once — but an error whose text carries a moving unquoted number
    # is "news" every single time, which bypasses the throttle entirely. A
    # budget bounds that without delaying a genuinely distinct failure, which a
    # time floor would.
    failure_alert_max_signature_changes: int = 3

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

    # H-05, decided: the frontend uses a BROWSER-VISIBLE SCOPED TOKEN, not a
    # server-side proxy. A static key embedded in browser JavaScript is
    # extractable, so it is treated as a PUBLIC CAPABILITY rather than a
    # secret: it reaches only the redacted projection, it is rate-limited, it
    # rotates on its own schedule, and it grants no silence, retry, render-text
    # or admin right. Setting this false is the assertion that the read key is
    # only ever held by a trusted server-side proxy — which is a different
    # architecture, so it must be stated rather than assumed.
    alerts_read_token_is_public: bool = True

    # Rotation overlap. A public token has to be rotatable without taking the
    # dashboard down, and a single key forces a hard cutover — which in
    # practice means the rotation never happens. The previous key stays valid
    # until it is cleared; it is a SEPARATE variable so retiring it is its own
    # deliberate edit.
    alerts_read_api_key_previous: str = ""

    # A public capability gets its own ceiling, tighter than an operator's.
    alerts_public_read_rate_limit: str = "30/minute"

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

    @property
    def imessage_configured(self) -> bool:
        """Credentials + destination present. Independent of the switch, so a
        half-configured deployment reports "enabled but not configured" rather
        than looking identical to one that was never turned on."""
        return bool(self.imessage_api_base_url and self.imessage_api_key
                    and self.imessage_recipient)

    @property
    def imessage_enabled_but_unconfigured(self) -> bool:
        """The switch is on and the credentials are not there. Its own property
        because this state must never look like "iMessage is simply off"."""
        return self.imessage_enabled and not self.imessage_configured

    @property
    def daily_digest_transport(self) -> Literal["imessage", "sipgate", "none"]:
        """Which transport carries the daily digest.

        iMessage wins when both switches are on — see the IMESSAGE_* block. The
        scheduler gates on this rather than on `effective_daily_sms_enabled`,
        because an operator who set SMS_ENABLED=false and IMESSAGE_ENABLED=true
        means "send my digest over iMessage", not "stop sending my digest".

        REQUIRES `imessage_configured`, not merely the switch. Selecting on the
        switch alone meant that adding IMESSAGE_ENABLED=true to a WORKING SMS
        deployment silently killed the digest: the transport flipped, the job
        was still scheduled, and every run skipped with "not configured" while
        the health projection went on reporting the digest as enabled.

        This is not the fallback the IMESSAGE_* block forbids. That rule is
        about a send that FAILED — a proxy that is down must never quietly
        become an SMS, because the silence is the signal. A blank URL is not a
        failed send; it is a transport that was never set up, and preferring a
        configured one over nothing loses no information. The unconfigured
        switch is reported loudly by every operator surface rather than being
        absorbed here: see `imessage_enabled_but_unconfigured`."""
        if self.imessage_enabled and self.imessage_configured:
            return "imessage"
        if self.effective_daily_sms_enabled:
            return "sipgate"
        return "none"


#: Settings whose absence disables a transport silently. `extra="ignore"`
#: (model_config, above) means pydantic drops an unrecognised environment key
#: without a word, so `IMESSAG_ENABLED=true` — one character short — reads as
#: "iMessage off". Paired with SMS_ENABLED=false that yields a service which
#: sends nothing and says nothing about why.
_TYPO_PRONE = ("IMESSAGE_ENABLED", "IMESSAGE_API_BASE_URL", "IMESSAGE_API_KEY",
               "IMESSAGE_RECIPIENT", "SMS_ENABLED")


def near_miss_env_keys(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """Environment keys that look like a misspelling of a known setting.

    Returns (actual_key, probable_intent) pairs. Deliberately a plain function
    and not a pydantic validator: app/config.py has no validators, every gate
    in this codebase lives at its use site, and a boot-time hard failure over a
    stray environment key would be a worse outcome than a loud log line."""
    known = {k.lower() for k in Settings.model_fields}
    hits: list[tuple[str, str]] = []
    for key in environ:
        upper = key.upper()
        if upper.lower() in known:
            continue
        for candidate in _TYPO_PRONE:
            if upper == candidate:
                continue
            # One edit apart AND sharing a real prefix. The edit test alone
            # flags SES_ENABLED as a misspelling of SMS_ENABLED — a correctly
            # spelled variable belonging to an unrelated service, since a
            # container's whole environment is searched. A misconfiguration
            # check that cries wolf on legitimate settings is one an operator
            # learns to ignore, which costs more than the typo it was added to
            # catch. Every real case shares a long prefix: IMESSAG_ENABLED (7),
            # IMESSAGE_ENABLE (15), IMESSAGEE_ENABLED (8). SES/SMS share one
            # character. The cost is that a typo in the first few characters is
            # no longer caught, which is the rarer and more visible mistake.
            if (abs(len(upper) - len(candidate)) <= 1
                    and _common_prefix_len(upper, candidate) >= _MIN_SHARED_PREFIX
                    and _within_one_edit(upper, candidate)):
                hits.append((key, candidate))
                break
    return hits


#: Characters a candidate and a suspected typo of it must share up front. Every
#: name in _TYPO_PRONE is at least 11 characters, so this is not restrictive.
_MIN_SHARED_PREFIX = 4


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    # strict=False on purpose: the inputs have different lengths whenever the
    # edit is an insertion or a deletion, which is most of the cases this
    # exists to serve.
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def _within_one_edit(a: str, b: str) -> bool:
    """True when `a` becomes `b` with at most one insertion, deletion or
    substitution. Kept explicit rather than pulling in a Levenshtein
    dependency for a five-line predicate."""
    if a == b:
        return True
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    i = j = 0
    skipped = False
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = True
        j += 1
    return True


@lru_cache
def get_settings() -> Settings:
    return Settings()
