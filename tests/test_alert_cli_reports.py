"""Operator commands and read-only alert governance reports."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.alerts.cli import build_parser as build_alert_parser
from app.alerts.models import AlertInputSnapshot
from app.alerts.reports import (
    economic_observation_statistics,
    snapshot_export_rows,
    transition_statistics,
)
from app.cli import build_parser as build_root_parser
from tests.test_alert_evaluation import make_input


def test_exact_mandate_cli_surfaces_parse():
    alerts = build_alert_parser()
    assert alerts.parse_args(["recover-leases", "--once"]).command == \
        "recover-leases"

    root = build_root_parser()
    alerts_root = root.parse_args(["alerts", "recover-leases", "--once"])
    assert alerts_root.alert_args == ["recover-leases", "--once"]
    export = root.parse_args([
        "export", "snapshots", "--all", "--format", "parquet",
        "--out", "/tmp/snapshots.parquet",
    ])
    assert export.export_command == "snapshots"
    deltas = root.parse_args([
        "stats", "deltas", "--economic-observations", "--out",
        "/tmp/deltas.json",
    ])
    assert deltas.stats_command == "deltas"
    transitions = root.parse_args([
        "stats", "transitions", "--out", "/tmp/transitions.json",
    ])
    assert transitions.stats_command == "transitions"


def test_the_weekly_digest_command_is_gone():
    """Owner decision D2a: the weekly digest is deleted, its subcommand too."""
    with pytest.raises(SystemExit):
        build_alert_parser().parse_args(["digest", "--window", "2026-W33", "--dry-run"])


def _persist_inputs(inputs) -> None:
    from app.db import session_scope

    with session_scope() as session:
        for index, item in enumerate(inputs, start=1):
            moment = datetime.fromisoformat(item.computed_at)
            session.add(AlertInputSnapshot(
                input_identity=item.input_identity,
                snapshot_id=None,
                origin=str(item.origin),
                built_at=moment,
                computed_at=moment,
                alert_input_schema_version=item.schema_version,
                methodology_version=item.methodology_version,
                methodology_sha256=item.methodology_sha256,
                reconstructed=item.reconstructed,
                evaluation_eligibility=str(item.evaluation_eligibility),
                ineligibility_reasons=list(item.ineligibility_reasons),
                payload=json.dumps(item.model_dump(mode="json"), sort_keys=True),
                payload_sha256=f"{index}" * 64,
            ))


def _transition_history():
    return [
        make_input(
            identity="a" * 64,
            computed_at="2026-08-15T02:00:00+00:00",
            effective="trim",
            base="trim",
            rf4=False,
            faber="in",
        ).model_copy(update={"snapshot_id": None}),
        make_input(
            identity="b" * 64,
            computed_at="2026-08-15T06:00:00+00:00",
            effective="de-risk",
            base="trim",
            rf4=True,
            faber="out",
        ).model_copy(update={"snapshot_id": None}),
        make_input(
            identity="c" * 64,
            computed_at="2026-08-15T10:00:00+00:00",
            effective="trim",
            base="trim",
            rf4=False,
            faber="in",
        ).model_copy(update={"snapshot_id": None}),
    ]


def test_full_snapshot_export_preserves_typed_point_in_time_provenance(isolated_db):
    from app.db import session_scope

    _persist_inputs(_transition_history())
    with session_scope() as session:
        rows = snapshot_export_rows(session)

    assert len(rows) == 3
    middle = rows[1]
    assert middle["headline_median"] != middle["point_score"]
    assert middle["base_action_band"] == "trim"
    assert middle["effective_action_state"] == "de-risk"
    assert middle["expected_recompute_slot"]
    red_flags = json.loads(middle["red_flags_json"])
    evidence = json.loads(middle["indicators_json"])
    assert red_flags[0]["flag_id"] == "rf4"
    assert evidence[0]["economic_observation_key"]
    assert evidence[0]["source_revision_key"]
    assert evidence[0]["computation_fingerprint"]
    assert "legs_json" in middle
    assert "falsification_events_json" in middle


def test_delta_statistics_never_conflate_observations_revisions_and_runs(isolated_db):
    from app.db import session_scope

    _persist_inputs(_transition_history())
    with session_scope() as session:
        report = economic_observation_statistics(session)

    breadth = next(
        row for row in report["by_domain"]
        if row["observation_domain_id"] == "indicator.d1.breadth"
    )
    assert breadth["evidence_occurrence_count"] == 3
    assert breadth["recomputation_count"] == 3
    # All three synthetic runs refer to the same persisted economic period.
    assert breadth["economic_observation_count"] == 1
    assert breadth["source_revision_count"] == 1
    assert breadth["computation_fingerprint_count"] == 1
    assert breadth["repeat_occurrence_count"] == 2


def test_transition_statistics_cover_every_mandated_operational_family(isolated_db):
    from app.db import session_scope

    _persist_inputs(_transition_history())
    with session_scope() as session:
        report = transition_statistics(session)

    assert report["entering_derisk_by_origin"] == {
        "hold": 0,
        "suppressed": 0,
        "trim": 1,
    }
    assert report["one_snapshot_derisk_reversal_count"] == 1
    assert report["base_effective_divergence_count"] == 1
    assert report["red_flag_activations"]["rf4"] == 1
    assert report["faber_transitions"] == {
        "SPY:in->out": 1,
        "SPY:out->in": 1,
    }
    for required in (
        "non_fresh_evidence_by_state",
        "stale_episode_count",
        "data_quality_guard_episode_count",
        "sidecar_gap_count",
        "evaluation_timeout_count",
        "evaluation_conflict_count",
        "unknown_delivery_count",
    ):
        assert required in report


def test_statistics_cli_writes_reports_without_mutating_the_database(
        isolated_db, tmp_path, capsys):
    from app.cli import main
    from app.db import session_scope

    _persist_inputs(_transition_history())
    with session_scope() as session:
        before = session.execute(
            select(func.count()).select_from(AlertInputSnapshot)
        ).scalar_one()

    deltas = tmp_path / "deltas.json"
    transitions = tmp_path / "transitions.json"
    assert main([
        "stats", "deltas", "--economic-observations", "--out", str(deltas),
    ]) == 0
    assert main([
        "stats", "transitions", "--out", str(transitions),
    ]) == 0
    capsys.readouterr()
    assert json.loads(deltas.read_text(encoding="utf-8"))["report"] == \
        "economic-observation-deltas"
    assert json.loads(transitions.read_text(encoding="utf-8"))["report"] == \
        "alert-transition-statistics"
    with session_scope() as session:
        after = session.execute(
            select(func.count()).select_from(AlertInputSnapshot)
        ).scalar_one()
    assert after == before
