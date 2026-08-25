"""The weekly digest (audit B-15).

The product objective is a fixed daily message replaced by event alerts PLUS a
weekly digest. The event half existed; without this half, Stage 4 removes the
daily digest and puts nothing in its place.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.alerts.artifacts import load_active, register
from app.alerts.digest import digest_dedupe_key, plan_digest
from app.alerts.enums import DeliveryKind, DigestItemStatus, Priority
from app.alerts.models import (
    AlertDelivery,
    AlertDeliveryMember,
    AlertDigestItem,
    AlertEpisode,
    AlertSilence,
)
from app.alerts.repository import new_ulid, utc_ms
from app.db import session_scope

pytestmark = pytest.mark.usefixtures("isolated_db")

NOW = datetime(2026, 8, 24, 6, 30, tzinfo=UTC)      # a Monday
WINDOW = "2026-W34"


def _registered(session) -> str:
    """The digest delivery references a REGISTERED ruleset, like any other."""
    artifacts = load_active(session)
    register(session, artifacts)
    session.flush()
    return artifacts.ruleset.rules_sha256


def _graph(session, rules_sha: str) -> tuple[str, str]:
    """The rows an episode legitimately points at: an input and an evaluation.

    Fabricating the identifiers instead would pass only because the foreign
    keys were off, which is exactly what this schema turns on.
    """
    from app.alerts.models import AlertEvaluation, AlertInputSnapshot

    identity = "t" * 64
    if session.get(AlertInputSnapshot, identity) is None:
        session.add(AlertInputSnapshot(
            input_identity=identity, snapshot_id=None, origin="MANUAL",
            built_at=NOW, computed_at=NOW, alert_input_schema_version=1,
            methodology_version="v", methodology_sha256="m" * 64,
            reconstructed=False, evaluation_eligibility="EVALUABLE",
            ineligibility_reasons=[], payload="{}", payload_sha256="p" * 64))
    evaluation_id = "01M0DIGESTEVAL0000000000000"[:26]
    if session.get(AlertEvaluation, evaluation_id) is None:
        session.add(AlertEvaluation(
            evaluation_id=evaluation_id, idempotency_key="idem-digest",
            input_identity=identity, mode="shadow", live_profile="default",
            current_rules_sha256=rules_sha, evaluation_set_sha256="s" * 64,
            evaluated_ruleset_hashes=[rules_sha], evaluator_version="v",
            status="COMMITTED", attempt_count=1, started_at=NOW))
    session.flush()
    return identity, evaluation_id



def _provenance() -> tuple[str, str]:
    """Real phrase-set provenance. `plan_digest` requires it, so tests supply
    it rather than leaning on a default that used to disable the check."""
    ps = _phrase_set()
    return ps.version, ps.sha256


def _pending_item(session, *, rules_sha: str, rule_id: str,
                  window: str = WINDOW) -> str:
    identity, evaluation_id = _graph(session, rules_sha)
    episode_id = new_ulid(utc_ms(NOW))
    session.add(AlertEpisode(
        episode_id=episode_id, mode="shadow", live_profile="default",
        # distinct per rule: the partial unique index allows ONE open episode
        # per instance, which is the constraint B-05 was about
        instance_fingerprint=hashlib.sha256(rule_id.encode()).hexdigest(),
        rule_id=rule_id,
        priority=Priority.P3, episode_status="FIRING",
        is_open=True, origin_rules_sha256=rules_sha,
        trigger_input_identity=identity, opened_at=NOW,
        created_evaluation_id=evaluation_id, last_evaluation_id=evaluation_id))
    session.flush()          # the digest item FKs to the episode
    item_id = new_ulid(utc_ms(NOW))
    session.add(AlertDigestItem(
        digest_item_id=item_id, episode_id=episode_id, digest_window_key=window,
        status=DigestItemStatus.PENDING, pending_at=NOW))
    return item_id


def _silence_rule(session, rule_id: str) -> None:
    """Create the same active silence an operator creates through the API."""
    session.add(AlertSilence(
        silence_id=new_ulid(utc_ms(NOW)), matcher_kind="RULE_ID",
        matcher_value=rule_id, starts_at=NOW - timedelta(minutes=1),
        ends_at=NOW + timedelta(hours=1), comment="test",
        created_by_redacted="operator", created_at=NOW,
    ))


def test_a_window_becomes_one_delivery_with_its_items_as_members():
    with session_scope() as session:
        sha = _registered(session)
        _pending_item(session, rules_sha=sha, rule_id="structure.cape_record_near")
        _pending_item(session, rules_sha=sha, rule_id="regime.derisk_edge_approach")
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                           window_key=WINDOW, now=NOW)

    assert plan.quiet is False
    assert len(plan.item_ids) == 2

    with session_scope() as session:
        deliveries = session.query(AlertDelivery).all()
        assert len(deliveries) == 1
        assert deliveries[0].delivery_kind == DeliveryKind.DIGEST
        assert deliveries[0].priority == Priority.P3, (
            "a digest is a scheduled summary, reported in load but outside the "
            "non-P1 caps (mandate 9.2)")
        assert len(session.query(AlertDeliveryMember).all()) == 2
        assert all(i.status == DigestItemStatus.PLANNED
                   for i in session.query(AlertDigestItem).all())


def test_a_quiet_week_does_not_fabricate_a_memberless_delivery():
    """TEST is the only delivery kind allowed to have zero members.

    Digest-job liveness has its own durable heartbeat.  Burning a provider
    intent with no episode member would contradict the transport invariant and
    could then be mistaken for one of the successful weekly digests required
    by cutover.
    """
    with session_scope() as session:
        sha = _registered(session)
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                           window_key=WINDOW, now=NOW)

    assert plan.quiet is True
    assert plan.delivery_id is None
    assert plan.skipped_reason == "no digest items for this window"

    with session_scope() as session:
        assert session.query(AlertDelivery).all() == []
        assert session.query(AlertDeliveryMember).all() == []


def test_replanning_the_same_window_is_a_no_op():
    """A retried job, a restarted scheduler and a manual run must converge.

    The window key IS the identity, so three attempts produce one digest rather
    than three messages about the same week.
    """
    with session_scope() as session:
        sha = _registered(session)
        _pending_item(session, rules_sha=sha, rule_id="structure.s2_saturation")
        first = plan_digest(session, mode="shadow", live_profile="default",
                            planning_rules_sha256=sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                            window_key=WINDOW, now=NOW)
    with session_scope() as session:
        second = plan_digest(session, mode="shadow", live_profile="default",
                             planning_rules_sha256=sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                             window_key=WINDOW, now=NOW)

    assert second.delivery_id == first.delivery_id
    assert second.skipped_reason
    with session_scope() as session:
        assert len(session.query(AlertDelivery).all()) == 1


def test_the_dedupe_key_separates_namespaces():
    """Shadow and live must not share a digest."""
    shadow = digest_dedupe_key(mode="shadow", live_profile="default", window_key=WINDOW)
    live = digest_dedupe_key(mode="live", live_profile="default", window_key=WINDOW)
    other_week = digest_dedupe_key(mode="shadow", live_profile="default",
                                   window_key="2026-W35")
    assert len({shadow, live, other_week}) == 3


def test_an_item_from_another_window_is_not_swept_in():
    with session_scope() as session:
        sha = _registered(session)
        _pending_item(session, rules_sha=sha, rule_id="structure.s2_saturation", window="2026-W33")
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                           window_key=WINDOW, now=NOW)
    assert plan.quiet is True, "last week's item belongs to last week's digest"


def test_the_job_digests_the_window_that_closed(monkeypatch):
    """Running Monday for the week that ended Sunday is the point.

    Digesting the CURRENT window would summarise a few hours and then never
    mention the rest of the week.
    """
    from app.alerts.calendars import digest_window_key
    from app.jobs import alert_digest

    monkeypatch.setenv("ALERTS_MODE", "shadow")
    from app.config import get_settings
    get_settings.cache_clear()

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(
            session,
            rules_sha=rules_sha,
            rule_id="regime.band_to_derisk",
            window=digest_window_key(NOW - timedelta(days=1)),
        )

    result = alert_digest.run_once(now=NOW)
    assert result["window_key"] == digest_window_key(NOW - timedelta(days=1))
    assert result["window_key"] != digest_window_key(NOW), (
        "the job digested the window it is standing in")

    with session_scope() as session:
        delivery = session.get(AlertDelivery, result["delivery_id"])
        assert delivery.delivery_kind == DeliveryKind.DIGEST


def test_a_missed_monday_does_not_lose_the_week(monkeypatch):
    """The scheduler's misfire grace is finite; a week is not recoverable.

    The windows that still OWE a digest are exactly those with items waiting in
    them, so the catch-up is derived from the items rather than from a
    lookback I would have had to guess.
    """
    from app.jobs import alert_digest

    monkeypatch.setenv("ALERTS_MODE", "shadow")
    from app.config import get_settings
    get_settings.cache_clear()

    # an item stranded in a window from long before any fixed lookback
    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="regime.band_to_derisk", window="2026-W02")

    result = alert_digest.run_once(now=NOW)
    assert "2026-W02" in result["recovered_windows"], (
        "a window older than any fixed lookback was stranded")
    assert result["windows_planned"] == 1

    # running again changes nothing: the window key is the identity
    again = alert_digest.run_once(now=NOW)
    assert again["windows_planned"] == 0


def test_a_late_run_never_digests_the_week_it_is_standing_in(monkeypatch):
    """`now - 1 day` is only correct on a Monday.

    Run on a Tuesday — a catch-up, or an operator by hand — and yesterday is
    still inside the current week, so the job would summarise a few days and
    then never mention the rest of them.
    """
    from datetime import timedelta

    from app.alerts.calendars import digest_window_key
    from app.jobs import alert_digest

    monkeypatch.setenv("ALERTS_MODE", "shadow")
    from app.config import get_settings
    get_settings.cache_clear()

    tuesday = NOW + timedelta(days=1)
    result = alert_digest.run_once(now=tuesday)
    assert result["window_key"] != digest_window_key(tuesday), (
        "the job digested the open week")
    assert result["window_key"] == digest_window_key(tuesday - timedelta(days=2))


# --- the message itself ----------------------------------------------------

def _phrase_set():
    from app.alerts.artifacts import validate_phrase_set
    with open("config/alert_phrases.v3.4.json", encoding="utf-8") as fh:
        return validate_phrase_set(fh.read())


def test_a_quiet_week_still_has_something_to_say():
    """The reviewed quiet template remains deterministic and transport-safe."""
    from app.alerts.digest import render_digest_body

    result = render_digest_body(_phrase_set(), item_count=0)
    assert "keine Ereignisse" in result.body
    assert result.septet_count <= 160
    # nothing was interpolated, so no fact was consulted
    assert result.selected_fact_ids == []


def test_a_busy_week_reports_its_count_and_not_a_sample():
    """The count is honest; naming the first three of twelve would not be."""
    from app.alerts.digest import render_digest_body

    result = render_digest_body(_phrase_set(), item_count=12)
    assert "12 Ereignisse" in result.body
    assert result.selected_fact_ids == ["F_DIGEST_COUNT"]
    assert result.septet_count <= 160


def test_the_digest_is_never_assembled_from_invented_text():
    """A phrase set without the digest fragments fails; it does not improvise."""
    from dataclasses import replace

    from app.alerts.digest import render_digest_body
    from app.alerts.errors import RenderRejected

    stripped = replace(_phrase_set(), headlines={})
    with pytest.raises(RenderRejected):
        render_digest_body(stripped, item_count=0)


def test_a_legacy_memberless_digest_is_cancelled_before_render_or_send():
    """The dispatcher independently enforces TEST as the sole exemption.

    Migration 0016 prevents new non-TEST rows from reaching SENDING without a
    member, but a queued row may predate that migration.  Runtime must fail
    closed too, before either a final render or provider call exists.
    """
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.enums import PlanningState, TransportStatus
    from app.alerts.models import AlertRender
    from app.alerts.sender import NullSender

    sender = NullSender()

    with session_scope() as session:
        rules_sha = _registered(session)
        delivery_id = new_ulid(utc_ms(NOW))
        session.add(AlertDelivery(
            delivery_id=delivery_id,
            dedupe_key=f"v1|DIGEST|shadow|default|{WINDOW}",
            dedupe_version=1,
            manual_retry_sequence=0,
            mode="shadow",
            live_profile="default",
            planning_rules_sha256=rules_sha,
            delivery_kind=DeliveryKind.DIGEST,
            priority=Priority.P3,
            transport_status=TransportStatus.PENDING,
            planning_state=PlanningState.READY,
            scheduled_window_key=WINDOW,
            not_before=NOW,
            created_at=NOW,
            updated_at=NOW,
            attempts=0,
            duplicate_risk_acknowledged=False,
            recipient_ref="default",
        ))

    report = dispatch_once(session_scope, phrase_set=_phrase_set(), mode="shadow",
                           live_profile="default", sender=sender, now=NOW)

    assert report.cancelled == 1
    assert sender.sent == []
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery.transport_status == TransportStatus.CANCELLED
        assert delivery.cancel_reason == "ALL_MEMBERS_RESOLVED"
        assert session.execute(select(AlertRender)).scalars().all() == []


# --- what the panel caught -------------------------------------------------

def test_a_digest_never_consumes_another_namespace_s_items():
    """`AlertDigestItem` has no mode or profile of its own.

    Those live on the episode, so a query keyed only on the window would let a
    shadow digest swallow live items, mark them PLANNED — removing them from
    the live digest that should have carried them — and report a count drawn
    from someone else's week.
    """
    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha, rule_id="regime.band_to_derisk")

        # the live namespace has nothing of its own this week
        plan = plan_digest(session, mode="live", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1], window_key=WINDOW,
                           now=NOW)
        assert plan.quiet is True, "a live digest consumed a shadow item"
        assert plan.item_ids == []

        # and the shadow item is still there for the shadow digest to take
        shadow = plan_digest(session, mode="shadow", live_profile="default",
                             planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1], window_key=WINDOW,
                             now=NOW)
        assert len(shadow.item_ids) == 1
        assert shadow.quiet is False


def test_the_count_is_what_happened_not_what_is_still_open():
    """A retrospective counts the week, not Monday morning.

    Revalidation drops members whose episodes resolved or were silenced since
    planning. Counting the survivors would under-report exactly the weeks with
    the most movement — a week where everything fired and then resolved would
    read as quiet.
    """
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.enums import TransportStatus
    from app.alerts.sender import NullSender

    with session_scope() as session:
        rules_sha = _registered(session)
        for rule in ("regime.band_to_derisk", "regime.band_hold_to_trim",
                     "tripwire.rf4_first"):
            _pending_item(session, rules_sha=rules_sha, rule_id=rule)
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1], window_key=WINDOW,
                           now=NOW)
        delivery_id = plan.delivery_id
        assert len(plan.item_ids) == 3

        # every episode resolves before the digest is dispatched
        from app.alerts.enums import EpisodeStatus
        for episode in session.execute(select(AlertEpisode)).scalars().all():
            episode.is_open = False
            episode.episode_status = EpisodeStatus.RESOLVED
            episode.resolved_at = NOW

    sender = NullSender()
    dispatch_once(session_scope, phrase_set=_phrase_set(), mode="shadow",
                  live_profile="default", sender=sender, now=NOW)

    assert sender.sent, "the digest was not dispatched"
    body = sender.sent[0][1]
    assert "3 Ereignisse" in body, f"the week under-reported itself: {body!r}"
    assert "keine Ereignisse" not in body
    with session_scope() as session:
        assert session.get(AlertDelivery, delivery_id).transport_status \
            == TransportStatus.SENT


def test_a_silenced_episode_is_not_disclosed_by_the_count():
    """A count is still telling.

    The two drop reasons mean opposite things. RESOLVED_BEFORE_SEND is "it
    happened and then cleared" — a retrospective counts that. SILENCED is the
    operator asking not to be told, and reporting "3 Ereignisse" when two were
    silenced discloses precisely what the silence was for.
    """
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.enums import EpisodeStatus
    from app.alerts.sender import NullSender

    with session_scope() as session:
        rules_sha = _registered(session)
        for rule in ("regime.band_to_derisk", "regime.band_hold_to_trim",
                     "tripwire.rf4_first"):
            _pending_item(session, rules_sha=rules_sha, rule_id=rule)
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1], window_key=WINDOW,
                           now=NOW)
        assert len(plan.item_ids) == 3

        episodes = session.execute(select(AlertEpisode)).scalars().all()
        # one silenced, one resolved, one still open
        silenced_rule = episodes[0].rule_id
        resolved_rule = episodes[1].rule_id
        _silence_rule(session, silenced_rule)
        episodes[1].is_open = False
        episodes[1].episode_status = EpisodeStatus.RESOLVED
        episodes[1].resolved_at = NOW

    sender = NullSender()
    dispatch_once(session_scope, phrase_set=_phrase_set(), mode="shadow",
                  live_profile="default", sender=sender, now=NOW)

    body = sender.sent[0][1]
    assert "2 Ereignisse" in body, (
        f"the silenced episode was disclosed, or the resolved one dropped: {body!r}")
    with session_scope() as session:
        items = session.execute(
            select(AlertDigestItem).order_by(AlertDigestItem.digest_item_id)
        ).scalars().all()
        by_rule = {
            session.get(AlertEpisode, item.episode_id).rule_id: item
            for item in items
        }
        assert by_rule[silenced_rule].status == DigestItemStatus.CANCELLED
        assert by_rule[resolved_rule].status == DigestItemStatus.DELIVERED


def test_a_frozen_digest_is_cancelled_when_membership_changes():
    """A final render is immutable even when the provider was not attempted.

    Replacing it would create two competing final renders and make timestamp
    ordering decide which text is authoritative.  Sending it after a silence
    would disclose stale membership.  The only safe outcome is cancellation,
    with the unaffected item moved into the documented later-window retry
    lifecycle.
    """
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.enums import TransportStatus
    from app.alerts.models import AlertRender
    from app.alerts.sender import NullSender

    with session_scope() as session:
        rules_sha = _registered(session)
        for rule in ("regime.band_to_derisk", "regime.band_hold_to_trim"):
            _pending_item(session, rules_sha=rules_sha, rule_id=rule)
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1], window_key=WINDOW,
                           now=NOW)
        delivery_id = plan.delivery_id
        episodes = session.execute(
            select(AlertEpisode).order_by(AlertEpisode.rule_id)
        ).scalars().all()
        represented = [episode.episode_id for episode in episodes]

        # a render exists from an earlier pass that never reached the wire
        session.add(AlertRender(
            render_id=new_ulid(utc_ms(NOW)), delivery_id=delivery_id,
            render_source="TEMPLATE_FULL", fallback_reason=None,
            planning_phrase_set_version="v3.2", planning_phrase_set_sha256="p" * 64,
            render_context_hash="c" * 64, fact_catalog_hash="f" * 64,
            selected_fact_ids=[], selected_phrase_codes=[],
            validation_results={"represented_member_ids": represented},
            final_message="Wochenrueckblick: 2 Ereignisse. Naechster Rueckblick Montag.",
            gsm7_septets=60, created_at=NOW))
        session.flush()
        assert session.get(AlertDelivery, delivery_id).attempts == 0

        # then one of them is silenced
        silenced = episodes[0]
        survivor = episodes[1]
        _silence_rule(session, silenced.rule_id)

    sender = NullSender()
    report = dispatch_once(
        session_scope,
        phrase_set=_phrase_set(),
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=NOW,
    )

    assert sender.sent == []
    assert report.cancelled == 1
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery.transport_status == TransportStatus.CANCELLED
        assert delivery.cancel_reason == "RENDERED_MEMBER_WITHDRAWN"
        rows = session.execute(
            select(AlertDigestItem).where(
                AlertDigestItem.delivery_id == delivery_id
            )
        ).scalars().all()
        by_episode = {item.episode_id: item for item in rows}
        assert by_episode[silenced.episode_id].status == DigestItemStatus.CANCELLED
        assert by_episode[silenced.episode_id].last_error_code == "SILENCED"
        assert by_episode[survivor.episode_id].status == DigestItemStatus.FAILED
        assert by_episode[survivor.episode_id].last_error_code \
            == "RENDERED_MEMBER_WITHDRAWN"


def test_the_catch_up_does_not_look_into_another_namespace(monkeypatch):
    """`AlertDigestItem` carries no mode or profile; the episode does.

    An unqualified DISTINCT lets a shadow job discover windows that only ever
    had live activity, learn something happened in them, and plan empty digests
    that consume the window keys the live job needs.
    """
    from app.jobs import alert_digest

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="regime.band_to_derisk", window="2026-W02")

    # the item above belongs to a shadow episode; run the job as LIVE
    monkeypatch.setenv("ALERTS_MODE", "live")
    from app.config import get_settings
    get_settings.cache_clear()

    result = alert_digest.run_once(now=NOW)
    assert "2026-W02" not in result["recovered_windows"], (
        "a live job discovered a shadow namespace's window")


def test_the_recovered_list_survives_an_already_planned_current_window(monkeypatch):
    """`planned[1:]` assumed the current window is always planned and first.

    When it already has a delivery it is not in the list at all, so the
    genuinely recovered window sitting at index zero was reported as the
    routine one and dropped from the log.
    """
    from app.alerts.calendars import last_closed_digest_window
    from app.jobs import alert_digest

    monkeypatch.setenv("ALERTS_MODE", "shadow")
    from app.config import get_settings
    get_settings.cache_clear()

    current = last_closed_digest_window(NOW)
    with session_scope() as session:
        rules_sha = _registered(session)
        # the current window is already done...
        _pending_item(
            session,
            rules_sha=rules_sha,
            rule_id="structure.s2_saturation",
            window=current,
        )
        plan_digest(session, mode="shadow", live_profile="default",
                    planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1], window_key=current, now=NOW)
        # ...and an older one is still owed
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="regime.band_to_derisk", window="2026-W02")

    result = alert_digest.run_once(now=NOW)
    assert result["recovered_windows"] == ["2026-W02"], result["recovered_windows"]


def test_quiet_windows_spanned_by_an_outage_are_not_replayed(monkeypatch):
    """A fortnight of downtime must not fabricate historical empty intents.

    The current heartbeat proves the job resumed; history that held no member
    evidence carries no additional information.
    """
    from app.jobs import alert_digest

    monkeypatch.setenv("ALERTS_MODE", "shadow")
    from app.config import get_settings
    get_settings.cache_clear()

    with session_scope() as session:
        _registered(session)

    result = alert_digest.run_once(now=NOW)
    assert result["windows_planned"] == 0, (
        f"an outage replayed empty weeks: {result['recovered_windows']}")


def test_an_item_arriving_late_joins_a_digest_that_has_not_been_sent():
    """The window key IS the digest's identity, so no second delivery can
    ever carry a late item. Returning early left it PENDING forever.
    """
    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha, rule_id="regime.band_to_derisk")
        first = plan_digest(session, mode="shadow", live_profile="default",
                            planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1], window_key=WINDOW,
                            now=NOW)
        assert len(first.item_ids) == 1

        # an episode opens late for the same, still-unsent window
        _pending_item(session, rules_sha=rules_sha, rule_id="tripwire.rf4_first")
        second = plan_digest(session, mode="shadow", live_profile="default",
                             planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1], window_key=WINDOW,
                             now=NOW)

        assert second.delivery_id == first.delivery_id, "a second digest appeared"
        assert len(second.item_ids) == 1, "the late item was stranded"
        assert "absorbed" in (second.skipped_reason or "")

        remaining = session.execute(
            select(AlertDigestItem).where(
                AlertDigestItem.status == DigestItemStatus.PENDING)
        ).scalars().all()
        assert remaining == []


def test_a_late_item_cannot_mutate_a_digest_after_its_render_is_final():
    """Late evidence waits for carry-forward once provider intent is frozen."""
    from app.alerts.enums import TransportStatus
    from app.alerts.models import AlertRender

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(
            session,
            rules_sha=rules_sha,
            rule_id="regime.band_to_derisk",
        )
        first = plan_digest(
            session,
            mode="shadow",
            live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key=WINDOW,
            now=NOW,
        )
        original_member = session.execute(
            select(AlertDeliveryMember).where(
                AlertDeliveryMember.delivery_id == first.delivery_id
            )
        ).scalars().one()
        delivery = session.get(AlertDelivery, first.delivery_id)
        delivery.transport_status = TransportStatus.RETRY_DUE
        delivery.attempts = 1
        delivery.not_before = NOW
        session.add(AlertRender(
            render_id=new_ulid(utc_ms(NOW)),
            delivery_id=first.delivery_id,
            render_source="template_full",
            planning_phrase_set_version=_provenance()[0],
            planning_phrase_set_sha256=_provenance()[1],
            render_context_hash="c" * 64,
            fact_catalog_hash="f" * 64,
            selected_fact_ids=["F_DIGEST_COUNT"],
            selected_phrase_codes=["DIGEST_SUMMARY"],
            validation_results={
                "represented_member_ids": [original_member.episode_id],
            },
            final_message=(
                "Wochenrueckblick: 1 Ereignisse. Naechster Rueckblick Montag."
            ),
            gsm7_septets=60,
            created_at=NOW,
        ))
        session.flush()

        late_item_id = _pending_item(
            session,
            rules_sha=rules_sha,
            rule_id="tripwire.rf4_first",
        )
        after = plan_digest(
            session,
            mode="shadow",
            live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key=WINDOW,
            now=NOW,
        )

        assert after.delivery_id == first.delivery_id
        assert after.item_ids == []
        assert after.stranded == 1
        assert "frozen" in (after.skipped_reason or "")
        late_item = session.get(AlertDigestItem, late_item_id)
        assert late_item.status == DigestItemStatus.PENDING
        assert late_item.delivery_id is None
        members = session.execute(
            select(AlertDeliveryMember).where(
                AlertDeliveryMember.delivery_id == first.delivery_id
            )
        ).scalars().all()
        assert [member.episode_id for member in members] \
            == [original_member.episode_id]


def test_an_item_arriving_after_the_send_is_counted_not_folded_in():
    """Absorbing then would claim the message said something it did not."""
    from app.alerts.enums import TransportStatus

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha, rule_id="regime.band_to_derisk")
        first = plan_digest(session, mode="shadow", live_profile="default",
                            planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1], window_key=WINDOW,
                            now=NOW)
        session.get(AlertDelivery, first.delivery_id).transport_status = \
            TransportStatus.SENT
        session.flush()

        _pending_item(session, rules_sha=rules_sha, rule_id="tripwire.rf4_first")
        after = plan_digest(session, mode="shadow", live_profile="default",
                            planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1], window_key=WINDOW,
                            now=NOW)

    assert after.item_ids == []
    assert after.stranded == 1, "a late item vanished without being counted"
    assert "already sent" in (after.skipped_reason or "")


def test_a_hand_run_cannot_burn_the_open_week(monkeypatch):
    """The window key is the identity, so consuming it is permanent.

    One hand-run naming the current week would leave that week unable to
    produce a digest at all, silently.
    """
    from app.alerts.calendars import digest_window_key
    from app.jobs import alert_digest

    monkeypatch.setenv("ALERTS_MODE", "shadow")
    from app.config import get_settings
    get_settings.cache_clear()

    result = alert_digest.run_once(now=NOW, window_key=digest_window_key(NOW))
    assert result["status"] == "refused"
    assert "has not closed" in result["reason"]

    with session_scope() as session:
        assert session.execute(select(AlertDelivery)).scalars().all() == []


def test_the_library_default_is_the_closed_window_not_the_open_one():
    """The job passed an explicit window, so the default was the unguarded path.

    Any caller omitting the argument consumed a partial week — and the window
    key IS the digest's identity, so that week could never produce a real
    digest afterwards.
    """
    from app.alerts.calendars import digest_window_key, last_closed_digest_window

    with session_scope() as session:
        rules_sha = _registered(session)
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1], now=NOW)

    assert plan.window_key == last_closed_digest_window(NOW)
    assert plan.window_key != digest_window_key(NOW)


def test_plan_digest_refuses_an_open_or_future_window():
    """Guarding only the job left every other caller able to burn a window."""
    from app.alerts.calendars import digest_window_key

    with session_scope() as session:
        rules_sha = _registered(session)
        for window in (digest_window_key(NOW), "2099-W40"):
            plan = plan_digest(session, mode="shadow", live_profile="default",
                               planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                               window_key=window, now=NOW)
            assert plan.delivery_id is None, window
            assert "has not closed" in (plan.skipped_reason or ""), window

        assert session.execute(select(AlertDelivery)).scalars().all() == []


def test_a_late_item_keeps_its_episode_origin_not_the_callers_current_pair():
    """Digest membership provenance comes from the episode registry binding."""
    from app.alerts.models import AlertDeliveryMember

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha, rule_id="regime.band_to_derisk")
        first = plan_digest(session, mode="shadow", live_profile="default",
                            planning_rules_sha256=rules_sha,
                            phrase_set_version=_provenance()[0],
                            phrase_set_sha256=_provenance()[1],
                            window_key=WINDOW, now=NOW)

        # A late planning call cannot stamp arbitrary current bytes on it.
        _pending_item(session, rules_sha=rules_sha, rule_id="tripwire.rf4_first")
        plan_digest(session, mode="shadow", live_profile="default",
                    planning_rules_sha256=rules_sha,
                    phrase_set_version=_provenance()[0],
                    phrase_set_sha256=_provenance()[1],
                    window_key=WINDOW, now=NOW)

        members = session.execute(
            select(AlertDeliveryMember).where(
                AlertDeliveryMember.delivery_id == first.delivery_id)
        ).scalars().all()

    assert len(members) == 2
    assert {m.origin_phrase_set_version for m in members} == {_provenance()[0]}
    assert {m.origin_phrase_set_sha256 for m in members} == {_provenance()[1]}


def test_an_item_orphaned_by_a_sent_digest_is_carried_into_the_next_one():
    """It had nowhere left to go.

    The window key is its digest's identity, so no second delivery can carry
    it, and its own window's query is finished. Late but true beats an event
    the operator hears about in no message at all.
    """
    from app.alerts.enums import TransportStatus

    with session_scope() as session:
        rules_sha = _registered(session)
        # last week reported and sent
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="regime.band_to_derisk", window="2026-W33")
        last = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                           window_key="2026-W33", now=NOW)
        session.get(AlertDelivery, last.delivery_id).transport_status = \
            TransportStatus.SENT
        session.flush()

        # an event surfaces for that week AFTER it was reported
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="tripwire.rf4_first", window="2026-W33")
        stranded = plan_digest(session, mode="shadow", live_profile="default",
                               planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                               window_key="2026-W33", now=NOW)
        assert stranded.stranded == 1

        # this week's digest picks it up rather than leaving it forever
        this_week = plan_digest(session, mode="shadow", live_profile="default",
                                planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                                window_key=WINDOW, now=NOW)

    assert this_week.carried_forward == 1
    assert len(this_week.item_ids) == 1
    assert this_week.quiet is False


def test_an_unreported_earlier_window_keeps_its_own_items():
    """Carrying these forward would rob that digest of its content.

    The distinction is whether the earlier window can still carry the item
    itself: if its digest does not exist yet, it can.
    """
    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="regime.band_to_derisk", window="2026-W33")

        # W33 has no digest yet, so its item is not swept into W34
        this_week = plan_digest(session, mode="shadow", live_profile="default",
                                planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                                window_key=WINDOW, now=NOW)
        assert this_week.quiet is True
        assert this_week.carried_forward == 0

        # and W33's own digest still finds it
        last = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                           window_key="2026-W33", now=NOW)
        assert len(last.item_ids) == 1


def test_member_phrase_pair_must_match_its_exact_origin_ruleset():
    """A member cannot self-authorize unrelated, otherwise valid phrase bytes."""
    from app.alerts.artifacts import register
    from app.alerts.dispatcher import planning_phrase_set
    from app.alerts.models import AlertPhraseSetRegistry
    from app.alerts.phrase_registry import validate_phrase_set

    v32 = validate_phrase_set(
        open("config/alert_phrases.v3.2.json", encoding="utf-8").read())

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha, rule_id="regime.band_to_derisk")
        # Plan truthfully, then tamper the persisted member to an unrelated but
        # otherwise valid artifact. The member cannot self-authorise it.
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                           window_key=WINDOW, now=NOW)

        # The registry holds v3.2's real bytes; mere registry presence does not
        # let the member override what its origin ruleset authorized.
        register(session, load_active(session))
        session.add(AlertPhraseSetRegistry(
            phrase_set_version=v32.version, phrase_set_sha256=v32.sha256,
            canonical_json=v32.canonical_json,
            validator_version="1", validated_at=NOW,
            worst_case_test_sha256=v32.worst_case_test_sha256))
        session.flush()

        member = session.execute(
            select(AlertDeliveryMember).where(
                AlertDeliveryMember.delivery_id == plan.delivery_id)
        ).scalars().one()
        member.origin_phrase_set_version = v32.version
        member.origin_phrase_set_sha256 = v32.sha256
        session.flush()

        delivery = session.get(AlertDelivery, plan.delivery_id)
        resolved = planning_phrase_set(session, delivery, _phrase_set())

    assert resolved is None


def test_an_unregistered_planning_phrase_set_is_refused_before_queueing(monkeypatch):
    """Fail-closed. A quietly re-worded alert is worse than a visible failure.

    Falling back to whatever this process holds meant the message could go out
    worded differently from the one that was planned and reviewed. A render
    failure is visible and recoverable; that is not.
    """
    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha, rule_id="regime.band_to_derisk")
        with pytest.raises(ValueError, match="registry binding"):
            plan_digest(session, mode="shadow", live_profile="default",
                        planning_rules_sha256=rules_sha,
                        phrase_set_version="v0.0-never-registered",
                        phrase_set_sha256="z" * 64,
                        window_key=WINDOW, now=NOW)
        assert session.execute(select(AlertDelivery)).scalars().all() == []


def test_a_phrase_set_whose_bytes_moved_is_refused_before_queueing():
    """Resolving by VERSION alone trusts that a version still means what it did.

    The member recorded the digest precisely so that could be verified rather
    than assumed.
    """
    from app.alerts.artifacts import register
    from app.alerts.models import AlertPhraseSetRegistry
    from app.alerts.phrase_registry import validate_phrase_set

    with session_scope() as session:
        rules_sha = _registered(session)
        v32 = validate_phrase_set(
            open("config/alert_phrases.v3.2.json", encoding="utf-8").read())
        session.add(AlertPhraseSetRegistry(
            phrase_set_version=v32.version, phrase_set_sha256=v32.sha256,
            canonical_json=v32.canonical_json, validator_version="1",
            validated_at=NOW, worst_case_test_sha256=v32.worst_case_test_sha256))
        register(session, load_active(session))
        session.flush()

        _pending_item(session, rules_sha=rules_sha, rule_id="regime.band_to_derisk")
        # planned against v3.2's VERSION but a digest that is not v3.2's
        with pytest.raises(ValueError, match="registry binding"):
            plan_digest(session, mode="shadow", live_profile="default",
                        planning_rules_sha256=rules_sha,
                        phrase_set_version=v32.version,
                        phrase_set_sha256="9" * 64,
                        window_key=WINDOW, now=NOW)
        assert session.execute(select(AlertDelivery)).scalars().all() == []


def test_a_digest_takes_its_text_from_the_ruleset_that_planned_it():
    """A queued digest uses the reviewed phrase artifact bound at planning."""
    from app.alerts.dispatcher import planning_phrase_set

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(
            session,
            rules_sha=rules_sha,
            rule_id="regime.band_to_derisk",
        )
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                           window_key=WINDOW, now=NOW)
        assert plan.quiet is False
        delivery = session.get(AlertDelivery, plan.delivery_id)
        current = _phrase_set()
        assert planning_phrase_set(session, delivery, current) is current


def test_a_member_with_no_recorded_provenance_fails_the_render():
    """A default that quietly disables an integrity control is worse than none.

    `plan_digest` used to let the phrase-set version and digest default to
    empty strings, and a member with no provenance was then rendered from
    whatever the process held — so a queued digest's wording could change
    across a deploy with nothing recording that it had.
    """
    from app.alerts.dispatcher import planning_phrase_set
    from app.alerts.models import AlertDeliveryMember

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha, rule_id="regime.band_to_derisk")
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                           window_key=WINDOW, now=NOW)

        # a member that predates the requirement, carrying nothing
        member = session.execute(
            select(AlertDeliveryMember).where(
                AlertDeliveryMember.delivery_id == plan.delivery_id)
        ).scalars().first()
        member.origin_phrase_set_version = ""
        member.origin_phrase_set_sha256 = ""
        session.flush()

        delivery = session.get(AlertDelivery, plan.delivery_id)
        assert planning_phrase_set(session, delivery, _phrase_set()) is None


def test_plan_digest_will_not_record_a_member_without_provenance():
    """The parameters are required, so the empty case cannot be created."""
    import inspect

    signature = inspect.signature(plan_digest)
    for name in ("phrase_set_version", "phrase_set_sha256"):
        assert signature.parameters[name].default is inspect.Parameter.empty, (
            f"{name} has a default again, which re-enables the hole")


def test_a_digest_whose_planning_ruleset_text_is_gone_fails_the_render():
    """Same rule as the member path: unreproducible planned text fails."""
    from app.alerts.dispatcher import planning_phrase_set
    from app.alerts.models import AlertRulesetRegistry

    class _Different:
        """A running set that is NOT the planned one, forcing the registry path."""
        version = "v0.0-running"
        sha256 = "0" * 64

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(
            session,
            rules_sha=rules_sha,
            rule_id="regime.band_to_derisk",
        )
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                           window_key=WINDOW, now=NOW)
        assert plan.quiet is False
        delivery = session.get(AlertDelivery, plan.delivery_id)

        # the ruleset's phrase set IS registered, so it resolves
        resolved = planning_phrase_set(session, delivery, _Different())
        assert resolved is not None
        assert resolved.version != _Different.version

        # and when the ruleset names a digest the registry does not hold,
        # the planned text cannot be reproduced
        session.get(AlertRulesetRegistry, rules_sha).phrase_set_sha256 = "9" * 64
        session.flush()
        assert planning_phrase_set(session, delivery, _Different()) is None


def test_automatic_digest_retry_reuses_one_byte_identical_final_render(monkeypatch):
    """Definite provider rejection permits a retry, not a different message."""
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.enums import SenderOutcome, TransportStatus
    from app.alerts.models import AlertRender
    from app.alerts.sender import SendResult

    class SequenceSender:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def send(self, message, *, recipient_ref, idempotency_key=None):
            self.calls.append((message, idempotency_key))
            if len(self.calls) == 1:
                return SendResult(
                    outcome=SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED,
                    http_status=503,
                    error_code="TEMPORARY",
                    request_started=False,
                )
            return SendResult(
                outcome=SenderOutcome.CONFIRMED_SUCCESS,
                http_status=204,
                request_started=True,
            )

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(
            session,
            rules_sha=rules_sha,
            rule_id="regime.band_to_derisk",
        )
        plan = plan_digest(
            session,
            mode="shadow",
            live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key=WINDOW,
            now=NOW,
        )
        delivery_id = plan.delivery_id

    sender = SequenceSender()
    first = dispatcher_module.dispatch_once(
        session_scope,
        phrase_set=_phrase_set(),
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=NOW,
    )
    assert first.failed == 1

    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery.transport_status == TransportStatus.RETRY_DUE
        original = session.execute(
            select(AlertRender).where(AlertRender.delivery_id == delivery_id)
        ).scalars().one()
        original_id = original.render_id
        original_body = original.final_message

    def forbidden_rerender(*_args, **_kwargs):
        raise AssertionError("automatic retry attempted to replace its final render")

    monkeypatch.setattr(
        dispatcher_module,
        "render_digest_body",
        forbidden_rerender,
    )
    second = dispatcher_module.dispatch_once(
        session_scope,
        phrase_set=_phrase_set(),
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=NOW + timedelta(minutes=1),
    )

    assert second.sent == 1
    assert sender.calls == [
        (original_body, delivery_id),
        (original_body, delivery_id),
    ]
    with session_scope() as session:
        renders = session.execute(
            select(AlertRender).where(AlertRender.delivery_id == delivery_id)
        ).scalars().all()
        assert [render.render_id for render in renders] == [original_id]


def test_frozen_digest_cancellation_replans_only_the_unsilenced_survivor():
    """Invalidating frozen prose cannot strand unaffected digest evidence."""
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.enums import SenderOutcome, TransportStatus
    from app.alerts.sender import SendResult

    class TransientSender:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send(self, message, *, recipient_ref, idempotency_key=None):
            self.messages.append(message)
            return SendResult(
                outcome=SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED,
                http_status=503,
                error_code="TEMPORARY",
                request_started=False,
            )

    with session_scope() as session:
        rules_sha = _registered(session)
        for rule_id in (
            "regime.band_to_derisk",
            "structure.s2_saturation",
        ):
            _pending_item(session, rules_sha=rules_sha, rule_id=rule_id)
        plan = plan_digest(
            session,
            mode="shadow",
            live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key=WINDOW,
            now=NOW,
        )
        delivery_id = plan.delivery_id

    sender = TransientSender()
    first = dispatch_once(
        session_scope,
        phrase_set=_phrase_set(),
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=NOW,
    )
    assert first.failed == 1

    with session_scope() as session:
        episodes = session.execute(
            select(AlertEpisode).order_by(AlertEpisode.rule_id)
        ).scalars().all()
        silenced_id = episodes[0].episode_id
        survivor_id = episodes[1].episode_id
        _silence_rule(session, episodes[0].rule_id)

    second = dispatch_once(
        session_scope,
        phrase_set=_phrase_set(),
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=NOW + timedelta(minutes=1),
    )
    assert second.cancelled == 1
    assert len(sender.messages) == 1

    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery.transport_status == TransportStatus.CANCELLED
        assert delivery.cancel_reason == "RENDERED_MEMBER_WITHDRAWN"
        items = session.execute(
            select(AlertDigestItem).where(
                AlertDigestItem.delivery_id == delivery_id
            )
        ).scalars().all()
        by_episode = {item.episode_id: item for item in items}
        assert by_episode[silenced_id].status == DigestItemStatus.CANCELLED
        assert by_episode[silenced_id].last_error_code == "SILENCED"
        assert by_episode[survivor_id].status == DigestItemStatus.FAILED
        assert by_episode[survivor_id].last_error_code \
            == "RENDERED_MEMBER_WITHDRAWN"

        later = plan_digest(
            session,
            mode="shadow",
            live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key="2026-W35",
            now=NOW + timedelta(days=7),
        )
        assert later.carried_forward == 1
        assert len(later.item_ids) == 1
        survivor = session.get(AlertDigestItem, by_episode[survivor_id].digest_item_id)
        assert survivor.delivery_id == later.delivery_id
        assert survivor.status == DigestItemStatus.PLANNED
        assert by_episode[silenced_id].delivery_id == delivery_id


def test_digest_item_becomes_delivered_only_after_confirmed_send():
    from app.alerts.outbox import mark_sent

    with session_scope() as session:
        rules_sha = _registered(session)
        item_id = _pending_item(
            session, rules_sha=rules_sha, rule_id="regime.band_to_derisk")
        plan = plan_digest(
            session, mode="shadow", live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key=WINDOW, now=NOW)
        delivery = session.get(AlertDelivery, plan.delivery_id)
        mark_sent(session, delivery, now=NOW, http_status=200)
        item = session.get(AlertDigestItem, item_id)
        assert item.status == DigestItemStatus.DELIVERED
        assert item.delivered_at == NOW
        assert item.last_error_code is None


@pytest.mark.parametrize("outcome", ["permanent", "render"])
def test_definite_digest_failure_marks_its_items_failed(outcome):
    from app.alerts.outbox import mark_permanent, mark_render_failed

    with session_scope() as session:
        rules_sha = _registered(session)
        item_id = _pending_item(
            session, rules_sha=rules_sha, rule_id="regime.band_to_derisk")
        plan = plan_digest(
            session, mode="shadow", live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key=WINDOW, now=NOW)
        delivery = session.get(AlertDelivery, plan.delivery_id)
        if outcome == "permanent":
            mark_permanent(
                session, delivery, now=NOW, error_code="HTTP_400",
                message="definite rejection", http_status=400)
            expected_error = "HTTP_400"
        else:
            mark_render_failed(
                session, delivery, now=NOW, reason="reviewed text unavailable")
            expected_error = "RENDER_REJECTED"
        item = session.get(AlertDigestItem, item_id)
        assert item.status == DigestItemStatus.FAILED
        assert item.delivered_at is None
        assert item.last_error_code == expected_error


def test_ambiguous_digest_marks_items_unknown_and_blocks_carry_forward():
    from app.alerts.outbox import mark_unknown

    with session_scope() as session:
        rules_sha = _registered(session)
        item_id = _pending_item(
            session, rules_sha=rules_sha, rule_id="regime.band_to_derisk",
            window="2026-W33")
        first = plan_digest(
            session, mode="shadow", live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key="2026-W33", now=NOW)
        delivery = session.get(AlertDelivery, first.delivery_id)
        mark_unknown(session, delivery, now=NOW, reason="socket closed")

        later = plan_digest(
            session, mode="shadow", live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key=WINDOW, now=NOW)
        item = session.get(AlertDigestItem, item_id)
        assert item.status == DigestItemStatus.UNKNOWN
        assert item.last_error_code == "AMBIGUOUS"
        assert later.quiet is True
        assert later.item_ids == []


def test_definitely_failed_digest_item_is_replanned_in_the_next_window():
    from app.alerts.outbox import mark_permanent

    with session_scope() as session:
        rules_sha = _registered(session)
        item_id = _pending_item(
            session, rules_sha=rules_sha, rule_id="regime.band_to_derisk",
            window="2026-W33")
        first = plan_digest(
            session, mode="shadow", live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key="2026-W33", now=NOW)
        mark_permanent(
            session, session.get(AlertDelivery, first.delivery_id), now=NOW,
            error_code="HTTP_400", message="rejected", http_status=400)

        later = plan_digest(
            session, mode="shadow", live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key=WINDOW, now=NOW)
        item = session.get(AlertDigestItem, item_id)
        assert later.carried_forward == 1
        assert later.item_ids == [item_id]
        assert item.status == DigestItemStatus.PLANNED
        assert item.delivery_id == later.delivery_id
        assert item.last_error_code is None
