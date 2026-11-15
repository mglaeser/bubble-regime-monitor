"""What the retention job deletes, and what it keeps (docs/ALERT_SYSTEM.md 11a).

The job has two effects and no third: at the message horizon the TEXT of a
settled render is emptied, at the metadata horizon an `alert_event` row of
settled work is deleted. No other table has a horizon (docs/REPLAY_MILESTONE.md:
"Snapshots are never pruned"). These tests are that statement, row for row.

They replace a tuple of names in app/alerts/retention.py (`METADATA_PRESERVED`,
read by no code) and `test_retention_preserves_events_of_open_episodes`, which
asserted two strings of that tuple. Neither held anything: with the
open-episode guard deleted from `sweep_settled_events`, all 9 retention tests
of tests/test_alert_addendum.py passed (mutation run, 2026-11-15).
`test_an_event_of_an_open_episode_is_kept_at_any_age` fails under that
mutation; `test_retention_deletes_events_and_no_row_of_another_table` fails
when the short sweep deletes the render row instead of emptying its text, and
when the long sweep also deletes rows of another table.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.alerts.canonical import new_ulid
from app.alerts.enums import TransportStatus
from tests.test_alert_addendum_support import (
    NOW,
    persist_snapshot,
    seed_open_episode,
    seed_render,
)

OLD = NOW - timedelta(days=900)     # past both horizons (400 and 800 days)


def _old_event(*, episode_id: str | None = None, delivery_id: str | None = None) -> str:
    from app.alerts.models import AlertEvent
    from app.db import session_scope

    event_id = new_ulid(0)
    with session_scope() as session:
        session.add(AlertEvent(
            event_id=event_id, occurred_at=OLD, causation_type="EVALUATION",
            causation_id="evaluation-1", actor_type="SYSTEM", action="episode_opened",
            episode_id=episode_id, delivery_id=delivery_id, suppression_reasons=[]))
    return event_id


def _close_the_episode() -> str:
    from app.alerts.models import AlertEpisode
    from app.db import session_scope

    with session_scope() as session:
        episode = session.execute(select(AlertEpisode)).scalar_one()
        episode.episode_status, episode.is_open, episode.resolved_at = "RESOLVED", False, OLD
        return episode.episode_id


def _run():
    from app.alerts.retention import run_retention
    from app.config import get_settings
    from app.db import session_scope

    with session_scope() as session:
        return run_retention(session, settings=get_settings(), now=NOW)


def test_an_event_of_an_open_episode_is_kept_at_any_age(isolated_db):
    """The trail that explains a still-firing mechanism is the one most likely
    to be needed; the same event goes once its episode has closed."""
    from app.alerts.models import AlertEpisode, AlertEvent
    from app.db import session_scope

    seed_open_episode()
    with session_scope() as session:
        episode_id = session.execute(select(AlertEpisode.episode_id)).scalar_one()
    event_id = _old_event(episode_id=episode_id)

    assert _run().events_deleted == 0
    with session_scope() as session:
        assert session.get(AlertEvent, event_id) is not None

    _close_the_episode()
    assert _run().events_deleted == 1
    with session_scope() as session:
        assert session.get(AlertEvent, event_id) is None


def test_retention_deletes_events_and_no_row_of_another_table(isolated_db):
    """One 900-day-old row in every table that grows, all of it settled: the
    job takes the event and the text of the render, and the row count of every
    other table is unchanged. The tables are read from the schema, so a table
    added later is counted too."""
    from app.alerts.models import AlertConfirmationObservation, AlertDelivery, AlertRender
    from app.db import session_scope
    from app.models import Base, DailyClose, MessageEngineAttempt, Snapshot, SourceHealth

    render_id = seed_render(created_at=OLD, transport=TransportStatus.SENT)
    episode_id = _close_the_episode()
    snapshot_id = persist_snapshot()    # with its indicator readings
    with session_scope() as session:
        delivery_id = session.execute(select(AlertDelivery.delivery_id)).scalar_one()
        session.get(Snapshot, snapshot_id).computed_at = OLD
        session.add(AlertConfirmationObservation(
            mode="shadow", live_profile="default", rules_sha256="r" * 64,
            instance_fingerprint="fp-addendum", candidate_started_input="input-addendum",
            source_id="breadth_pct", economic_observation_key="k" * 64,
            source_revision_key="v" * 64, computation_fingerprint="c" * 64,
            observed_at=OLD, confirmation_role="CONFIRMATION"))
        session.add(DailyClose(symbol="SPY", date=OLD.date(), close=1.0, fetched_at=OLD))
        session.add(SourceHealth(source="breadth", ok=True, checked_at=OLD))
        session.add(MessageEngineAttempt(
            trigger="daily_digest", channel="imessage", priority=3, started_at=OLD,
            finished_at=OLD, outcome="ok", message="old", source="generated"))
    event_id = _old_event(episode_id=episode_id, delivery_id=delivery_id)

    def counts() -> dict[str, int]:
        with session_scope() as session:
            return {table.name: session.execute(
                select(func.count()).select_from(table)).scalar_one()
                for table in Base.metadata.sorted_tables}

    before = counts()
    for seeded in ("alert_input_snapshot", "alert_evaluation", "alert_episode",
                   "alert_delivery", "alert_delivery_member", "alert_render",
                   "alert_confirmation_observation", "alert_ruleset_registry",
                   "alert_phrase_set_registry", "alert_event", "snapshots",
                   "indicator_readings", "daily_close", "source_health",
                   "message_engine_attempts"):
        assert before[seeded] >= 1, seeded

    report = _run()

    assert (report.message_bodies_redacted, report.events_deleted) == (1, 1)
    assert counts() == {**before, "alert_event": before["alert_event"] - 1}
    with session_scope() as session:
        from app.alerts.models import AlertEvent

        assert session.get(AlertEvent, event_id) is None
        assert session.get(AlertRender, render_id).final_message == ""
