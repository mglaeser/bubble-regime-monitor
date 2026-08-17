"""Run an alert replay and write the Stage-gate evidence artifact.

A thin, deliberate wrapper around `app.alerts.cli dryrun`. It exists because a
gate is a thing an operator runs and commits the output of, and that deserves
an entry point with a stable name and a stable default output path rather than
a remembered flag combination.

    python -m scripts.alert_replay --state-db /tmp/replay.db
    python -m scripts.alert_replay --state-db /tmp/replay.db --from 2025-01-01
    python -m scripts.alert_replay --state-db /tmp/replay.db --out docs/…json

Exit codes are the gate:

    0   the replay ran and every check passed
    1   the replay ran and a check FAILED, or the artifacts are invalid

Nothing here can send: `app.alerts.replay` imports no provider and runs in
`dryrun` mode, whose state lives in its own namespace.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_EVENTS = Path(__file__).resolve().parents[1] / "config" / \
    "alert_mandatory_events.v3.2.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alert_replay",
        description="Replay captured alert sidecars into an isolated database",
    )
    parser.add_argument("--state-db", required=True,
                        help="path for the ISOLATED replay database (recreated each run)")
    parser.add_argument("--from", dest="from_moment", help="ISO-8601 window start")
    parser.add_argument("--to", dest="to_moment", help="ISO-8601 window end")
    parser.add_argument("--source-db", help="source DB URL (default: the app's)")
    parser.add_argument("--rules", help="ruleset path (default: the configured candidate)")
    parser.add_argument("--phrases", help="phrase-set path (default: the configured one)")
    parser.add_argument("--events", default=str(DEFAULT_EVENTS),
                        help="mandatory-event catalogue (default: the shipped one)")
    parser.add_argument("--stage", type=int,
                        help="gate rules at this rollout stage instead of the "
                             "committed one — how the evidence for advancing to "
                             "stage N is gathered WITHOUT advancing to it")
    parser.add_argument("--out", help="also write the summary JSON here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from app.alerts.cli import main as cli_main

    forwarded = ["dryrun", "--state-db", args.state_db]
    for flag, value in (("--from", args.from_moment), ("--to", args.to_moment),
                        ("--source-db", args.source_db), ("--rules", args.rules),
                        ("--phrases", args.phrases), ("--out", args.out)):
        if value:
            forwarded += [flag, value]
    # An events file that does not exist is not an error: the catalogue ships
    # empty, and replay reports recall as "not measurable" rather than 100%.
    if args.events and Path(args.events).exists():
        forwarded += ["--events", args.events]
    if args.stage is not None:
        forwarded += ["--stage", str(args.stage)]
    return cli_main(forwarded)


if __name__ == "__main__":
    sys.exit(main())
