"""Operator commands and read-only alert governance reports."""

from __future__ import annotations

import json
import tomllib
from datetime import datetime
from pathlib import Path

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

ROOT = Path(__file__).resolve().parents[1]


def _containerfile_instructions(keyword: str) -> list[str]:
    """The arguments of every Containerfile instruction of one kind, read as
    the builder reads the file: comment lines dropped, continuation lines
    joined."""
    lines = [line for line in (ROOT / "Containerfile").read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("#")]
    instructions = [line.split(None, 1) for line in "\n".join(lines).replace("\\\n", " ").splitlines()]
    return [parts[1].strip() for parts in instructions if len(parts) == 2 and parts[0] == keyword]


def test_the_image_puts_the_source_where_the_command_looks():
    """README ("the CLI in the container") and docs/ALERT_SYSTEM.md ("Operating
    it") name `bubblegauge ...` as the operator's command in the image. pip
    writes that command from `[project.scripts]` as a script that imports
    `app.cli`. The image installs the project before it copies the source, so
    site-packages holds no `app`, and a script's first import path is the
    script's own directory, not the working directory: the source is found
    only where `PYTHONPATH` points. Without it the command ended in
    `ModuleNotFoundError: No module named 'app'`.

    This pins the file. That the command runs in the built image is checked by
    the step after the build in CI's `image` job."""
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts == {"bubblegauge": "app.cli:main"}
    (workdir,) = _containerfile_instructions("WORKDIR")
    assert workdir == "/app"
    assert ". ." in _containerfile_instructions("COPY"), "the source is copied into the working directory"
    env = dict(pair.split("=", 1) for line in _containerfile_instructions("ENV") for pair in line.split())
    assert env.get("PYTHONPATH") == workdir, env


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


def test_alerts_evaluate_has_one_mode(monkeypatch, capsys):
    """README, the operator table: "`bubblegauge alerts evaluate` always runs
    in shadow and sends nothing". So the command takes no mode option: a
    `--shadow` that was on whether given or not offered a choice that did not
    exist. A live evaluation is the recompute's, the recovery job's or the
    admin route's (`shadow=false`), never this command's. (`--once` on the
    other commands is as inert and stays: it is the spelling README and
    docs/ALERT_SYSTEM.md give for them.)"""
    import app.services.alert_integration as integration
    from app.alerts.cli import main

    calls: list[tuple[str, str | None]] = []

    def record(input_identity: str, *, mode: str | None = None, now=None) -> None:
        calls.append((input_identity, mode))

    monkeypatch.setattr(integration, "evaluate_input", record)
    monkeypatch.setenv("ALERTS_MODE", "live")      # the host's mode is not the command's

    assert main(["evaluate", "--input-identity", "x"]) == 1      # no such sidecar
    assert calls == [("x", "shadow")]
    with pytest.raises(SystemExit):
        build_alert_parser().parse_args(["evaluate", "--input-identity", "x", "--shadow"])
    capsys.readouterr()


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


def test_a_snapshot_from_before_the_typed_contract_is_no_sidecar_gap(isolated_db):
    """A sidecar gap is a TYPED snapshot without its sidecar, the definition
    health and `reconcile_sidecars` use (docs/ALERT_SYSTEM.md, section 11:
    "missing typed sidecars"). A snapshot stored before the typed contract has
    none by design and nothing can build one for it."""
    from app.alerts.recovery import reconcile_sidecars
    from app.db import session_scope
    from app.models import Snapshot

    def snapshot(computed_at: str, contract: int | None) -> Snapshot:
        return Snapshot(
            computed_at=datetime.fromisoformat(computed_at), service_version="test",
            median=52.0, iqr_lo=50.0, iqr_hi=55.0, band5=47.0, band95=58.0,
            point_score=52.0, action_band="trim", alert_contract_version=contract)

    with session_scope() as session:
        session.add(snapshot("2026-07-11T21:26:00+00:00", None))
        typed = snapshot("2026-08-20T18:01:00+00:00", 1)
        session.add(typed)
        session.flush()
        typed_id = typed.id

    with session_scope() as session:
        report = transition_statistics(session)
        # The job that reports gaps, on the same rows: the typed one alone.
        assert reconcile_sidecars(session) == [typed_id]
    assert report["sidecar_gap_count"] == 1


def test_a_change_of_letter_case_is_no_faber_transition(isolated_db):
    """The report's key says transitions: a trend leg that changed state. The
    stored word changed its spelling once ("IN", then "in") while the leg
    stayed in; that is no transition, and the next real one is still counted."""
    from app.db import session_scope

    history = [
        make_input(identity=letter * 64, computed_at=f"2026-08-25T{hour}:00:00+00:00",
                   faber=word).model_copy(update={"snapshot_id": None})
        for letter, hour, word in (("a", "18", "IN"), ("b", "22", "in"), ("c", "23", "out"))
    ]
    _persist_inputs(history)
    with session_scope() as session:
        report = transition_statistics(session)

    assert report["faber_transitions"] == {"SPY:in->out": 1}


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
