"""Top-level ``bubblegauge`` command.

Alert operations retain their established parser under ``bubblegauge alerts``;
the root adds the point-in-time export and governance-statistics surfaces from
mandate section 26.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def cmd_alerts(args: argparse.Namespace) -> int:
    from app.alerts.cli import main as alerts_main

    return alerts_main(args.alert_args)


def cmd_export_snapshots(args: argparse.Namespace) -> int:
    from app.alerts.reports import snapshot_export_rows, write_parquet
    from app.db import get_engine

    with Session(get_engine()) as session:
        rows = snapshot_export_rows(session)
    try:
        write_parquet(rows, Path(args.out))
    except RuntimeError as exc:
        _print({"ok": False, "error": str(exc)})
        return 1
    _print({"ok": True, "format": "parquet", "rows": len(rows),
            "out": str(Path(args.out))})
    return 0


def cmd_stats_deltas(args: argparse.Namespace) -> int:
    from app.alerts.reports import (
        economic_observation_statistics,
        write_json_report,
    )
    from app.db import get_engine

    with Session(get_engine()) as session:
        report = economic_observation_statistics(session)
    write_json_report(report, Path(args.out))
    _print({"ok": True, "report": report["report"], "out": str(Path(args.out))})
    return 0


def cmd_stats_transitions(args: argparse.Namespace) -> int:
    from app.alerts.reports import transition_statistics, write_json_report
    from app.db import get_engine

    with Session(get_engine()) as session:
        report = transition_statistics(session)
    write_json_report(report, Path(args.out))
    _print({"ok": True, "report": report["report"], "out": str(Path(args.out))})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bubblegauge")
    commands = parser.add_subparsers(dest="root_command", required=True)

    alerts = commands.add_parser(
        "alerts",
        help="alert-system operator commands",
        add_help=False,
    )
    alerts.add_argument("alert_args", nargs=argparse.REMAINDER)
    alerts.set_defaults(func=cmd_alerts)

    export = commands.add_parser("export", help="point-in-time data exports")
    export_commands = export.add_subparsers(dest="export_command", required=True)
    snapshots = export_commands.add_parser(
        "snapshots", help="export all persisted alert sidecars"
    )
    snapshots.add_argument("--all", action="store_true", required=True)
    snapshots.add_argument("--format", choices=("parquet",), required=True)
    snapshots.add_argument("--out", required=True)
    snapshots.set_defaults(func=cmd_export_snapshots)

    stats = commands.add_parser("stats", help="alert governance statistics")
    stats_commands = stats.add_subparsers(dest="stats_command", required=True)
    deltas = stats_commands.add_parser(
        "deltas", help="observation/revision/recomputation counts"
    )
    deltas.add_argument(
        "--economic-observations", action="store_true", required=True
    )
    deltas.add_argument("--out", required=True)
    deltas.set_defaults(func=cmd_stats_deltas)
    transitions = stats_commands.add_parser(
        "transitions", help="decision and alert transition counts"
    )
    transitions.add_argument("--out", required=True)
    transitions.set_defaults(func=cmd_stats_transitions)
    return parser


def main(argv: list[str] | None = None) -> int:
    effective = list(sys.argv[1:] if argv is None else argv)
    # Delegate before the root parser consumes options: the alert parser owns
    # its complete nested help and option surface.  A lightweight root entry
    # above keeps ``bubblegauge --help`` honest about the command's existence.
    if effective and effective[0] == "alerts":
        from app.alerts.cli import main as alerts_main

        return alerts_main(effective[1:])
    args = build_parser().parse_args(effective)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
