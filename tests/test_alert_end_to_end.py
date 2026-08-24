"""The path the audit says does not exist: snapshot -> ... -> SENT.

Audit finding B-01: `plan()` and `persist_plan()` were never called from any
production path, so a rule could reach FIRING, an episode could open, and no
delivery was ever created. The dispatcher had nothing to claim, which means
setting ALERTS_MODE=live would have sent nothing at all.

Every existing alert test builds an idealised `AlertInput` by hand and stops at
the state decision. This one starts from a committed Snapshot and ends at a
delivery marked SENT by a NullSender, because that is the only assertion that
would have failed before the planner was wired in.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from app.alerts.enums import TransportStatus
from app.alerts.models import (
    AlertDelivery,
    AlertDeliveryMember,
    AlertEpisode,
    AlertRender,
)
from app.db import session_scope
from app.models import Snapshot

pytestmark = pytest.mark.usefixtures("isolated_db")


def _red_flag_meta(observed_at: datetime) -> dict:
    """A complete typed red-flag contract.

    Without one the sidecar is PARTIAL with `no_typed_red_flag_contract`, and a
    partial input cannot exercise the band rule — which would make this test
    pass for the wrong reason.
    """
    stamp = observed_at.isoformat()
    flags = {}
    for fid, unit in (("rf1", "stat"), ("rf2", "pp"), ("rf3", "bps"), ("rf4", "pct")):
        flags[fid] = {
            "flag_id": fid, "source_key": fid, "active": False, "fireable": True,
            "state": "INACTIVE", "distance_to_threshold": -1.0, "unit": unit,
            "period_start": "2026-08-19", "period_end": "2026-08-19",
            "published_at": None, "observed_at": stamp, "data_state": "FRESH",
        }
    return {"contract_version": 1, "flags": flags,
            "override_required_count": 2,
            "override_fireable_universe_count": 4,
            "override_fired": False}


def _snapshot(session, *, computed_at: datetime, effective: str, prev_id: int | None):
    """A snapshot carrying the typed contract the sidecar needs."""
    snap = Snapshot(
        computed_at=computed_at, service_version="test",
        median=61.0 if effective == "de-risk" else 52.0,
        iqr_lo=58.0, iqr_hi=64.0, band5=55.0, band95=67.0,
        point_score=61.0 if effective == "de-risk" else 52.0,
        action_band=effective, override_fired=False,
        red_flag_count=0, red_flag_detail={},
        alert_contract_version=1,
        score_action_band=effective, base_action_band=effective,
        effective_action_state=effective,
        band_suppressed_by_coverage=False, data_degraded=False,
        red_flag_meta=_red_flag_meta(computed_at),
        prev_snapshot_id=prev_id,
        block_s={"indicators": {}}, block_d={"indicators": {}},
        trend_states={}, fast_alarm={}, data_freshness={})
    session.add(snap)
    session.flush()
    return snap


def test_a_band_transition_reaches_a_sent_delivery(tmp_path, monkeypatch):
    """The whole chain, end to end.

    trim -> de-risk is `regime.band_to_derisk`, the P1 the mandate cares most
    about. Before the planner was wired this produced a FIRING episode and
    nothing else; the assertion that matters is the delivery.
    """
    # A stage-3 ruleset on disk, which is exactly what Stage 3 will be. The
    # delivery rules are gated `enabled_in_stages: [3, ...]`, so at the
    # committed stage 1 nothing plans anything and this test would prove
    # nothing. Re-staging is done by writing the FILE, never by passing an
    # argument: `enabled_in_stages` is the rollout gate and the production path
    # may not choose its own stage.
    import yaml

    source = yaml.safe_load(
        pathlib.Path("config/alert_rules.v3.2.yaml").read_text(encoding="utf-8"))
    source["meta"]["active_stage"] = 3
    staged = tmp_path / "alert_rules.stage3.yaml"
    staged.write_text(yaml.safe_dump(source, sort_keys=False, allow_unicode=True),
                      encoding="utf-8")

    monkeypatch.setenv("ALERTS_RULES_PATH", str(staged))
    monkeypatch.setenv("ALERTS_MODE", "shadow")
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    from app.alerts.sender import NullSender
    from app.services.alert_integration import capture_alert_input, evaluate_input

    base = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
    identities = []
    with session_scope() as session:
        first = _snapshot(session, computed_at=base, effective="trim", prev_id=None)
        first_id = first.id
    identities.append(capture_alert_input(first_id))

    with session_scope() as session:
        second = _snapshot(session, computed_at=base + timedelta(hours=4),
                           effective="de-risk", prev_id=first_id)
        second_id = second.id
    identities.append(capture_alert_input(second_id))

    for identity in identities:
        evaluate_input(identity)

    # 1. the condition fired and an episode exists
    with session_scope() as session:
        episodes = session.query(AlertEpisode).all()
        assert episodes, "the band transition must open an episode"
        firing = [e for e in episodes if e.rule_id == "regime.band_to_derisk"]
        assert firing, f"expected band_to_derisk, saw {[e.rule_id for e in episodes]}"

    # 2. THE ASSERTION THAT WOULD HAVE FAILED: a delivery intent exists
    with session_scope() as session:
        deliveries = session.query(AlertDelivery).all()
        assert deliveries, (
            "no delivery was created — the planner is not wired into the "
            "atomic apply (audit B-01)")
        members = session.query(AlertDeliveryMember).all()
        assert members, "a market delivery must carry at least one member"
        assert any(m.rule_id == "regime.band_to_derisk" for m in members)

    # 3. the dispatcher can CLAIM it — the outbox is reachable
    from app.alerts.artifacts import load_active
    from app.alerts.dispatcher import dispatch_once

    with session_scope() as session:
        phrase_set = load_active(session).phrase_set

    # `not_before` is stamped from the evaluation's real clock, not from the
    # snapshot's timestamp, so the dispatcher must run at a moment after it.
    settings = get_settings()
    report = dispatch_once(
        session_scope, phrase_set=phrase_set, mode=settings.alerts_mode,
        live_profile=settings.alerts_live_profile, sender=NullSender(),
        now=datetime.now(UTC) + timedelta(hours=1))

    assert report.claimed, (
        f"the dispatcher found nothing to claim: {report.as_dict()}")

    # 4. and it reaches SENT through the NullSender
    #
    # This is the assertion the whole chain exists for. It required the render
    # context to carry the PREDECESSOR input: `BAND_TO_DERISK` reads
    # "Stufe {F_BAND_EFFECTIVE} erreicht (vorher {F_BAND_PREVIOUS})", and no
    # single input can say what a state moved FROM. Without it the fact is
    # unauthorized, the render is rejected, and every band P1 died in
    # RENDER_FAILED (audit B-14).
    assert report.sent == report.claimed, (
        f"the delivery did not reach SENT: {report.as_dict()}")

    with session_scope() as session:
        deliveries = session.query(AlertDelivery).all()
        assert {d.transport_status for d in deliveries} == {TransportStatus.SENT}
        render = session.query(AlertRender).first()
        assert render is not None, "a sent delivery must persist its render"
        assert render.final_message, "the body must be persisted with the render"
        assert render.gsm7_septets and render.gsm7_septets <= 160, (
            f"body must fit one GSM-7 message, got {render.gsm7_septets} septets")
        assert "vorher" in render.final_message, (
            f"the transition phrase needs the predecessor: {render.final_message!r}")


def test_the_predecessor_comes_from_lineage_not_from_the_clock(isolated_db):
    """`prev_snapshot_id` is what scoring recorded as the predecessor.

    Picking "the most recent sidecar before this timestamp" instead looks
    identical in steady state and diverges exactly when it matters: a skipped
    recompute, a retry, or an out-of-order arrival puts a DIFFERENT snapshot
    immediately before the trigger, and the message then says the state moved
    from a band it never moved from.
    """
    from app.alerts.repository import load_input_for_snapshot
    from app.services.alert_integration import capture_alert_input

    base = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
    with session_scope() as session:
        first = _snapshot(session, computed_at=base, effective="hold", prev_id=None).id
    capture_alert_input(first)

    # an unrelated snapshot lands BETWEEN them in time but is not the lineage
    with session_scope() as session:
        interloper = _snapshot(session, computed_at=base + timedelta(hours=1),
                               effective="trim", prev_id=None).id
    capture_alert_input(interloper)

    with session_scope() as session:
        third = _snapshot(session, computed_at=base + timedelta(hours=2),
                          effective="de-risk", prev_id=first).id
    capture_alert_input(third)

    with session_scope() as session:
        trigger = load_input_for_snapshot(session, third)
        previous = load_input_for_snapshot(session, trigger.prev_snapshot_id)

    assert previous is not None
    assert previous.snapshot_id == first, "lineage, not the nearest timestamp"
    assert previous.effective_action_state == "hold", (
        "the interloper would have claimed the move came from 'trim'")


def test_the_renderer_reads_the_predecessor_rather_than_resolving_it(
        isolated_db, tmp_path, monkeypatch):
    """Resolved ONCE, at plan time, and recorded on the episode.

    Lineage is immutable, but the fallback is a query over "what exists before
    this timestamp" — and a reconstruction or backfill can insert a sidecar
    between the trigger and its original predecessor at any time. Evaluation
    happens once; dispatch happens later, sometimes much later behind quiet
    hours. Two independent lookups can therefore disagree, and the message
    would name a band the decision never saw with nothing in the record to
    show it.
    """
    import inspect

    import yaml

    from app.alerts import dispatcher
    from app.alerts.models import AlertEpisode
    from app.services.alert_integration import capture_alert_input, evaluate_input

    source_yaml = yaml.safe_load(
        pathlib.Path("config/alert_rules.v3.2.yaml").read_text(encoding="utf-8"))
    source_yaml["meta"]["active_stage"] = 3
    staged = tmp_path / "alert_rules.stage3.yaml"
    staged.write_text(yaml.safe_dump(source_yaml, sort_keys=False, allow_unicode=True),
                      encoding="utf-8")
    monkeypatch.setenv("ALERTS_RULES_PATH", str(staged))
    monkeypatch.setenv("ALERTS_MODE", "shadow")
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    source = inspect.getsource(dispatcher._build_context)
    assert "resolve_predecessor" not in source, (
        "the dispatcher must not re-resolve the predecessor")
    assert "predecessor_input_identity" in source

    base = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
    with session_scope() as session:
        first = _snapshot(session, computed_at=base, effective="trim", prev_id=None).id
    i1 = capture_alert_input(first)
    with session_scope() as session:
        second = _snapshot(session, computed_at=base + timedelta(hours=4),
                           effective="de-risk", prev_id=first).id
    i2 = capture_alert_input(second)
    evaluate_input(i1)
    evaluate_input(i2)

    with session_scope() as session:
        episodes = [e for e in session.query(AlertEpisode).all()
                    if e.rule_id == "regime.band_to_derisk"]
        assert episodes, "the transition must have opened an episode"
        assert episodes[0].predecessor_input_identity == i1, (
            "the episode must record the input the decision was made against")


def test_an_episode_cannot_name_a_predecessor_that_does_not_exist(isolated_db):
    """`trigger_input_identity` has a foreign key; the predecessor needs one too.

    Without it an episode can outlive the sidecar it names, and the render then
    loses `F_BAND_PREVIOUS` and dies in RENDER_FAILED — the exact failure this
    column was added to prevent. Nothing prunes sidecars today, but "nothing
    does this yet" is not "nothing can".
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.exc import IntegrityError

    from app.alerts.models import (
        AlertEpisode,
        AlertEvaluation,
        AlertInputSnapshot,
    )
    from app.db import get_engine

    keys = sa_inspect(get_engine()).get_foreign_keys("alert_episode")
    assert any(k["constrained_columns"] == ["predecessor_input_identity"]
               and k["referred_table"] == "alert_input_snapshot"
               for k in keys), f"no foreign key on the predecessor: {keys}"

    moment = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
    identity, evaluation_id = "t" * 64, "01M0PREDFKEVAL00000000000A"

    with session_scope() as session:
        from app.alerts.artifacts import load_active, register
        artifacts = load_active(session)
        register(session, artifacts)
        rules_sha = artifacts.ruleset.rules_sha256
        session.add(AlertInputSnapshot(
            input_identity=identity, snapshot_id=None, origin="MANUAL",
            built_at=moment, computed_at=moment, alert_input_schema_version=1,
            methodology_version="v", methodology_sha256="m" * 64,
            reconstructed=False, evaluation_eligibility="EVALUABLE",
            ineligibility_reasons=[], payload="{}", payload_sha256="p" * 64))
        session.flush()
        session.add(AlertEvaluation(
            evaluation_id=evaluation_id, idempotency_key="idem-predfk",
            input_identity=identity, mode="shadow", live_profile="default",
            current_rules_sha256=rules_sha, evaluation_set_sha256="s" * 64,
            evaluated_ruleset_hashes=[rules_sha], evaluator_version="v",
            status="COMMITTED", attempt_count=1, started_at=moment))
        session.flush()

        # a predecessor that was never captured is refused
        with pytest.raises(IntegrityError):
            session.add(AlertEpisode(
                episode_id="01M0PREDFKEPISODE0000000A",
                mode="shadow", live_profile="default",
                origin_rules_sha256=rules_sha, instance_fingerprint="f" * 64,
                rule_id="regime.band_to_derisk", labels={}, priority=1,
                episode_status="FIRING", is_open=True, suppression_reasons=[],
                opened_at=moment, trigger_input_identity=identity,
                predecessor_input_identity="n" * 64,
                created_evaluation_id=evaluation_id,
                last_evaluation_id=evaluation_id))
            session.flush()
        session.rollback()


