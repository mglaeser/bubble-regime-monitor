"""The weekly digest (audit B-15).

The product objective is a fixed daily message replaced by event alerts PLUS a
weekly digest. The event half existed; without this half, Stage 4 removes the
daily digest and puts nothing in its place.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

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


def test_the_job_digests_the_window_that_closed():
    """Running Monday for the week that ended Sunday is the point.

    Digesting the CURRENT window would summarise a few hours and then never
    mention the rest of the week.
    """
    import inspect

    from app.jobs import alert_digest

    source = inspect.getsource(alert_digest.run_once)
    assert "timedelta(days=1)" in source
    assert "window that just CLOSED" in source or "just CLOSED" in source


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
