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
from app.alerts.models import AlertDelivery, AlertDeliveryMember, AlertDigestItem, AlertEpisode
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


def test_a_window_becomes_one_delivery_with_its_items_as_members():
    with session_scope() as session:
        sha = _registered(session)
        _pending_item(session, rules_sha=sha, rule_id="structure.cape_record_near")
        _pending_item(session, rules_sha=sha, rule_id="regime.derisk_edge_approach")
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=sha,
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


def test_a_quiet_week_still_sends():
    """Silence is what a broken system produces too.

    After Stage 4 this is the only scheduled message the operator receives, so
    "nothing fired this week" is the proof-of-life the daily digest used to
    provide by accident. A digest that skips quiet weeks is indistinguishable
    from a digest job that died.
    """
    with session_scope() as session:
        sha = _registered(session)
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=sha,
                           window_key=WINDOW, now=NOW)

    assert plan.quiet is True
    assert plan.delivery_id is not None, "a quiet week must still produce a delivery"

    with session_scope() as session:
        assert len(session.query(AlertDelivery).all()) == 1
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
                            window_key=WINDOW, now=NOW)
    with session_scope() as session:
        second = plan_digest(session, mode="shadow", live_profile="default",
                             planning_rules_sha256=sha,
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
    assert result["windows_planned"] >= 2

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
    with open("config/alert_phrases.v3.2.json", encoding="utf-8") as fh:
        return validate_phrase_set(fh.read())


def test_a_quiet_week_still_has_something_to_say():
    """Proof-of-life. Silence is also what a dead scheduler produces."""
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


def test_a_memberless_digest_is_not_cancelled_as_all_resolved():
    """The one legitimate memberless market delivery.

    Every other kind with no members is a delivery whose reason to exist went
    away. A quiet digest's reason to exist IS that nothing happened, so the
    generic cancel would delete exactly the message Stage 4 depends on.
    """
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.enums import TransportStatus
    from app.alerts.sender import NullSender

    sender = NullSender()

    with session_scope() as session:
        rules_sha = _registered(session)
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha, window_key=WINDOW,
                           now=NOW)
        assert plan.quiet is True
        delivery_id = plan.delivery_id

    report = dispatch_once(session_scope, phrase_set=_phrase_set(), mode="shadow",
                           live_profile="default", sender=sender, now=NOW)

    assert report.cancelled == 0, "the quiet digest was cancelled"
    assert sender.sent and "keine Ereignisse" in sender.sent[0][1]
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery.transport_status == TransportStatus.SENT


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
                           planning_rules_sha256=rules_sha, window_key=WINDOW,
                           now=NOW)
        assert plan.quiet is True, "a live digest consumed a shadow item"
        assert plan.item_ids == []

        # and the shadow item is still there for the shadow digest to take
        shadow = plan_digest(session, mode="shadow", live_profile="default",
                             planning_rules_sha256=rules_sha, window_key=WINDOW,
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
                           planning_rules_sha256=rules_sha, window_key=WINDOW,
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
                           planning_rules_sha256=rules_sha, window_key=WINDOW,
                           now=NOW)
        assert len(plan.item_ids) == 3

        episodes = session.execute(select(AlertEpisode)).scalars().all()
        # one silenced, one resolved, one still open
        episodes[0].suppression_reasons = ["SILENCED"]
        episodes[1].is_open = False
        episodes[1].episode_status = EpisodeStatus.RESOLVED
        episodes[1].resolved_at = NOW

    sender = NullSender()
    dispatch_once(session_scope, phrase_set=_phrase_set(), mode="shadow",
                  live_profile="default", sender=sender, now=NOW)

    body = sender.sent[0][1]
    assert "2 Ereignisse" in body, (
        f"the silenced episode was disclosed, or the resolved one dropped: {body!r}")


def test_a_silence_after_the_first_render_is_not_disclosed_by_a_stale_body():
    """Render reuse exists so a retry does not change a message that may have
    arrived. That reasoning only holds once something has been transmitted.

    A digest that was rendered and then held — budget, quiet hours, a crash —
    has sent nothing, so its cached count is just a stale number. Reusing it
    after an episode is silenced discloses exactly what the silence was for.
    """
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.models import AlertRender
    from app.alerts.sender import NullSender

    with session_scope() as session:
        rules_sha = _registered(session)
        for rule in ("regime.band_to_derisk", "regime.band_hold_to_trim"):
            _pending_item(session, rules_sha=rules_sha, rule_id=rule)
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha, window_key=WINDOW,
                           now=NOW)
        delivery_id = plan.delivery_id

        # a render exists from an earlier pass that never reached the wire
        session.add(AlertRender(
            render_id=new_ulid(utc_ms(NOW)), delivery_id=delivery_id,
            render_source="TEMPLATE_FULL", fallback_reason=None,
            planning_phrase_set_version="v3.2", planning_phrase_set_sha256="p" * 64,
            render_context_hash="c" * 64, fact_catalog_hash="f" * 64,
            selected_fact_ids=[], selected_phrase_codes=[], validation_results={},
            final_message="Wochenrueckblick: 2 Ereignisse. Naechster Rueckblick Montag.",
            gsm7_septets=60, created_at=NOW))
        session.flush()
        assert session.get(AlertDelivery, delivery_id).attempts == 0

        # then one of them is silenced
        episode = session.execute(select(AlertEpisode)).scalars().first()
        episode.suppression_reasons = ["SILENCED"]

    sender = NullSender()
    dispatch_once(session_scope, phrase_set=_phrase_set(), mode="shadow",
                  live_profile="default", sender=sender, now=NOW)

    body = sender.sent[0][1]
    assert "1 Ereignisse" in body, (
        f"the stale render disclosed the silenced episode: {body!r}")


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
        plan_digest(session, mode="shadow", live_profile="default",
                    planning_rules_sha256=rules_sha, window_key=current, now=NOW)
        # ...and an older one is still owed
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="regime.band_to_derisk", window="2026-W02")

    result = alert_digest.run_once(now=NOW)
    assert result["recovered_windows"] == ["2026-W02"], result["recovered_windows"]


def test_quiet_windows_spanned_by_an_outage_are_not_replayed(monkeypatch):
    """A fortnight of downtime must not deliver a dozen empty messages.

    That is worse than the gap it fills: it trains the operator to ignore the
    one channel Stage 4 leaves them. The resumed cadence is the proof-of-life;
    history that held nothing carries no information.
    """
    from app.jobs import alert_digest

    monkeypatch.setenv("ALERTS_MODE", "shadow")
    from app.config import get_settings
    get_settings.cache_clear()

    with session_scope() as session:
        _registered(session)

    result = alert_digest.run_once(now=NOW)
    assert result["windows_planned"] == 1, (
        f"an outage replayed empty weeks: {result['recovered_windows']}")
