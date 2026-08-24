"""Stage 1 foundations: identities, calendars, GSM-7, artifacts, capture.

These test the properties the rest of the system assumes without re-checking:
that a provider failover cannot manufacture a confirmation, that a TTL is
computed in the right calendar, that a phrase set physically fits, and that
capturing evidence is independent of whether alerting is switched on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app import methodology as _M
from app.alerts import observation as obs
from app.alerts.canonical import canonical_json, identity_hash, is_ulid, new_ulid, sorted_hash_set
from app.alerts.errors import PhraseSetInvalid, RulesetInvalid, sanitize
from app.alerts.gsm7 import Gsm7Error, fits_single_sms, septets
from app.alerts.phrase_registry import validate_phrase_set
from app.alerts.registry import instance_fingerprint, unresolved_pins, validate_ruleset
from tests.conftest import register_promoted

RULES_PATH = "config/alert_rules.v3.2.yaml"
PHRASES_PATH = "config/alert_phrases.v3.2.json"


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def phrase_set():
    with open(PHRASES_PATH, encoding="utf-8") as fh:
        return validate_phrase_set(fh.read())


@pytest.fixture(scope="module")
def ruleset(phrase_set):
    with open(RULES_PATH, encoding="utf-8") as fh:
        raw = fh.read()
    return validate_ruleset(
        raw,
        phrase_set_version=phrase_set.version,
        phrase_set_sha256=phrase_set.sha256,
        methodology_version=_M.get_path("_meta", "methodology_version"),
        methodology_manifest_sha256=_M.frozen_sha256(),
        service_version="3.8.0",
    )


def test_complete_ruleset_inventory(ruleset):
    """Every rule and constellation the mandate names is present, enabled or not."""
    ids = {rule.rule_id for rule in ruleset.rules()}
    for expected in (
        "regime.band_to_derisk", "regime.band_hold_to_trim", "regime.band_trim_to_hold",
        "regime.base_band_moved_while_suppressed", "regime.score_jump_1r",
        "regime.score_trend_7d", "regime.derisk_edge_approach", "regime.iqr_edge_cross",
        "override.first_flag", "override.warning", "override.fires", "override.resolves",
        "legs.faber_spy_out_high_risk", "legs.faber_spy_out_standard", "legs.faber_qqq_out",
        "legs.sma200_flip", "legs.faber_prewarning",
        "tripwire.rf4_first", "tripwire.rf4_persistent", "tripwire.rf3_credit_stress",
        "tripwire.margin_rollover",
        "credit.hy_watch", "credit.ig_watch", "credit.ig_alarm", "credit.ebp_sign_flip",
        "credit.leverage_escalation", "credit.watchlist_stale",
        "structure.cape_record_near", "structure.s2_saturation", "structure.s3_tier_100",
        "structure.s3_tier_150", "structure.s3_approach_100", "structure.s5_percentile_jump",
        "dynamics.breadth_downtrend", "dynamics.d3_gate_fires", "dynamics.d4_lppls_wake",
        "dynamics.d4_band_structure",
        "vol.backwardation", "vol.vrp_nonpositive", "vol.skew_extreme",
        "vol.v_multiplier_change",
        "ops.coverage_risk_masking", "ops.coverage_degraded_info", "ops.indicator_stale",
        "ops.rf_input_unavailable", "ops.period_label_future", "ops.observed_at_future",
        "ops.flag_contract_mismatch", "ops.known_issue_active",
        "ops.condition_unknown_persistent", "ops.missing_input_sidecar",
        "ops.stale_evaluation_abandoned", "ops.provider_cooldown", "ops.recompute_outage",
        "ops.indicator_floor", "ops.rules_invalid", "ops.alerting_unavailable",
        "ops.delivery_unknown",
        "cal.finra_release", "cal.ebp_release", "cal.hyperscaler_earnings",
        "cal.ssga_holdings", "cal.release_missing",
    ):
        assert expected in ids, f"{expected} missing from the ruleset"

    constellation_ids = {
        c.constellation_id for c in ruleset.document.constellations
    }
    assert constellation_ids == {f"C-{n:02d}" for n in range(1, 25)}


def test_no_python_only_rule(ruleset):
    """No rule may exist only in code: the source registry is data-driven and
    every declared source resolves through it."""
    from app.alerts.sources import SOURCE_REGISTRY

    for rule in ruleset.rules():
        for source_id in rule.source_fields:
            assert source_id in SOURCE_REGISTRY


def test_unpinned_rules_are_disabled(ruleset):
    pins = unresolved_pins(ruleset)
    assert pins, "the inventory should still carry unresolved pins"
    for rule_id in pins:
        rule = ruleset.rule(rule_id)
        assert not rule.enabled, f"{rule_id} is enabled while carrying an unresolved pin"


def test_p1_rules_are_never_held(ruleset):
    for rule in ruleset.rules():
        if rule.priority == 1:
            assert rule.quiet_hours_exempt and rule.budget_exempt
        else:
            assert not rule.quiet_hours_exempt and not rule.budget_exempt


def test_hold_source_freshness_required(ruleset):
    for rule in ruleset.rules():
        for name in rule.hold_sources:
            assert rule.freshness_requirements.get(name)


def test_band_rule_reads_effective_action_state(ruleset):
    rule = ruleset.rule("regime.band_to_derisk")
    assert rule.source_fields == ["effective_action_state"]
    assert rule.authoritative is True


def test_hold_trim_suppressed_to_derisk_are_all_p1(ruleset):
    rule = ruleset.rule("regime.band_to_derisk")
    assert set(rule.condition.from_states) == {"hold", "trim", "suppressed"}
    assert rule.condition.to_states == ["de-risk"]
    assert rule.priority == 1


def test_rf3_consumes_persisted_flag(ruleset):
    rule = ruleset.rule("tripwire.rf3_credit_stress")
    assert rule.source_fields == ["rf3_active"]
    assert "hy_oas_bps" not in rule.source_fields


def test_faber_gate_uses_headline_median(ruleset):
    rule = ruleset.rule("legs.faber_spy_out_high_risk")
    assert "headline_median" in rule.source_fields
    assert "point_score" not in rule.source_fields
    gate = next(t for t in rule.thresholds if t.name == "gate_median")
    assert gate.value == 55.0


def test_credit_hy_watch_is_not_rf3(ruleset):
    rule = ruleset.rule("credit.hy_watch")
    assert "rf3_active" not in rule.source_fields
    assert "DISPLAY_ONLY" in rule.required_caveat_codes


def test_rules_hash_is_stable(ruleset):
    with open(RULES_PATH, encoding="utf-8") as fh:
        again = validate_ruleset(
            fh.read(),
            phrase_set_version=ruleset.phrase_set_version,
            phrase_set_sha256=ruleset.phrase_set_sha256,
            methodology_version=_M.get_path("_meta", "methodology_version"),
            methodology_manifest_sha256=_M.frozen_sha256(),
            service_version="3.8.0",
        )
    assert again.rules_sha256 == ruleset.rules_sha256


# ---------------------------------------------------------------------------
# loader rejections
# ---------------------------------------------------------------------------


def _mutate(raw: str, old: str, new: str) -> str:
    assert old in raw
    return raw.replace(old, new, 1)


def _validate(raw: str, phrase_set):
    return validate_ruleset(
        raw,
        phrase_set_version=phrase_set.version,
        phrase_set_sha256=phrase_set.sha256,
        methodology_version=_M.get_path("_meta", "methodology_version"),
        methodology_manifest_sha256=_M.frozen_sha256(),
        service_version="3.8.0",
    )


@pytest.fixture(scope="module")
def raw_rules() -> str:
    with open(RULES_PATH, encoding="utf-8") as fh:
        return fh.read()


def test_loader_rejects_bare_score_source(raw_rules, phrase_set):
    broken = _mutate(raw_rules, "source_fields: [effective_action_state]",
                     "source_fields: [score]")
    with pytest.raises(RulesetInvalid):
        _validate(broken, phrase_set)


def test_loader_rejects_unknown_source(raw_rules, phrase_set):
    broken = _mutate(raw_rules, "source_fields: [rf3_active]",
                     "source_fields: [rf3_actve]")
    with pytest.raises(RulesetInvalid):
        _validate(broken, phrase_set)


def test_loader_rejects_authoritative_hysteresis(raw_rules, phrase_set):
    """A numeric off-level on a persisted DECISION is a second band edge."""
    broken = _mutate(
        raw_rules,
        "    condition: {kind: boolean_state, source: rf3_active, equals: true}",
        "    condition: {kind: threshold, source: rf3_active, op: ge, threshold: x,\n"
        "                off_threshold: y}",
    )
    with pytest.raises(RulesetInvalid):
        _validate(broken, phrase_set)


def test_loader_rejects_p1_that_is_not_exempt(raw_rules, phrase_set):
    broken = _mutate(raw_rules,
                     "    quiet_hours_exempt: true\n    budget_exempt: true\n"
                     "    phrase_set: \"v3.2\"\n    required_caveat_codes: []\n\n"
                     "  - rule_id: regime.band_hold_to_trim",
                     "    quiet_hours_exempt: false\n    budget_exempt: true\n"
                     "    phrase_set: \"v3.2\"\n    required_caveat_codes: []\n\n"
                     "  - rule_id: regime.band_hold_to_trim")
    with pytest.raises(RulesetInvalid):
        _validate(broken, phrase_set)


def test_loader_rejects_unknown_field(raw_rules, phrase_set):
    broken = _mutate(raw_rules, "  - rule_id: regime.band_to_derisk",
                     "  - rule_id: regime.band_to_derisk\n    surprise_field: 1")
    with pytest.raises(RulesetInvalid):
        _validate(broken, phrase_set)


def test_loader_rejects_methodology_mismatch(raw_rules, phrase_set):
    with pytest.raises(RulesetInvalid, match="methodology"):
        validate_ruleset(
            raw_rules,
            phrase_set_version=phrase_set.version,
            phrase_set_sha256=phrase_set.sha256,
            methodology_version="v4-something-else",
            methodology_manifest_sha256=_M.frozen_sha256(),
            service_version="3.8.0",
        )


def test_loader_rejects_service_version_outside_range(raw_rules, phrase_set):
    with pytest.raises(RulesetInvalid, match="service version"):
        validate_ruleset(
            raw_rules,
            phrase_set_version=phrase_set.version,
            phrase_set_sha256=phrase_set.sha256,
            methodology_version=_M.get_path("_meta", "methodology_version"),
            methodology_manifest_sha256=_M.frozen_sha256(),
            service_version="2.0.0",
        )


def test_loader_rejects_dominance_cycle(raw_rules, phrase_set):
    broken = _mutate(raw_rules, "    supersedes: [tripwire.rf4_first]",
                     "    supersedes: [tripwire.rf4_first, tripwire.rf4_all_clear]")
    broken = _mutate(broken,
                     "  - rule_id: tripwire.rf4_all_clear",
                     "  - rule_id: tripwire.rf4_all_clear")
    broken = broken.replace(
        "    resolution: {policy: single_shot}\n    cooldown_seconds: 172800\n"
        "    reminder_policy: {enabled: false, after_seconds: null, max_reminders: 0}\n"
        "    supersedes: []\n    cancel_unsent_superseded: false\n"
        "    group_key: tripwire",
        "    resolution: {policy: single_shot}\n    cooldown_seconds: 172800\n"
        "    reminder_policy: {enabled: false, after_seconds: null, max_reminders: 0}\n"
        "    supersedes: [tripwire.rf4_persistent]\n    cancel_unsent_superseded: false\n"
        "    group_key: tripwire", 1)
    with pytest.raises(RulesetInvalid, match="cycle"):
        _validate(broken, phrase_set)


# ---------------------------------------------------------------------------
# phrase set
# ---------------------------------------------------------------------------


def test_all_templates_fit_worst_case(phrase_set):
    assert phrase_set.worst_case["minimal_assembly"] <= 160
    assert phrase_set.worst_case["full_assembly"] <= 160


def test_every_fragment_is_gsm7(phrase_set):
    for table in (phrase_set.headlines, phrase_set.phrases,
                  phrase_set.next_checks, phrase_set.caveats):
        for fragment in table.values():
            assert fits_single_sms(fragment.text)


def test_phrase_set_rejects_non_gsm7(phrase_set):
    import json

    raw = json.loads(phrase_set.canonical_json)
    raw["headlines"]["BAD"] = {"text": "Kursrückgang bestätigt 📉", "slots": []}
    with pytest.raises(PhraseSetInvalid):
        validate_phrase_set(json.dumps(raw))


def test_phrase_set_rejects_undeclared_fact(phrase_set):
    import json

    raw = json.loads(phrase_set.canonical_json)
    raw["headlines"]["BAD"] = {"text": "Wert {F_MADE_UP}.", "slots": ["F_MADE_UP"]}
    with pytest.raises(PhraseSetInvalid):
        validate_phrase_set(json.dumps(raw))


def test_phrase_set_forbids_an_ambiguous_score_fact(phrase_set):
    import json

    raw = json.loads(phrase_set.canonical_json)
    raw["facts"]["F_SCORE"] = {"label": "Score", "unit": "", "max_width": 5}
    with pytest.raises(PhraseSetInvalid, match="F_SCORE"):
        validate_phrase_set(json.dumps(raw))


def test_median_and_point_score_are_separate_facts(phrase_set):
    assert "F_HEADLINE_MEDIAN" in phrase_set.facts
    assert "F_POINT_SCORE" in phrase_set.facts
    assert "F_SCORE" not in phrase_set.facts


def test_no_advice_or_probability_language(phrase_set):
    banned = ("wahrscheinlich", "kaufen", "verkaufen", "garantiert", "sicher ")
    for table in (phrase_set.headlines, phrase_set.phrases, phrase_set.next_checks):
        for fragment in table.values():
            lowered = fragment.text.lower()
            for word in banned:
                assert word not in lowered, f"{fragment.code} contains {word!r}"


# ---------------------------------------------------------------------------
# GSM-7
# ---------------------------------------------------------------------------


def test_gsm7_extension_cost():
    assert septets("a") == 1
    assert septets("€") == 2          # escape table: ESC + char
    assert septets("[]{}") == 8
    assert septets("äöü") == 3        # basic table, one septet each


def test_gsm7_rejects_ucs2_characters():
    with pytest.raises(Gsm7Error):
        septets("Kursrückgang 📉")
    assert fits_single_sms("Kursrückgang 📉") is False


def test_gsm7_never_transliterates():
    """A body that cannot be encoded raises rather than being rewritten."""
    with pytest.raises(Gsm7Error) as exc:
        septets("Preis 5 ≥ 3")
    assert exc.value.offending == "≥"


def test_a_160_character_body_can_still_be_too_long():
    body = "[" * 80          # 80 characters, 160 septets
    assert len(body) == 80
    assert septets(body) == 160
    assert fits_single_sms(body)
    assert not fits_single_sms(body + "[")


# ---------------------------------------------------------------------------
# observation identity
# ---------------------------------------------------------------------------


def _key(provider: str, payload: str, *, period: str = "2026-08-14") -> tuple[str, str]:
    econ = obs.economic_observation_key(obs.DOMAIN_HY_OAS, period_start=period,
                                        period_end=period)
    rev = obs.source_revision_key(econ, provider_id=provider, provider_vintage=payload,
                                  source_payload_sha256=payload)
    return econ, rev


def test_provider_failover_same_period_does_not_confirm():
    econ_a, rev_a = _key("fred", "payload-a")
    econ_b, rev_b = _key("tiingo", "payload-a")
    assert econ_a == econ_b          # confirmation counts THIS
    assert rev_a != rev_b            # provenance still differs


def test_vendor_revision_same_period_does_not_confirm():
    econ_a, rev_a = _key("fred", "v1")
    econ_b, rev_b = _key("fred", "v2")
    assert econ_a == econ_b
    assert rev_a != rev_b


def test_algorithm_redeploy_same_period_does_not_confirm():
    econ, rev = _key("fred", "v1")
    old = obs.computation_fingerprint(rev, algorithm_version="1", parameter_sha256="p",
                                      code_revision="abc")
    new = obs.computation_fingerprint(rev, algorithm_version="1", parameter_sha256="p",
                                      code_revision="def")
    assert old != new
    # ...but the economic key, which confirmation counts, is untouched.
    assert econ == _key("fred", "v1")[0]


def test_new_economic_period_confirms():
    econ_a, _ = _key("fred", "v1", period="2026-08-14")
    econ_b, _ = _key("fred", "v1", period="2026-08-15")
    assert econ_a != econ_b


def test_unknown_period_does_not_collide_with_epoch():
    unknown = obs.canonical_economic_period(None, None)
    assert unknown == "PERIOD_UNKNOWN"
    assert obs.economic_observation_key(obs.DOMAIN_HY_OAS) != \
        obs.economic_observation_key(obs.DOMAIN_HY_OAS, period_start="1970-01-01",
                                     period_end="1970-01-01")


def test_observation_domains_are_provider_independent():
    for domain in obs.OBSERVATION_DOMAINS:
        for provider in ("fred", "tiingo", "polygon", "twelvedata", "stooq"):
            assert provider not in domain


def test_build_evidence_rejects_an_unknown_domain():
    with pytest.raises(ValueError, match="unknown observation domain"):
        obs.build_evidence("market.made.up", 1.0, observed_at="2026-08-15T00:00:00+00:00")


# ---------------------------------------------------------------------------
# calendars
# ---------------------------------------------------------------------------


def test_us_trading_ttl_does_not_expire_over_a_weekend():
    from app.alerts.calendars import Calendar, resolve_ttl

    friday = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)     # a Friday
    expiry = resolve_ttl(calendar=Calendar.US_TRADING, intervals=2, grace_seconds=0,
                         start=friday)
    # Two SESSIONS later is Tuesday, not Sunday.
    assert expiry.date() >= date(2026, 8, 19)


def test_holidays_are_not_trading_days():
    from app.alerts.calendars import is_trading_day

    assert not is_trading_day(date(2026, 7, 3))     # Independence Day observed (Jul 4 = Sat)
    assert not is_trading_day(date(2026, 12, 25))   # Christmas
    assert not is_trading_day(date(2026, 4, 3))     # Good Friday 2026
    assert not is_trading_day(date(2026, 6, 19))    # Juneteenth
    assert is_trading_day(date(2026, 8, 14))


def test_month_end_is_the_last_session_not_the_last_day():
    from app.alerts.calendars import is_month_end_trading_day

    assert is_month_end_trading_day(date(2026, 5, 29))       # Sat 30 / Sun 31
    assert not is_month_end_trading_day(date(2026, 5, 31))


def test_recompute_slot_ttl_uses_slots_not_hours():
    from app.alerts.calendars import Calendar, resolve_ttl

    start = datetime(2026, 8, 15, 3, 0, tzinfo=UTC)
    expiry = resolve_ttl(calendar=Calendar.RECOMPUTE_SLOT, intervals=2, grace_seconds=0,
                         start=start)
    assert expiry == datetime(2026, 8, 15, 10, 0, tzinfo=UTC)


def test_quiet_hours_hold_exactly_at_22():
    from zoneinfo import ZoneInfo

    from app.alerts.calendars import in_quiet_hours, next_quiet_hours_release

    berlin = ZoneInfo("Europe/Berlin")
    assert in_quiet_hours(datetime(2026, 8, 15, 22, 0, tzinfo=berlin)) is True
    assert in_quiet_hours(datetime(2026, 8, 15, 21, 59, tzinfo=berlin)) is False
    assert in_quiet_hours(datetime(2026, 8, 15, 7, 0, tzinfo=berlin)) is False
    assert in_quiet_hours(datetime(2026, 8, 15, 6, 59, tzinfo=berlin)) is True

    held = datetime(2026, 8, 15, 23, 30, tzinfo=berlin)
    release = next_quiet_hours_release(held).astimezone(berlin)
    assert (release.hour, release.day) == (7, 16)


def test_quiet_hours_follow_dst_not_a_fixed_offset():
    from app.alerts.calendars import next_quiet_hours_release

    # 07:00 Berlin is 06:00Z under CET and 05:00Z under CEST.
    assert next_quiet_hours_release(datetime(2026, 1, 28, 23, 30, tzinfo=UTC)).hour == 6
    assert next_quiet_hours_release(datetime(2026, 7, 28, 23, 30, tzinfo=UTC)).hour == 5
    # Held on the night the clocks go forward (2026-03-29, 02:00 CET -> 03:00
    # CEST): the release must be re-localized on the NEW day, not carried over
    # with the old offset, so it lands at 05:00Z and not 06:00Z.
    across_dst = datetime(2026, 3, 28, 23, 30, tzinfo=UTC)   # 00:30 CET on the 29th
    assert next_quiet_hours_release(across_dst) == datetime(2026, 3, 29, 5, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# canonical helpers
# ---------------------------------------------------------------------------


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_sorted_hash_set_is_order_independent():
    assert sorted_hash_set(["a", "b"]) == sorted_hash_set(["b", "a"])
    assert sorted_hash_set(["a", "a", "b"]) == sorted_hash_set(["a", "b"])


def test_identity_hash_distinguishes_none_from_empty():
    assert identity_hash("x", None, "y") == identity_hash("x", "", "y")  # documented collapse
    assert identity_hash("x", "a") != identity_hash("xa")


def test_ulids_sort_by_mint_order_within_a_millisecond():
    stamp = 1_800_000_000_000
    ids = [new_ulid(stamp) for _ in range(50)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 50
    assert all(is_ulid(i) for i in ids)


def test_instance_fingerprint_is_stable_and_label_sensitive():
    a = instance_fingerprint("legs.faber_spy_out_standard", 1, {"asset": "SPY"})
    b = instance_fingerprint("legs.faber_spy_out_standard", 1, {"asset": "SPY"})
    c = instance_fingerprint("legs.faber_spy_out_standard", 1, {"asset": "QQQ"})
    d = instance_fingerprint("legs.faber_spy_out_standard", 2, {"asset": "SPY"})
    assert a == b
    assert a not in (c, d)


# ---------------------------------------------------------------------------
# sanitization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "failed for +4915112345678",
        "Authorization: Bearer abcdefgh12345678",
        "api_key=supersecretvalue&x=1",
        "https://user:pass@api.sipgate.com/v2/sessions/sms",  # pragma: allowlist secret
        "contact ops@example.com",
        "token: sk-ant-abcdefghijklmnop",
    ],
)
def test_sanitize_removes_secrets_and_pii(raw):
    cleaned = sanitize(raw)
    for leak in ("4915112345678", "abcdefgh12345678", "supersecretvalue", "pass@",
                 "ops@example.com", "sk-ant-abcdefghijklmnop"):
        assert leak not in cleaned


def test_sanitize_bounds_length():
    assert len(sanitize("x" * 5000)) <= 500


# ---------------------------------------------------------------------------
# capture (P0a)
# ---------------------------------------------------------------------------


def _persist_snapshot(isolated_db):
    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=1_000, mc_seed=20260711, gsadf_contested=True)
    return persist_snapshot(data, raw)


def test_sidecar_capture_commits_when_alerts_disabled(isolated_db, monkeypatch):
    """Evidence capture and notification mode are INDEPENDENT switches."""
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    monkeypatch.setenv("ALERTS_MODE", "disabled")
    from app.config import get_settings

    get_settings.cache_clear()

    snap_id = _persist_snapshot(isolated_db)
    from app.services.alert_integration import capture_alert_input

    identity = capture_alert_input(snap_id)
    assert identity

    from app.alerts.models import AlertInputSnapshot
    from app.db import session_scope

    with session_scope() as session:
        row = session.get(AlertInputSnapshot, identity)
        assert row is not None
        assert row.snapshot_id == snap_id
        assert row.evaluation_eligibility == "EVALUABLE"
    get_settings.cache_clear()


def test_capture_is_on_by_default_because_that_is_what_stage_1_is(isolated_db):
    """Stage 1 is "schema, sidecar capture ON, alerts disabled, ... replay".

    Capture off would make the stage inert — no sidecars means nothing to
    replay — while still claiming to have been reached. The default-off rule
    governs the flags that make the service ACT; capture writes one evidence
    row, calls no provider and cannot alter a score.
    """
    from app.config import get_settings

    assert get_settings().alert_input_capture is True
    assert get_settings().alerts_mode == "disabled"      # THIS one stays off

    snap_id = _persist_snapshot(isolated_db)
    from app.services.alert_integration import capture_alert_input

    assert capture_alert_input(snap_id) is not None


def test_the_environment_flag_is_a_kill_switch(isolated_db, monkeypatch):
    """An operator can still stop capture without editing an artifact."""
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    snap_id = _persist_snapshot(isolated_db)
    from app.services.alert_integration import capture_alert_input

    assert capture_alert_input(snap_id) is None
    get_settings.cache_clear()


def test_the_ruleset_can_disable_capture(isolated_db, monkeypatch, tmp_path):
    """`capture.enabled` in the promoted artifact is READ, not decoration.

    An artifact that says capture is on while the code has it off is an
    artifact that lies, which is worse than one that says nothing.
    """
    from pathlib import Path

    from app.config import get_settings

    raw = Path("config/alert_rules.v3.2.yaml").read_text(encoding="utf-8")
    assert "capture:\n  enabled: true" in raw
    off = tmp_path / "rules.yaml"
    off.write_text(raw.replace("capture:\n  enabled: true",
                               "capture:\n  enabled: false", 1), encoding="utf-8")
    monkeypatch.setenv("ALERTS_RULES_PATH", str(off))
    monkeypatch.setenv("ALERTS_PHRASE_PATH", "config/alert_phrases.v3.2.json")
    get_settings.cache_clear()

    snap_id = _persist_snapshot(isolated_db)
    from app.services.alert_integration import capture_alert_input

    assert capture_alert_input(snap_id) is None
    get_settings.cache_clear()


def test_an_unloadable_ruleset_does_not_stop_capture(isolated_db, monkeypatch, tmp_path):
    """Evidence collection is never the dangerous direction.

    The sidecars are exactly what an operator needs to diagnose the ruleset
    that failed to load. Refusing to record them because the rules are broken
    destroys the record of the period you most need to look at, and a lost
    sidecar can never be backfilled.
    """
    from app.config import get_settings

    broken = tmp_path / "broken.yaml"
    broken.write_text("meta: {this: is not a ruleset}\n", encoding="utf-8")
    monkeypatch.setenv("ALERTS_RULES_PATH", str(broken))
    monkeypatch.setenv("ALERTS_LKG_PATH", str(broken))
    get_settings.cache_clear()

    snap_id = _persist_snapshot(isolated_db)
    from app.services.alert_integration import capture_alert_input

    assert capture_alert_input(snap_id) is not None
    get_settings.cache_clear()


def test_capture_is_idempotent(isolated_db, monkeypatch):
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    snap_id = _persist_snapshot(isolated_db)
    from app.services.alert_integration import capture_alert_input

    first = capture_alert_input(snap_id)
    second = capture_alert_input(snap_id)
    assert first == second

    from sqlalchemy import func, select

    from app.alerts.models import AlertInputSnapshot
    from app.db import session_scope

    with session_scope() as session:
        count = session.execute(
            select(func.count()).select_from(AlertInputSnapshot)).scalar_one()
    assert count == 1
    get_settings.cache_clear()


def test_sidecar_is_immutable_at_the_database_level(isolated_db, monkeypatch):
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    snap_id = _persist_snapshot(isolated_db)
    from app.services.alert_integration import capture_alert_input

    identity = capture_alert_input(snap_id)

    import sqlalchemy

    from app.db import session_scope

    with pytest.raises(sqlalchemy.exc.DatabaseError, match="immutable"), \
            session_scope() as session:
        session.execute(
            sqlalchemy.text("UPDATE alert_input_snapshot SET payload = 'tampered' "
                            "WHERE input_identity = :i"),
            {"i": identity},
        )
    get_settings.cache_clear()


def test_alert_failure_does_not_block_snapshot_commit(isolated_db, monkeypatch):
    """A broken alert hook must not cost a snapshot."""
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    import app.services.alert_integration as integration

    def _boom(_snapshot_id: int) -> None:
        raise RuntimeError("alert layer exploded")

    monkeypatch.setattr(integration, "on_snapshot_committed", _boom)

    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=1_000, mc_seed=20260711, gsadf_contested=True)
    snap_id = persist_snapshot(data, raw)      # must not raise
    assert snap_id
    get_settings.cache_clear()


def test_sidecar_carries_the_typed_contract_not_the_display_string(isolated_db, monkeypatch):
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    snap_id = _persist_snapshot(isolated_db)
    from app.services.alert_integration import capture_alert_input

    identity = capture_alert_input(snap_id)

    import json

    from app.alerts.dto import AlertInput
    from app.alerts.models import AlertInputSnapshot
    from app.db import session_scope

    with session_scope() as session:
        row = session.get(AlertInputSnapshot, identity)
        payload = AlertInput.model_validate(json.loads(row.payload))

    assert payload.effective_action_state == "trim"
    assert payload.headline_median is not None
    assert payload.point_score is not None
    assert payload.headline_median != payload.point_score   # separate facts, always
    assert {f.flag_id for f in payload.red_flags} == {"rf1", "rf2", "rf3", "rf4"}
    assert payload.red_flag("rf1").fireable is False        # no GSADF statistic in the fixture
    get_settings.cache_clear()


def test_busy_timeout_is_configured(isolated_db):
    import sqlalchemy

    from app.db import get_engine

    with get_engine().connect() as conn:
        timeout = conn.execute(sqlalchemy.text("PRAGMA busy_timeout")).scalar_one()
        wal = conn.execute(sqlalchemy.text("PRAGMA journal_mode")).scalar_one()
        fks = conn.execute(sqlalchemy.text("PRAGMA foreign_keys")).scalar_one()
    assert timeout > 0
    assert str(wal).lower() == "wal"
    assert fks == 1


def test_an_inert_confirmation_basis_is_reported_not_silently_accepted(ruleset):
    """A basis naming an economic period is enforced by the candidate latch,
    and mandate 8.1 scopes that latch to `count > 1`.

    At count 1 there is no candidate: the rule fires on the transition and the
    basis is never consulted, so the declaration reads as a control and is not.
    What actually limits repeat notifications there is `cooldown_seconds`.

    Warned rather than rejected — a third of the shipped rules are written this
    way and all carry a real cooldown, so it is a documentation defect in the
    artifact, not a broken rule. But it must not pass in silence: the point is
    that a reader should not believe an enforcement that is not happening.
    """
    inert = [w for w in ruleset.warnings if "no candidate latch enforces it" in w]
    assert inert, "an inert period basis must be reported"

    # every warning names the cooldown that is doing the actual limiting
    for warning in inert:
        assert "cooldown_seconds=" in warning

    # transition bases are NOT flagged: one transition is exactly what they mean
    assert not [w for w in inert if "authoritative_transition" in w]
    assert not [w for w in inert if "adjacent_snapshots" in w]


def test_live_mode_refuses_a_ruleset_that_was_never_promoted(isolated_db):
    """Promotion is the operator's deliberate act, or it is decoration.

    Without this check a valid but UNPROMOTED candidate placed on disk is
    evaluated and dispatched exactly like an approved one — health reported
    `live_matches_promoted` and nothing enforced it (audit B-04). Shadow and
    dryrun may run an unpromoted candidate; that is what they are for.
    """
    import pytest

    from app.alerts.artifacts import load_active_for_mode
    from app.alerts.errors import AlertingUnavailable
    from app.db import session_scope

    with session_scope() as session:
        # nothing has been promoted yet
        assert load_active_for_mode(session, mode="shadow") is not None
        assert load_active_for_mode(session, mode="dryrun") is not None

        with pytest.raises(AlertingUnavailable) as caught:
            load_active_for_mode(session, mode="live")
        assert "PROMOTED" in str(caught.value)


def test_live_mode_accepts_the_ruleset_once_it_is_promoted(isolated_db):
    from app.alerts.artifacts import load_active, load_active_for_mode
    from app.db import session_scope

    with session_scope() as session:
        artifacts = load_active(session)
        register_promoted(session, artifacts)

    with session_scope() as session:
        admitted = load_active_for_mode(session, mode="live")
        assert admitted.ruleset.rules_sha256 == artifacts.ruleset.rules_sha256


def test_load_active_offers_no_mode_argument_it_would_ignore():
    """A parameter that looks like a control and is not is worse than none.

    An earlier version accepted `mode` and ignored it, so a caller writing
    `load_active(session, mode="live")` read as guarded and received an
    unpromoted candidate. The live check lives in `load_active_for_mode`, and
    the only way to be sure it is not skipped is for the mode-blind loader to
    refuse the argument outright.
    """
    import inspect

    from app.alerts.artifacts import load_active, load_active_for_mode

    assert "mode" not in inspect.signature(load_active).parameters
    assert "mode" in inspect.signature(load_active_for_mode).parameters


def test_live_admission_binds_the_phrase_set_and_not_only_the_rules(isolated_db):
    """The rules decide WHETHER to alert; the phrase set decides what it SAYS.

    Binding only `rules_sha256` admits a ruleset whose rules were promoted
    while its text was not — and the text is the half that reaches the phone.
    """
    from dataclasses import replace

    import pytest

    from app.alerts import artifacts as artifacts_module
    from app.alerts.artifacts import load_active, load_active_for_mode
    from app.alerts.errors import AlertingUnavailable
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register_promoted(session, loaded)
        session.flush()

        # Same rules, different text: the one case the old check waved through.
        drifted = replace(loaded.ruleset, phrase_set_sha256="d" * 64)
        original = artifacts_module.load_active
        artifacts_module.load_active = (
            lambda s, **kw: replace(loaded, ruleset=drifted))
        try:
            with pytest.raises(AlertingUnavailable) as caught:
                load_active_for_mode(session, mode="live")
        finally:
            artifacts_module.load_active = original

    message = str(caught.value)
    assert "phrase set" in message
    assert "text change that was never promoted" in message