def test_the_delivery_carries_the_runs_profile_not_the_ambient_one(monkeypatch):
    """They agree in a single-profile deployment and diverge where it matters.

    An evaluation running for one profile would otherwise stamp its deliveries
    with whichever profile the process was configured for — and the sender
    resolves the recipient from that ref.
    """
    from app.alerts.engine import _recipient_ref

    monkeypatch.setenv("ALERTS_LIVE_PROFILE", "ambient")
    from app.config import get_settings
    get_settings.cache_clear()

    assert _recipient_ref("house") == "house"
    assert _recipient_ref("") == "default"


def test_render_time_status_covers_all_four_outcomes(isolated_db):
    """Mandate 17.5 — the two hard outcomes, not only the two easy ones.

    UNKNOWN at render must carry its data-quality caveat and claim no
    resolution; a resolved member is dropped; a materially changed member
    renders trigger AND current rather than presenting stale numbers as now.
    """
    from app.alerts.render_context import render_time_status

    assert render_time_status(condition_state="FIRING", resolved=True,
                              materially_changed=False) == "RESOLVED_BEFORE_SEND"
    assert render_time_status(condition_state="UNKNOWN", resolved=False,
                              materially_changed=True) == "UNKNOWN_AT_RENDER"
    assert render_time_status(condition_state="FIRING", resolved=False,
                              materially_changed=True) \
        == "MATERIALLY_CHANGED_BUT_ACTIVE"
    assert render_time_status(condition_state="FIRING", resolved=False,
                              materially_changed=False) == "STILL_FIRING"

    # and the dispatcher actually consults it now — U-02 recorded this helper
    # as having no callers, which meant 17.5 was implemented and unenforced
    import inspect

    from app.alerts import dispatcher

    assert "render_time_status(" in inspect.getsource(dispatcher._build_context)


