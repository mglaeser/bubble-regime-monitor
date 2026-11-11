"""A fact the newest sidecar lacks is the trigger's, and the message says so.

docs/ALERT_SYSTEM.md 11e: `UNKNOWN_AT_RENDER` renders WITH the data-quality
caveat. AGENTS.md invariant 4: a failed indicator is dropped, never replaced.
So a held alert whose reading the next recompute dropped must still go out.

The trigger's facts used to stand in only when the current sidecar gave none
at all. 17 of the 26 shipped rules also render the next run, which is always
there, so one dropped reading left the headline unfillable and the delivery
ended RENDER_FAILED, which is final. Production, 2026-08-20 .. 2026-11-10
(read-only copy): no fact an enabled rule renders was missing in 490 recompute
sidecars; CAPE, which no enabled rule renders, was dropped for the three runs
of one night (2026-10-21 18:00 .. 2026-10-22 02:00 UTC).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.alerts.models import AlertDelivery, AlertEpisode, AlertRender, AlertRuleState
from app.db import session_scope
from tests.test_alert_end_to_end import _dispatch, _red_flag_meta, _snapshot, _stage3_shadow

pytestmark = pytest.mark.usefixtures("isolated_db")


def _run(at, prev_id, *, rf4, breadth):
    """One recompute. rf4: 'off', 'on' or 'unreadable' (breadth was dropped)."""
    from app.services.alert_integration import capture_alert_input, evaluate_input

    with session_scope() as session:
        snap = _snapshot(session, computed_at=at, effective="trim", prev_id=prev_id)
        meta = _red_flag_meta(at)
        flag = meta["flags"]["rf4"]
        if rf4 == "on":
            flag.update(active=True, state="ACTIVE", distance_to_threshold=0.1)
        elif rf4 == "unreadable":
            flag.update(active=False, fireable=False, state="UNKNOWN",
                        data_state="MISSING", distance_to_threshold=None)
        snap.red_flag_meta = meta
        snap.block_d = {"indicators": {"d1": (
            {"dropped": True, "value": None, "as_of": None, "stale": None}
            if breadth is None else
            {"value": breadth, "as_of": at.date().isoformat(), "stale": False})}}
        snap.expected_recompute_slot = at + timedelta(hours=4)
        snapshot_id = snap.id
    evaluate_input(capture_alert_input(snapshot_id, now=at), now=at)
    return snapshot_id


def test_a_held_alert_whose_reading_the_next_run_dropped_is_still_sent(
        tmp_path, monkeypatch):
    from app.alerts.sender import NullSender

    _stage3_shadow(tmp_path, monkeypatch, "fact-fallback")
    evening = datetime(2026, 8, 20, 18, 0, tzinfo=UTC)
    first = _run(evening, None, rf4="off", breadth=60.0)
    night = evening + timedelta(hours=4)            # 22:00 UTC, 00:00 Berlin: quiet hours
    second = _run(night, first, rf4="on", breadth=49.9)
    sender = NullSender()
    _dispatch(sender, night + timedelta(minutes=2))
    with session_scope() as session:
        held = session.query(AlertDelivery).one()
        assert (held.transport_status, held.planning_state) == ("PENDING", "HELD_QUIET")
    _run(night + timedelta(hours=4), second, rf4="unreadable", breadth=None)   # 02:00 UTC
    with session_scope() as session:
        episode = session.query(AlertEpisode).filter_by(rule_id="tripwire.rf4_first").one()
        state = session.query(AlertRuleState).filter_by(rule_id="tripwire.rf4_first").one()
        assert episode.is_open and state.condition_state == "UNKNOWN"

    _dispatch(sender, datetime(2026, 8, 21, 5, 0, 5, tzinfo=UTC))             # 07:00 Berlin

    with session_scope() as session:
        delivery = session.query(AlertDelivery).one()
        assert delivery.transport_status == "SENT", (
            delivery.transport_status, delivery.last_error_message_redacted)
        body = session.query(AlertRender).one().final_message
    assert body == ("Breitenflagge rf4 erstmals aktiv, Breite 49.9%. "
                    "Naechster Lauf 06:00 UTC. Kontext veraltet. Zustand unklar."), body


def test_a_missing_current_fact_is_the_triggers_and_marked_stale():
    from app.alerts.render_context import build_member_context
    from tests.test_alert_evaluation import make_input

    trigger = make_input(identity="t" * 64, computed_at="2026-08-24T06:00:00+00:00",
                         effective="de-risk", base="de-risk")
    current = make_input(identity="u" * 64, computed_at="2026-08-24T10:00:00+00:00",
                         effective="de-risk", base="de-risk").model_copy(
                             update={"base_action_band": None})
    common = dict(
        episode_id="01M0FACTFALLBACK000000000A", rule_id="ops.coverage_risk_masking",
        priority=2, trigger=trigger,
        authorized_fact_ids=frozenset({"F_BAND_BASE", "F_BAND_EFFECTIVE"}),
        authorized_phrase_codes=frozenset(), required_caveat_codes=(),
        condition_status="STILL_FIRING", origin_phrase_set_version="v3.5",
        origin_phrase_set_sha256="s" * 64, origin_rules_sha256="r" * 64)

    member = build_member_context(current=current, **common)
    assert member.facts == {"F_BAND_BASE": "de-risk", "F_BAND_EFFECTIVE": "de-risk"}
    assert "CONTEXT_STALE" in member.required_caveat_codes

    # no special case for a current view with no fact at all
    empty = build_member_context(current=current.model_copy(
        update={"effective_action_state": None}), **common)
    assert empty.facts == {"F_BAND_BASE": "de-risk", "F_BAND_EFFECTIVE": "de-risk"}
    assert "CONTEXT_STALE" in empty.required_caveat_codes

    complete = build_member_context(current=trigger, **common)
    assert "CONTEXT_STALE" not in complete.required_caveat_codes


def test_a_stale_member_carries_no_material_change_clause():
    """One fact missing beside another that moved: stale, and no "Ausloeser X,
    jetzt Y".

    The ruleset validator proves two caveat sets to fit one message: stale
    with unknown, and material change (registry._check_render_contracts). An
    incompatible or absent current input never made both, and a missing fact
    must not either: at the declared slot widths the two together would take
    13 of the 26 shipped rules past 160 septets (up to 174). The deltas stay
    in the context, and so in its hash.
    """
    from app.alerts.render_context import build_member_context
    from tests.test_alert_evaluation import make_input

    trigger = make_input(identity="v" * 64, computed_at="2026-08-24T06:00:00+00:00",
                         effective="de-risk", base="de-risk")
    moved = make_input(identity="w" * 64, computed_at="2026-08-24T10:00:00+00:00",
                       effective="trim", base="de-risk")
    common = dict(
        episode_id="01M0FACTFALLBACK000000000B", rule_id="ops.coverage_risk_masking",
        priority=2, trigger=trigger,
        authorized_fact_ids=frozenset({"F_BAND_BASE", "F_BAND_EFFECTIVE"}),
        authorized_phrase_codes=frozenset(), required_caveat_codes=(),
        condition_status="MATERIALLY_CHANGED_BUT_ACTIVE", origin_phrase_set_version="v3.5",
        origin_phrase_set_sha256="s" * 64, origin_rules_sha256="r" * 64)

    stale = build_member_context(current=moved.model_copy(
        update={"base_action_band": None}), **common)
    assert stale.facts == {"F_BAND_BASE": "de-risk", "F_BAND_EFFECTIVE": "trim"}
    assert "CONTEXT_STALE" in stale.required_caveat_codes
    assert "MATERIAL_CHANGE" not in stale.required_caveat_codes
    assert stale.material_change_fact_id is None
    assert stale.fact_deltas["F_BAND_EFFECTIVE"]["trigger"] == "de-risk"

    # with every fact there, the material change is shown as before
    complete = build_member_context(current=moved, **common)
    assert "MATERIAL_CHANGE" in complete.required_caveat_codes
    assert "CONTEXT_STALE" not in complete.required_caveat_codes
    assert complete.facts["F_TRIGGER_VALUE"] == "de-risk"
    assert complete.facts["F_CURRENT_VALUE"] == "trim"
