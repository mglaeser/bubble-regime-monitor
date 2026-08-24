"""Planning, rendering and transport classification.

These are the properties that decide whether the SMS that arrives says
something true, and whether a lost response becomes a duplicate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.alerts.budgets import BudgetLimits, BudgetUsage, check_budget, user_load
from app.alerts.dominance import resolve
from app.alerts.enums import (
    DeliveryKind,
    PlanningState,
    SenderOutcome,
    SuppressionReason,
)
from app.alerts.errors import RenderRejected
from app.alerts.phrase_registry import validate_phrase_set
from app.alerts.planner import (
    NotificationMemory,
    PlanInputs,
    dedupe_key,
    plan,
)
from app.alerts.quiet_hours import release_time_for, would_be_held
from app.alerts.render_context import MemberContext, RenderContext, build_member_context
from app.alerts.renderer import honesty_lint, render, render_with_cascade
from app.alerts.rulespec import RuleSpec
from app.alerts.sender import NullSender, SipgateSender, classify_response
from app.alerts.state_machine import StateDecision
from tests.test_alert_evaluation import _rule, make_input

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)          # 12:00 Berlin — allowed
NIGHT = datetime(2026, 8, 15, 23, 30, tzinfo=UTC)       # 01:30 Berlin — quiet


@pytest.fixture(scope="module")
def phrase_set():
    with open("config/alert_phrases.v3.3.json", encoding="utf-8") as fh:
        return validate_phrase_set(fh.read())


# ---------------------------------------------------------------------------
# budgets and quiet hours
# ---------------------------------------------------------------------------


LIMITS = BudgetLimits(target_168h=2, cap_24h=3, cap_168h=6)


def test_p1_is_never_blocked_by_budget():
    exhausted = BudgetUsage(sent_24h=99, sent_168h=99, reserved=9, digest_168h=1)
    decision = check_budget(1, exhausted, LIMITS)
    assert decision.allowed is True
    assert decision.reason == "p1_exempt"


def test_non_p1_respects_both_caps():
    assert check_budget(2, BudgetUsage(2, 2, 0, 0), LIMITS).allowed is True
    assert check_budget(2, BudgetUsage(3, 3, 0, 0), LIMITS).reason == "cap_24h"
    assert check_budget(2, BudgetUsage(0, 6, 0, 0), LIMITS).reason == "cap_168h"


def test_reservations_count_against_the_budget():
    """Two messages already in flight must not be spent twice."""
    assert check_budget(2, BudgetUsage(2, 2, 1, 0), LIMITS).reason == "cap_24h"


def test_digest_is_reported_as_load_but_does_not_consume_the_caps():
    usage = BudgetUsage(sent_24h=2, sent_168h=5, reserved=0, digest_168h=1)
    assert check_budget(2, usage, LIMITS).allowed is True
    assert user_load(usage)["total_168h"] == 6


def test_p1_is_never_held_by_quiet_hours():
    assert would_be_held(1, NIGHT) is False
    assert release_time_for(1, NIGHT) == NIGHT


def test_p2_is_held_at_night_and_released_at_seven_berlin():
    assert would_be_held(2, NIGHT) is True
    released = release_time_for(2, NIGHT)
    assert released > NIGHT
    from zoneinfo import ZoneInfo

    assert released.astimezone(ZoneInfo("Europe/Berlin")).hour == 7


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------


def _member(episode_id="EP1", generation=1, role="PRIMARY", rules="r1"):
    from app.alerts.planner import MemberIntent

    return MemberIntent(
        episode_id=episode_id, rule_id="test.rule", instance_fingerprint="fp",
        member_role=role, notification_generation=generation,
        origin_rules_sha256=rules, origin_phrase_set_version="v3.2",
        origin_phrase_set_sha256="p1", priority=2,
    )


def test_dedupe_key_is_stable_across_an_automatic_retry():
    """A provider retry reuses the row, so the key must not move."""
    first = dedupe_key(delivery_kind=DeliveryKind.INITIAL, members=[_member()],
                       scheduled_window_key=None, manual_retry_sequence=0)
    again = dedupe_key(delivery_kind=DeliveryKind.INITIAL, members=[_member()],
                       scheduled_window_key=None, manual_retry_sequence=0)
    assert first == again


def test_reminder_generation_changes_the_dedupe_key():
    initial = dedupe_key(delivery_kind=DeliveryKind.INITIAL, members=[_member()],
                         scheduled_window_key=None, manual_retry_sequence=0)
    reminder = dedupe_key(delivery_kind=DeliveryKind.REMINDER,
                          members=[_member(generation=2)],
                          scheduled_window_key=None, manual_retry_sequence=0)
    assert initial != reminder


def test_manual_retry_sequence_changes_the_dedupe_key():
    """An operator-authorized retry must not collide with the original intent."""
    original = dedupe_key(delivery_kind=DeliveryKind.INITIAL, members=[_member()],
                          scheduled_window_key=None, manual_retry_sequence=0)
    retry = dedupe_key(delivery_kind=DeliveryKind.INITIAL, members=[_member()],
                       scheduled_window_key=None, manual_retry_sequence=1)
    assert original != retry


def test_a_new_digest_window_changes_the_dedupe_key():
    first = dedupe_key(delivery_kind=DeliveryKind.DIGEST, members=[_member()],
                       scheduled_window_key="2026-W33", manual_retry_sequence=0)
    second = dedupe_key(delivery_kind=DeliveryKind.DIGEST, members=[_member()],
                        scheduled_window_key="2026-W34", manual_retry_sequence=0)
    assert first != second


def test_member_order_does_not_change_the_dedupe_key():
    a, b = _member("EP1"), _member("EP2")
    assert dedupe_key(delivery_kind=DeliveryKind.BUNDLE, members=[a, b],
                      scheduled_window_key=None, manual_retry_sequence=0) == \
        dedupe_key(delivery_kind=DeliveryKind.BUNDLE, members=[b, a],
                   scheduled_window_key=None, manual_retry_sequence=0)


# ---------------------------------------------------------------------------
# dominance
# ---------------------------------------------------------------------------


def test_dominance_is_transitive_over_the_declared_graph():
    """B not firing must not rescue C from A."""
    a = _rule(rule_id="a", supersedes=["b"])
    b = _rule(rule_id="b", supersedes=["c"])
    c = _rule(rule_id="c")
    declared = {r.rule_id: r for r in (a, b, c)}

    outcome = resolve([a, c], declared)          # b is not firing
    assert "c" in outcome.suppressed
    assert outcome.winners == frozenset({"a"})

    outcome_all = resolve([a, b, c], declared)
    assert set(outcome_all.suppressed) == {"b", "c"}


def test_dominance_records_cancel_unsent_only_when_declared():
    a = _rule(rule_id="a", supersedes=["b"], cancel_unsent_superseded=True)
    b = _rule(rule_id="b")
    assert resolve([a, b], {"a": a, "b": b}).cancel_unsent == frozenset({"b"})

    quiet = _rule(rule_id="a", supersedes=["b"], cancel_unsent_superseded=False)
    assert resolve([quiet, b], {"a": quiet, "b": b}).cancel_unsent == frozenset()


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def _decision(rule_id: str, fingerprint: str) -> StateDecision:
    return StateDecision(
        rule_id=rule_id, instance_fingerprint=fingerprint,
        evaluation_status="OK", condition_state="FIRING",
        previous_condition_state="NORMAL", expected_state_version=0,
        activate_episode=True,
    )


def _inputs(rules: list[RuleSpec], *, now=NOW, **overrides) -> PlanInputs:
    decisions = [_decision(r.rule_id, f"fp-{r.rule_id}") for r in rules]
    base = {
        "now": now,
        "rules": {r.rule_id: r for r in rules},
        "decisions": decisions,
        "episode_ids": {f"fp-{r.rule_id}": f"EP-{r.rule_id}" for r in rules},
        "memories": {f"fp-{r.rule_id}": NotificationMemory() for r in rules},
        "origin_rules_sha256": "rules-hash",
        "phrase_set_version": "v3.2",
        "phrase_set_sha256": "phrase-hash",
        "budget_limits": LIMITS,
    }
    base.update(overrides)
    return PlanInputs(**base)


def _p1(rule_id="p1.rule"):
    return _rule(rule_id=rule_id, priority=1, quiet_hours_exempt=True, budget_exempt=True)


def _p2(rule_id="p2.rule", group="regime"):
    return _rule(rule_id=rule_id, priority=2, group_key=group)


def test_p1_is_ready_immediately_even_at_night_and_out_of_budget():
    result = plan(_inputs([_p1()], now=NIGHT,
                          budget_usage=BudgetUsage(99, 99, 9, 0)))
    assert len(result.deliveries) == 1
    delivery = result.deliveries[0]
    assert delivery.priority == 1
    assert delivery.planning_state == PlanningState.READY
    assert delivery.not_before == NIGHT


def test_p2_is_held_at_night_with_a_release_time():
    result = plan(_inputs([_p2()], now=NIGHT))
    delivery = result.deliveries[0]
    assert delivery.planning_state == PlanningState.HELD_QUIET
    assert delivery.hold_reason_code == "quiet_hours"
    assert delivery.not_before > NIGHT


def test_p2_is_held_by_budget_during_the_day():
    result = plan(_inputs([_p2()], budget_usage=BudgetUsage(3, 3, 0, 0)))
    delivery = result.deliveries[0]
    assert delivery.planning_state == PlanningState.HELD_BUDGET
    assert delivery.hold_reason_code == "cap_24h"


def test_p2_bundles_by_group_key():
    rules = [_p2("p2.a", "regime"), _p2("p2.b", "regime"), _p2("p2.c", "credit")]
    result = plan(_inputs(rules))
    kinds = {d.delivery_kind: len(d.members) for d in result.deliveries}
    assert kinds == {DeliveryKind.BUNDLE: 2, DeliveryKind.INITIAL: 1}


def test_p3_creates_a_digest_item_not_a_delivery():
    p3 = _rule(rule_id="p3.rule", priority=3)
    result = plan(_inputs([p3]))
    assert result.deliveries == []
    assert len(result.digest_items) == 1
    assert result.digest_items[0].digest_window_key.startswith("2026-W")


def test_p4_produces_neither():
    p4 = _rule(rule_id="p4.rule", priority=4)
    result = plan(_inputs([p4]))
    assert result.deliveries == [] and result.digest_items == []


def test_a_silenced_rule_is_suppressed_not_delivered():
    from app.alerts.silences import ActiveSilences

    rule = _p2()
    result = plan(_inputs(
        [rule], active_silences=ActiveSilences(rule_ids=frozenset({rule.rule_id}))))
    assert result.deliveries == []
    assert SuppressionReason.SILENCED in result.suppressions[f"EP-{rule.rule_id}"]


def test_cooldown_suppresses_a_repeat():
    rule = _p2()
    memory = NotificationMemory(last_sent_at=NOW - timedelta(minutes=5))
    result = plan(_inputs([rule], memories={f"fp-{rule.rule_id}": memory}))
    assert result.deliveries == []
    assert SuppressionReason.COOLDOWN in result.suppressions[f"EP-{rule.rule_id}"]


def test_flapping_suppresses_the_notification_not_the_condition():
    rule = _p2()
    result = plan(_inputs([rule],
                          flapping_fingerprints=frozenset({f"fp-{rule.rule_id}"})))
    assert result.deliveries == []
    assert SuppressionReason.FLAPPING in result.suppressions[f"EP-{rule.rule_id}"]


def test_same_generation_unknown_blocks_replanning():
    rule = _p2()
    memory = NotificationMemory(open_unknown_delivery_id="D1", open_unknown_priority=2)
    result = plan(_inputs([rule], memories={f"fp-{rule.rule_id}": memory}))
    assert result.deliveries == []
    assert SuppressionReason.UNKNOWN_BLOCK in result.suppressions[f"EP-{rule.rule_id}"]


def test_a_new_p1_can_bypass_a_lower_priority_unknown_with_the_risk_recorded():
    rule = _p1()
    memory = NotificationMemory(open_unknown_delivery_id="D1", open_unknown_priority=2)
    result = plan(_inputs([rule], memories={f"fp-{rule.rule_id}": memory}))
    assert len(result.deliveries) == 1
    delivery = result.deliveries[0]
    assert delivery.duplicate_risk_acknowledged is True
    assert delivery.prior_unknown_delivery_id == "D1"


def test_a_p1_does_not_bypass_another_p1_unknown():
    rule = _p1()
    memory = NotificationMemory(open_unknown_delivery_id="D1", open_unknown_priority=1)
    result = plan(_inputs([rule], memories={f"fp-{rule.rule_id}": memory}))
    assert result.deliveries == []


def test_an_already_open_generation_is_not_recreated():
    rule = _p2()
    result = plan(_inputs([rule],
                          open_generations=frozenset({(f"fp-{rule.rule_id}", 1)})))
    assert result.deliveries == []


def test_dominance_suppresses_the_loser_in_the_plan():
    winner = _p1("legs.high_risk")
    winner = _rule(rule_id="legs.high_risk", priority=1, quiet_hours_exempt=True,
                   budget_exempt=True, supersedes=["legs.standard"],
                   cancel_unsent_superseded=True)
    loser = _p2("legs.standard", "legs")
    result = plan(_inputs([winner, loser]))
    assert [d.priority for d in result.deliveries] == [1]
    assert SuppressionReason.SUPERSEDED in result.suppressions["EP-legs.standard"]
    assert result.cancel_unsent_for == frozenset({"legs.standard"})


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _context(phrase_set, *, codes=("BAND_TO_DERISK",), caveats=(), members=1):
    trigger = make_input(identity="i1", effective="de-risk", median=61.0, point=59.0)
    built = [
        build_member_context(
            episode_id=f"EP{i}", rule_id=f"rule.{i}", priority=2,
            trigger=trigger, current=trigger,
            authorized_phrase_codes=frozenset(codes),
            required_caveat_codes=tuple(caveats),
            condition_status="STILL_FIRING",
            origin_phrase_set_version=phrase_set.version,
            origin_phrase_set_sha256=phrase_set.sha256,
            origin_rules_sha256="rules-hash",
        )
        for i in range(members)
    ]
    return RenderContext(members=built)


def test_render_produces_a_gsm7_message_within_one_sms(phrase_set):
    codes = ("BAND_TO_DERISK", "MEDIAN_CONTEXT", "NEXT_RECOMPUTE")
    context = _context(phrase_set, codes=codes)
    # BAND_TO_DERISK needs F_BAND_PREVIOUS, which the trigger cannot supply.
    context.members[0].facts["F_BAND_PREVIOUS"] = "trim"
    object.__setattr__(context.members[0], "authorized_fact_ids",
                       frozenset(context.members[0].facts))
    result = render(context=context, phrase_set=phrase_set,
                    headline_code="BAND_TO_DERISK",
                    phrase_codes=["MEDIAN_CONTEXT"],
                    next_check_code="NEXT_RECOMPUTE", caveat_codes=[])
    assert result.septet_count <= 160
    assert "61.0" in result.body
    assert "de-risk" in result.body


def test_bundle_member_fact_isolation(phrase_set):
    """Member 1's number may never appear in member 0's phrase."""
    context = _context(phrase_set, codes=("BAND_TO_DERISK", "BREADTH_CONTEXT"),
                       members=2)
    primary = context.members[0]
    object.__setattr__(primary, "facts", {"F_BAND_EFFECTIVE": "de-risk",
                                          "F_BAND_PREVIOUS": "trim"})
    object.__setattr__(primary, "authorized_fact_ids", frozenset(primary.facts))
    with pytest.raises(RenderRejected, match="not authorized"):
        render(context=context, phrase_set=phrase_set,
               headline_code="BAND_TO_DERISK",
               phrase_codes=["BREADTH_CONTEXT"],   # needs F_BREADTH, not authorized
               next_check_code=None, caveat_codes=[])


def test_unauthorized_code_is_rejected(phrase_set):
    context = _context(phrase_set, codes=("BAND_TO_DERISK",))
    context.members[0].facts["F_BAND_PREVIOUS"] = "trim"
    object.__setattr__(context.members[0], "authorized_fact_ids",
                       frozenset(context.members[0].facts))
    with pytest.raises(RenderRejected, match="not authorized"):
        render(context=context, phrase_set=phrase_set,
               headline_code="BAND_TO_DERISK",
               phrase_codes=["LPPLS_LONG_LEADS"],       # not in the authorized set
               next_check_code=None, caveat_codes=[])


def test_unknown_code_is_rejected(phrase_set):
    context = _context(phrase_set, codes=("MADE_UP",))
    with pytest.raises(RenderRejected, match="unknown headline"):
        render(context=context, phrase_set=phrase_set, headline_code="MADE_UP",
               phrase_codes=[], next_check_code=None, caveat_codes=[])


def test_required_caveat_is_always_present(phrase_set):
    trigger = make_input(identity="i1", effective="suppressed", degraded=True,
                         suppressed=True)
    member = build_member_context(
        episode_id="EP1", rule_id="ops.coverage_risk_masking", priority=2,
        trigger=trigger, current=trigger,
        authorized_phrase_codes=frozenset({"COVERAGE_RISK_MASKING"}),
        required_caveat_codes=("DATA_DEGRADED",), condition_status="STILL_FIRING",
        origin_phrase_set_version=phrase_set.version,
        origin_phrase_set_sha256=phrase_set.sha256, origin_rules_sha256="r")
    assert "DATA_DEGRADED" in member.required_caveat_codes
    result = render(context=RenderContext(members=[member]), phrase_set=phrase_set,
                    headline_code="COVERAGE_RISK_MASKING", phrase_codes=[],
                    next_check_code=None, caveat_codes=[])
    assert "Datenlage eingeschraenkt" in result.body


def test_degraded_data_adds_a_caveat_without_being_asked(phrase_set):
    trigger = make_input(identity="i1", effective="de-risk", degraded=True)
    member = build_member_context(
        episode_id="EP1", rule_id="r", priority=2, trigger=trigger, current=trigger,
        authorized_phrase_codes=frozenset(), required_caveat_codes=(),
        condition_status="STILL_FIRING", origin_phrase_set_version=phrase_set.version,
        origin_phrase_set_sha256=phrase_set.sha256, origin_rules_sha256="r")
    assert "DATA_DEGRADED" in member.required_caveat_codes


def test_incompatible_current_input_falls_back_to_trigger_values(phrase_set):
    trigger = make_input(identity="i1", effective="de-risk", median=61.0)
    current = make_input(identity="i2", effective="trim", median=40.0)
    object.__setattr__(current, "methodology_sha256", "a-different-methodology")
    member = build_member_context(
        episode_id="EP1", rule_id="r", priority=2, trigger=trigger, current=current,
        authorized_phrase_codes=frozenset(), required_caveat_codes=(),
        condition_status="MATERIALLY_CHANGED_BUT_ACTIVE",
        origin_phrase_set_version=phrase_set.version,
        origin_phrase_set_sha256=phrase_set.sha256, origin_rules_sha256="r")
    assert member.facts["F_HEADLINE_MEDIAN"] == "61.0"     # the trigger value
    assert "CONTEXT_STALE" in member.required_caveat_codes


def test_median_and_point_score_are_separate_facts(phrase_set):
    trigger = make_input(identity="i1", median=61.0, point=59.0)
    member = build_member_context(
        episode_id="EP1", rule_id="r", priority=2, trigger=trigger, current=trigger,
        authorized_phrase_codes=frozenset(), required_caveat_codes=(),
        condition_status="STILL_FIRING", origin_phrase_set_version=phrase_set.version,
        origin_phrase_set_sha256=phrase_set.sha256, origin_rules_sha256="r")
    assert member.facts["F_HEADLINE_MEDIAN"] == "61.0"
    assert member.facts["F_POINT_SCORE"] == "59.0"
    assert "F_SCORE" not in member.facts


def test_honesty_lint_rejects_probability_and_advice_language():
    assert honesty_lint("Regime unveraendert.") is None
    assert honesty_lint("Ein Crash ist wahrscheinlich.") is not None
    assert honesty_lint("Jetzt verkaufen.") is not None
    assert honesty_lint("Der Ausgang ist sicher.") is not None


def test_render_cascade_falls_back_to_minimal(phrase_set):
    """A rejected full render degrades; it never invents a way to say something."""
    context = _context(phrase_set, codes=("BAND_TO_DERISK",))
    context.members[0].facts["F_BAND_PREVIOUS"] = "trim"
    object.__setattr__(context.members[0], "authorized_fact_ids",
                       frozenset(context.members[0].facts))
    result = render_with_cascade(
        context=context, phrase_set=phrase_set, headline_code="BAND_TO_DERISK",
        phrase_codes=["LPPLS_LONG_LEADS"],       # unauthorized -> full render fails
        next_check_code=None, caveat_codes=[])
    assert result.render_source == "template_minimal"
    assert result.fallback_reason.startswith("full_render_rejected")
    assert result.septet_count <= 160


def test_optional_fragments_are_omitted_whole_never_truncated(phrase_set):
    context = _context(phrase_set, codes=("RF3_CREDIT_STRESS", "MEDIAN_CONTEXT",
                                          "BREADTH_CONTEXT", "CREDIT_CONTEXT",
                                          "SEMIS_CONTEXT", "NEXT_RECOMPUTE"))
    member = context.members[0]
    facts = dict(member.facts)
    facts.update({"F_RF3_DISTANCE": "-12.4", "F_BREADTH": "56.0", "F_HY_OAS": "267",
                  "F_S3": "108.0"})
    object.__setattr__(member, "facts", facts)
    object.__setattr__(member, "authorized_fact_ids", frozenset(facts))
    result = render(context=context, phrase_set=phrase_set,
                    headline_code="RF3_CREDIT_STRESS",
                    phrase_codes=["MEDIAN_CONTEXT", "BREADTH_CONTEXT",
                                  "CREDIT_CONTEXT", "SEMIS_CONTEXT"],
                    next_check_code="NEXT_RECOMPUTE", caveat_codes=[])
    assert result.septet_count <= 160
    # Whatever survived is a COMPLETE fragment: no fragment ends mid-word.
    for code in result.selected_phrase_codes:
        fragment = phrase_set.fragment(code)
        if fragment is not None and not fragment.slots:
            assert fragment.text in result.body


# ---------------------------------------------------------------------------
# transport classification
# ---------------------------------------------------------------------------


def test_only_the_contract_status_is_confirmed_success():
    """Sipgate's contract is 204 No Content, exactly.

    This test used to assert that a 200 counted as success too. A 200 did not
    come from the send route — a captive portal, a health page, a wrong base
    URL — and calling it success records an alert as delivered while it went
    nowhere. The iMessage classifier had already fixed this; sipgate's kept
    the defect, and this assertion kept it green.
    """
    assert classify_response(204, "").outcome == SenderOutcome.CONFIRMED_SUCCESS
    other = classify_response(200, "{}")
    assert other.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert other.error_code == "UNEXPECTED_SUCCESS_STATUS"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_definite_rejections_are_permanent(status):
    result = classify_response(status, "bad request")
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert result.may_retry_automatically is False


def test_a_429_is_a_definite_decline_and_safe_to_repeat():
    result = classify_response(429, "try later")
    assert result.outcome == SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED
    assert result.may_retry_automatically is True


@pytest.mark.parametrize("status", [301, 500, 502, 503])
def test_a_transmitted_post_answered_oddly_is_ambiguous(status):
    """A 3xx or 5xx follows a fully transmitted POST.

    The message may already have been accepted, so auto-retrying is the
    duplicate the four-outcome contract exists to prevent. These used to be
    classified DEFINITE_TRANSIENT and retried unattended.
    """
    result = classify_response(status, "gateway says no")
    assert result.outcome == SenderOutcome.AMBIGUOUS_AFTER_TRANSMISSION
    assert result.may_retry_automatically is False


@pytest.fixture()
def sipgate_configured(isolated_db, monkeypatch):
    monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-test")
    monkeypatch.setenv("SIPGATE_TOKEN", "secret-test")            # pragma: allowlist secret
    monkeypatch.setenv("SIPGATE_RECIPIENT", "+490000000000")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_a_connect_failure_is_transient_not_ambiguous(sipgate_configured):
    """Nothing left this host, so a retry cannot duplicate."""
    def handler(_request):
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = SipgateSender(client=client).send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED
    assert result.request_started is False


def test_a_lost_response_after_transmission_is_ambiguous(sipgate_configured):
    """The bytes may have landed. This is NEVER auto-retried."""
    def handler(_request):
        raise httpx.ReadTimeout("no answer")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = SipgateSender(client=client).send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.AMBIGUOUS_AFTER_TRANSMISSION
    assert result.may_retry_automatically is False
    assert result.is_ambiguous is True


def test_unconfigured_credentials_are_a_permanent_rejection(isolated_db):
    result = SipgateSender().send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert result.error_code == "NOT_CONFIGURED"


def test_error_messages_are_sanitized_before_they_can_be_stored():
    result = classify_response(400, "rejected for +4915112345678, token=abcdefgh12345")
    assert "4915112345678" not in result.error_message_redacted
    assert "abcdefgh12345" not in result.error_message_redacted


def test_null_sender_records_without_sending():
    sender = NullSender()
    result = sender.send("hallo", recipient_ref="default")
    assert result.is_success
    assert sender.sent == [("default", "hallo")]


def test_dry_run_sender_never_constructs_a_live_client():
    from app.alerts.sender import default_sender

    assert isinstance(default_sender(live=False), NullSender)
    # The invariant is about the DRY RUN: nothing reaches a transport unless
    # live was asked for. Which live transport is chosen depends on what is
    # enabled and configured, and in an unconfigured test environment the
    # honest answer is "none" rather than sipgate — see
    # test_a_half_configured_imessage_never_falls_back_to_disabled_sms.
    assert not isinstance(default_sender(live=True), NullSender)


def test_the_recipient_number_never_enters_a_result():
    import inspect

    from app.alerts import sender as sender_module

    source = inspect.getsource(sender_module.SendResult)
    assert "recipient" not in source
    assert "recipient_ref" in inspect.getsource(sender_module._resolve_recipient)


def test_member_context_refuses_a_foreign_fact():
    member = MemberContext(
        episode_id="EP1", rule_id="r", priority=2,
        facts={"F_HEADLINE_MEDIAN": "61.0"},
        authorized_fact_ids=frozenset({"F_HEADLINE_MEDIAN"}),
        authorized_phrase_codes=frozenset(), required_caveat_codes=(),
        condition_status="STILL_FIRING", origin_phrase_set_version="v3.2",
        origin_phrase_set_sha256="p", origin_rules_sha256="r")
    assert member.fact("F_HEADLINE_MEDIAN") == "61.0"
    assert member.fact("F_BREADTH") is None


# ---------------------------------------------------------------------------
# outbox and watchdog
# ---------------------------------------------------------------------------


def test_watchdog_needs_two_missed_slots_and_the_grace_window():
    from app.alerts.watchdog import evaluate_outage

    last = datetime(2026, 8, 15, 6, 0, 5, tzinfo=UTC)      # right after the 06:00 slot

    # One missed slot (10:00) — not yet.
    assert evaluate_outage(last_snapshot_at=last,
                           now=datetime(2026, 8, 15, 11, 0, tzinfo=UTC)).firing is False
    # Two missed (10:00, 14:00) but inside the 90-minute grace after 14:00.
    inside = evaluate_outage(last_snapshot_at=last,
                             now=datetime(2026, 8, 15, 15, 0, tzinfo=UTC))
    assert inside.firing is False and inside.missed_slots == 2
    # Two missed, grace elapsed.
    fired = evaluate_outage(last_snapshot_at=last,
                            now=datetime(2026, 8, 15, 15, 40, tzinfo=UTC))
    assert fired.firing is True and fired.missed_slots == 2


def test_watchdog_does_not_fire_without_any_snapshot():
    from app.alerts.watchdog import evaluate_outage

    verdict = evaluate_outage(last_snapshot_at=None, now=NOW)
    assert verdict.firing is False
    assert verdict.missed_slots == 0
    assert "no snapshot" in verdict.reason


def test_watchdog_identity_is_stable_for_the_same_missed_slot():
    """Firing twice for one outage must not open a second episode."""
    from app.alerts.dto import watchdog_input_identity
    from app.alerts.watchdog import evaluate_outage

    last = datetime(2026, 8, 15, 6, 0, 5, tzinfo=UTC)
    first = evaluate_outage(last_snapshot_at=last,
                            now=datetime(2026, 8, 15, 15, 40, tzinfo=UTC))
    again = evaluate_outage(last_snapshot_at=last,
                            now=datetime(2026, 8, 15, 15, 55, tzinfo=UTC))
    assert first.expected_slot_key == again.expected_slot_key
    assert watchdog_input_identity(first.expected_slot_key) == \
        watchdog_input_identity(again.expected_slot_key)


def test_hold_for_budget_refuses_a_p1():
    from app.alerts.models import AlertDelivery
    from app.alerts.outbox import hold_for_budget

    delivery = AlertDelivery(
        delivery_id="D1", dedupe_key="k", mode="live", live_profile="default",
        planning_rules_sha256="r", delivery_kind="INITIAL", priority=1,
        transport_status="LEASED", planning_state="READY", created_at=NOW,
        updated_at=NOW, recipient_ref="default")
    with pytest.raises(ValueError, match="P1 is never held"):
        hold_for_budget(None, delivery, "cap_24h", now=NOW)


def test_a_p1_planning_state_that_is_held_cannot_be_persisted(isolated_db):
    """The database refuses it, not just the planner."""
    import sqlalchemy

    from app.alerts.models import AlertDelivery
    from app.db import session_scope

    rules_sha = _registered_rules_sha()          # a REAL parent, so only the
    with session_scope() as session:             # CHECK constraint can fail
        session.add(AlertDelivery(
            delivery_id="D-ok", dedupe_key="k-ok", mode="live", live_profile="default",
            planning_rules_sha256=rules_sha, delivery_kind="INITIAL", priority=1,
            transport_status="PENDING", planning_state="READY",
            created_at=NOW, updated_at=NOW, recipient_ref="default"))

    with pytest.raises(sqlalchemy.exc.IntegrityError,
                       match="ck_alert_delivery_p1_never_held"), session_scope() as session:
        session.add(AlertDelivery(
            delivery_id="D-held", dedupe_key="k-held", mode="live",
            live_profile="default", planning_rules_sha256=rules_sha,
            delivery_kind="INITIAL", priority=1, transport_status="PENDING",
            planning_state="HELD_QUIET", created_at=NOW, updated_at=NOW,
            recipient_ref="default"))


# ---------------------------------------------------------------------------
# LLM selection: codes only, never numbers, never on a P1
# ---------------------------------------------------------------------------


def _registered_rules_sha() -> str:
    """Register the shipped artifacts so foreign keys resolve."""
    from app.alerts.artifacts import register
    from app.db import session_scope
    from tests.test_alert_evaluation import _artifacts

    with session_scope() as session:
        return register(session, _artifacts(stage=3), now=NOW)


def _seed_delivery(delivery_id: str = "D1", *, priority: int = 2,
                   planning_state: str = "READY") -> str:
    """A minimal delivery row so a child row has a real parent to point at."""
    from app.alerts.models import AlertDelivery
    from app.db import session_scope

    rules_sha = _registered_rules_sha()
    with session_scope() as session:
        session.add(AlertDelivery(
            delivery_id=delivery_id, dedupe_key=f"k-{delivery_id}", mode="shadow",
            live_profile="default", planning_rules_sha256=rules_sha,
            delivery_kind="INITIAL", priority=priority, transport_status="PENDING",
            planning_state=planning_state, created_at=NOW, updated_at=NOW,
            recipient_ref="default"))
    return delivery_id


def test_p1_never_calls_the_llm(isolated_db, phrase_set):
    from app.alerts.llm_selector import select_codes
    from app.db import session_scope

    context = _context(phrase_set)
    with session_scope() as session:
        result = select_codes(session, delivery_id="D1", priority=1, context=context,
                              phrase_set=phrase_set, now=NOW)
    assert result.selection is None
    assert result.fallback_reason == "P1_DETERMINISTIC"

    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    from app.alerts.models import AlertLlmAttempt

    with session_scope() as session:
        attempts = session.execute(
            sa_select(func.count()).select_from(AlertLlmAttempt)).scalar_one()
    assert attempts == 0        # not even a recorded skip: it never got that far


def test_llm_budget_exhaustion_falls_back_without_delaying(isolated_db, phrase_set,
                                                           monkeypatch):
    from app.alerts.llm_selector import select_codes
    from app.db import session_scope

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")   # pragma: allowlist secret
    monkeypatch.setenv("ALERTS_LLM_RENDER_CAP_24H", "0")
    from app.config import get_settings

    get_settings.cache_clear()
    _seed_delivery()
    context = _context(phrase_set)
    with session_scope() as session:
        result = select_codes(session, delivery_id="D1", priority=2, context=context,
                              phrase_set=phrase_set, now=NOW)
    assert result.selection is None
    assert result.fallback_reason == "LLM_BUDGET_EXHAUSTED"
    assert result.render_source == "template_full"
    get_settings.cache_clear()


def test_every_llm_attempt_is_recorded_including_failures(isolated_db, phrase_set,
                                                          monkeypatch):
    from app.alerts.llm_selector import select_codes
    from app.db import session_scope

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")   # pragma: allowlist secret
    from app.config import get_settings

    get_settings.cache_clear()

    class BoomClient:
        def select(self, *, system, user):
            raise TimeoutError("model did not answer")

    _seed_delivery()
    context = _context(phrase_set)
    with session_scope() as session:
        result = select_codes(session, delivery_id="D1", priority=2, context=context,
                              phrase_set=phrase_set, now=NOW, client=BoomClient())
    assert result.status == "TIMEOUT"

    from sqlalchemy import select as sa_select

    from app.alerts.models import AlertLlmAttempt

    with session_scope() as session:
        row = session.execute(sa_select(AlertLlmAttempt)).scalars().one()
    assert row.status == "TIMEOUT"
    assert row.error_code == "TimeoutError"
    get_settings.cache_clear()


def test_an_unauthorized_code_from_the_model_is_rejected(phrase_set):
    from app.alerts.llm_selector import validate_selection

    context = _context(phrase_set, codes=("BAND_TO_DERISK",))
    with pytest.raises(ValueError, match="not authorized"):
        validate_selection({"headline_code": "OVERRIDE_FIRES", "phrase_codes": [],
                            "fact_ids": [], "caveat_codes": []}, context, phrase_set)


def test_a_foreign_fact_id_from_the_model_is_rejected(phrase_set):
    from app.alerts.llm_selector import validate_selection

    context = _context(phrase_set, codes=("BAND_TO_DERISK",))
    with pytest.raises(ValueError, match="not authorized for this member"):
        validate_selection({"headline_code": "BAND_TO_DERISK", "phrase_codes": [],
                            "fact_ids": ["F_TOP10"], "caveat_codes": []},
                           context, phrase_set)


def test_the_model_prompt_contains_only_codes_numbers_and_enums(phrase_set):
    """No scraped free text reaches the model — the A-10 containment."""
    import json

    from app.alerts.llm_selector import build_prompt

    payload = json.loads(build_prompt(_context(phrase_set), phrase_set))
    assert set(payload) == {
        "allowed_headline_codes", "allowed_phrase_codes", "allowed_next_check_codes",
        "allowed_caveat_codes", "required_caveat_codes", "available_fact_ids",
        "facts", "condition_status", "priority", "bundle_size",
    }
    for value in payload["facts"].values():
        assert isinstance(value, str) and len(value) <= 12


# ---------------------------------------------------------------------------
# named mandate properties (§27.8 / §21.3)
# ---------------------------------------------------------------------------


def test_stale_sending_becomes_unknown(isolated_db):
    """A crash mid-send may have reached the provider; only UNKNOWN is honest."""
    from datetime import UTC, datetime, timedelta

    from app.alerts.artifacts import load_active, register
    from app.alerts.canonical import new_ulid
    from app.alerts.enums import DeliveryKind, PlanningState, TransportStatus
    from app.alerts.models import AlertDelivery
    from app.alerts.outbox import recover_leases
    from app.alerts.repository import utc_ms
    from app.db import session_scope

    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts)
        delivery_id = new_ulid(utc_ms(now))
        session.add(AlertDelivery(
            delivery_id=delivery_id, dedupe_key=f"v1|X|{delivery_id}",
            dedupe_version=1, manual_retry_sequence=0, mode="shadow",
            live_profile="default",
            planning_rules_sha256=artifacts.ruleset.rules_sha256,
            delivery_kind=DeliveryKind.TEST, priority=2,
            transport_status=TransportStatus.SENDING,
            planning_state=PlanningState.NONE,
            not_before=now - timedelta(minutes=30),
            created_at=now - timedelta(minutes=30),
            updated_at=now - timedelta(minutes=30), attempts=1,
            lease_owner="dead-worker",
            lease_until=now - timedelta(minutes=10),
            request_started_at=now - timedelta(minutes=11),
            duplicate_risk_acknowledged=False, recipient_ref="default"))
        session.flush()
        recover_leases(session, now=now)
        assert session.get(AlertDelivery, delivery_id).transport_status \
            == TransportStatus.UNKNOWN


def test_a_test_delivery_dispatches_its_reviewed_fragment(isolated_db):
    """send-test proves the REAL pipeline: claim, render, classify.

    The body is the reviewed TEST_MESSAGE fragment — text typed into a request
    would bypass the one gate every phone-bound word goes through.
    """
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_active, register, validate_phrase_set
    from app.alerts.canonical import new_ulid
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.enums import DeliveryKind, PlanningState, TransportStatus
    from app.alerts.models import AlertDelivery
    from app.alerts.repository import utc_ms
    from app.alerts.sender import NullSender
    from app.db import session_scope

    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts)
        delivery_id = new_ulid(utc_ms(now))
        session.add(AlertDelivery(
            delivery_id=delivery_id, dedupe_key=f"v1|TEST|{delivery_id}",
            dedupe_version=1, manual_retry_sequence=0, mode="shadow",
            live_profile="default",
            planning_rules_sha256=artifacts.ruleset.rules_sha256,
            delivery_kind=DeliveryKind.TEST, priority=4,
            transport_status=TransportStatus.PENDING,
            planning_state=PlanningState.READY, not_before=now,
            created_at=now, updated_at=now, attempts=0,
            duplicate_risk_acknowledged=False, recipient_ref="default"))

    with open("config/alert_phrases.v3.3.json", encoding="utf-8") as fh:
        phrase_set = validate_phrase_set(fh.read())
    sender = NullSender()
    dispatch_once(session_scope, phrase_set=phrase_set, mode="shadow",
                  live_profile="default", sender=sender, now=now)

    assert sender.sent, "the TEST delivery was not dispatched"
    assert sender.sent[0][1] == phrase_set.headlines["TEST_MESSAGE"].text
    with session_scope() as session:
        assert session.get(AlertDelivery, delivery_id).transport_status \
            == TransportStatus.SENT