def test_an_unknown_condition_renders_with_its_caveat(isolated_db):
    """The message may go out; it may not pretend the condition is readable."""
    from app.alerts.phrase_registry import validate_phrase_set
    from app.alerts.render_context import build_member_context
    from app.alerts.renderer import render_with_cascade

    with open("config/alert_phrases.v3.3.json", encoding="utf-8") as fh:
        phrase_set = validate_phrase_set(fh.read())

    from tests.test_alert_evaluation import make_input

    trigger = make_input(identity="u1", computed_at="2026-08-24T06:00:00+00:00",
                         effective="de-risk", base="de-risk")
    previous = make_input(identity="u0", computed_at="2026-08-24T02:00:00+00:00",
                          effective="hold", base="hold")
    context_member = build_member_context(
        episode_id="01M0UNKNOWNRENDER00000000A", rule_id="regime.band_to_derisk",
        priority=1, trigger=trigger, current=trigger, previous=previous,
        authorized_phrase_codes=frozenset(phrase_set.all_codes()),
        required_caveat_codes=(), condition_status="UNKNOWN_AT_RENDER",
        origin_phrase_set_version=phrase_set.version,
        origin_phrase_set_sha256=phrase_set.sha256,
        origin_rules_sha256="r" * 64)

    from app.alerts.render_context import RenderContext

    result = render_with_cascade(
        context=RenderContext(members=[context_member]), phrase_set=phrase_set,
        headline_code="BAND_TO_DERISK", phrase_codes=[],
        next_check_code="NEXT_RECOMPUTE", caveat_codes=[])

    caveat = phrase_set.caveats["UNKNOWN_AT_RENDER"].text
    assert caveat in result.body, (
        f"an UNKNOWN condition rendered without its caveat: {result.body!r}")
