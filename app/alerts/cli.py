"""`bubblegauge alerts ...` — the operator surface.

Deliberately the only place `--promote` exists outside the admin API, and
deliberately not something a deploy script runs by default. Every command is
read-only unless its name says otherwise.

    python -m app.alerts.cli validate [--rules PATH] [--phrases PATH] [--promote]
    python -m app.alerts.cli preflight
    python -m app.alerts.cli ruleset
    python -m app.alerts.cli health
    python -m app.alerts.cli pending
    python -m app.alerts.cli evaluate --input-identity ID [--shadow]
    python -m app.alerts.cli dryrun [--from T] [--to T] --state-db PATH [--out PATH]
    python -m app.alerts.cli explain --evaluation-id ID
    python -m app.alerts.cli recover-evaluations --once
    python -m app.alerts.cli reconcile-sidecars
    python -m app.alerts.cli watchdog --once     # exit 2 = outage detected
    python -m app.alerts.cli dispatch --once     # one outbox pass
    python -m app.alerts.cli retention [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def cmd_validate(args: argparse.Namespace) -> int:
    from app.alerts.artifacts import validate_from_disk
    from app.alerts.errors import AlertError
    from app.alerts.registry import ruleset_summary
    from app.db import session_scope

    try:
        artifacts = validate_from_disk(
            rules_path=Path(args.rules) if args.rules else None,
            phrase_path=Path(args.phrases) if args.phrases else None,
        )
    except AlertError as exc:
        _print({"valid": False, "error_code": exc.code, "problems": exc.redacted().split("; ")})
        return 1

    summary = ruleset_summary(artifacts.ruleset)
    summary["phrase_worst_case"] = artifacts.phrase_set.worst_case
    if args.promote:
        from app.alerts.promotion_service import validate_register_and_promote

        with session_scope() as session:
            decision = validate_register_and_promote(
                session, artifacts, actor=args.by or "cli")
        summary["promoted"] = decision.promoted
        if not decision.promoted:
            # Refused. The artifact stays VALIDATED and unpromoted, the current
            # promotion is untouched, and the exit code says so — a promotion
            # that prints its blockers and exits 0 reads as success in a script.
            summary["blockers"] = list(decision.blockers)
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 1
        summary["note"] = ("promotion does NOT enable delivery; ALERTS_MODE is "
                           "unchanged and must be set by hand")
    else:
        summary["promoted"] = False
    _print({"valid": True, **summary})
    return 0


def cmd_preflight(_args: argparse.Namespace) -> int:
    """Everything that must be true before a stage is advanced."""
    from app.alerts.artifacts import validate_from_disk
    from app.alerts.errors import AlertError
    from app.config import get_settings, near_miss_env_keys
    from app.db import session_scope
    from app.models import Snapshot

    settings = get_settings()
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    try:
        artifacts = validate_from_disk()
        check("artifacts_valid", True,
              f"rules {artifacts.ruleset.rules_sha256[:12]}, "
              f"phrases {artifacts.phrase_set.sha256[:12]}")
        check("artifact_source_is_candidate", artifacts.source == "candidate",
              f"source={artifacts.source}")
    except AlertError as exc:
        check("artifacts_valid", False, exc.redacted())
        artifacts = None

    with session_scope() as session:
        from sqlalchemy import text

        for pragma, expected in (("foreign_keys", 1), ("journal_mode", "wal")):
            value = session.execute(text(f"PRAGMA {pragma}")).scalar_one()
            check(f"sqlite_{pragma}", str(value).lower() == str(expected).lower(),
                  f"{pragma}={value}")
        busy = session.execute(text("PRAGMA busy_timeout")).scalar_one()
        check("sqlite_busy_timeout", int(busy) > 0, f"busy_timeout={busy}")

        typed = session.execute(
            select(Snapshot).where(Snapshot.alert_contract_version.is_not(None)).limit(1)
        ).scalars().first()
        check("typed_snapshot_contract_present", typed is not None,
              "at least one snapshot carries the typed alert contract"
              if typed else "no snapshot has been written since Stage 0")

    check("capture_enabled", settings.alert_input_capture,
          f"ALERT_INPUT_CAPTURE={settings.alert_input_capture}")
    check("alerts_mode_recorded", True, f"ALERTS_MODE={settings.alerts_mode}")
    check("legacy_daily_digest", True,
          f"transport={settings.daily_digest_transport} "
          f"(effective_daily_sms_enabled={settings.effective_daily_sms_enabled}, "
          f"imessage_enabled={settings.imessage_enabled})")
    # Fails the preflight rather than merely reporting: a digest configured
    # under a misspelt key is indistinguishable from one deliberately off, and
    # this is the house mechanism for surfacing exactly that.
    near = near_miss_env_keys(os.environ)
    check("no_misspelt_settings", not near,
          "; ".join(f"{actual} looks like {intended}" for actual, intended in near)
          or "no near-miss environment keys")
    check("imessage_switch_matches_config", not settings.imessage_enabled_but_unconfigured,
          "IMESSAGE_ENABLED is on but URL/key/recipient are not all set — the digest "
          "is NOT going over iMessage" if settings.imessage_enabled_but_unconfigured
          else "iMessage switch and configuration agree")
    if settings.imessage_configured:
        from app.notify.imessage import check_destination

        problem = check_destination(settings.imessage_api_base_url.rstrip("/"))
        check("imessage_destination_safe", problem is None,
              problem or "IMESSAGE_API_BASE_URL is https (or loopback http)")

    if artifacts is not None:
        pins = [r.rule_id for r in artifacts.ruleset.rules()
                if any(not t.is_pinned for t in r.thresholds)]
        check("unresolved_pins_are_disabled",
              all(not artifacts.ruleset.rule(rid).enabled for rid in pins),
              f"{len(pins)} rules carry unresolved pins; all must be disabled")

    _print({"ok": all(c["ok"] for c in checks), "checks": checks})
    return 0 if all(c["ok"] for c in checks) else 1


def cmd_watchdog(_args: argparse.Namespace) -> int:
    """Exit 2 on a detected outage — a timer can act on that directly."""
    from app.alerts.watchdog import run_once

    result = run_once()
    _print(result)
    return 2 if result["firing"] else 0


def cmd_dispatch(_args: argparse.Namespace) -> int:
    from app.jobs.alert_dispatch import run_once

    result = run_once()
    _print(result)
    return 0


def cmd_retention(args: argparse.Namespace) -> int:
    """Expire message bodies; keep the audit trail. `--dry-run` commits nothing."""
    from app.jobs.alert_retention import run_once

    result = run_once(dry_run=bool(args.dry_run))
    _print(result)
    return 1 if result.get("skipped") else 0


def cmd_ruleset(_args: argparse.Namespace) -> int:
    from app.alerts.artifacts import load_active
    from app.alerts.registry import ruleset_summary
    from app.db import session_scope

    with session_scope() as session:
        artifacts = load_active(session)
    payload = ruleset_summary(artifacts.ruleset)
    payload["source"] = artifacts.source
    payload["fallback_reason"] = artifacts.fallback_reason
    _print(payload)
    return 0


def cmd_health(_args: argparse.Namespace) -> int:
    from app.alerts.artifacts import load_active
    from app.alerts.errors import AlertingUnavailable
    from app.alerts.health import health_projection
    from app.config import get_settings
    from app.db import session_scope

    with session_scope() as session:
        try:
            artifacts = load_active(session)
            ruleset, source, reason = artifacts.ruleset, artifacts.source, \
                artifacts.fallback_reason
        except AlertingUnavailable as exc:
            ruleset, source, reason = None, "unavailable", str(exc)
        payload = health_projection(session, settings=get_settings(), ruleset=ruleset,
                                    artifact_source=source, fallback_reason=reason)
    _print(payload)
    return 0 if payload["status"] != "critical" else 1


def cmd_pending(_args: argparse.Namespace) -> int:
    from app.alerts.health import episode_projection
    from app.alerts.models import AlertEpisode
    from app.db import session_scope

    with session_scope() as session:
        rows = session.execute(
            select(AlertEpisode).where(AlertEpisode.is_open.is_(True))
            .order_by(AlertEpisode.opened_at.asc())
        ).scalars().all()
        _print({"open_episodes": [episode_projection(r) for r in rows]})
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from app.services.alert_integration import evaluate_input

    outcome = evaluate_input(args.input_identity, mode="shadow" if args.shadow else None)
    if outcome is None:
        _print({"error": "no captured sidecar with that identity, or alerting is disabled"})
        return 1
    _print({
        "evaluation_id": outcome.evaluation_id,
        "status": outcome.status,
        "rules_evaluated": outcome.rules_evaluated,
        "duration_ms": outcome.duration_ms,
        "firing": [d.rule_id for d in outcome.firing],
        "notification_eligible": [d.rule_id for d in outcome.notification_eligible],
    })
    return 0


def _parse_moment(raw: str | None) -> Any:
    from datetime import UTC, datetime

    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def cmd_dryrun(args: argparse.Namespace) -> int:
    """Replay captured sidecars into an ISOLATED database — the Stage 1 gate.

    Reads history, writes nothing to production, sends nothing. Exit 1 when the
    replay itself failed a check, so a gate script can rely on the status code.
    """
    from app.alerts.artifacts import validate_from_disk
    from app.alerts.errors import AlertError
    from app.alerts.replay import ReplayConfig, run_replay
    from app.config import get_settings

    try:
        artifacts = validate_from_disk(
            rules_path=Path(args.rules) if args.rules else None,
            phrase_path=Path(args.phrases) if args.phrases else None,
        )
    except AlertError as exc:
        _print({"error_code": exc.code, "detail": exc.redacted()})
        return 1

    settings = get_settings()
    config = ReplayConfig(
        source_db_url=args.source_db or settings.db_url,
        state_db_path=Path(args.state_db),
        from_moment=_parse_moment(args.from_moment),
        to_moment=_parse_moment(args.to_moment),
        mandatory_events_path=Path(args.events) if args.events else None,
        evaluate_at_stage=args.stage,
    )
    try:
        summary = run_replay(config=config, ruleset=artifacts.ruleset,
                             phrase_set=artifacts.phrase_set)
    except OperationalError:
        # Overwhelmingly: the source database is not where the settings say, or
        # capture has never run so the file does not exist yet. The URL is not
        # echoed back — a database URL is the shape of thing that carries a
        # credential, even when this one does not.
        _print({"error_code": "REPLAY_SOURCE_UNREADABLE",
                "detail": "the source database could not be opened — check "
                          "DB_URL or pass --source-db, and confirm "
                          "ALERT_INPUT_CAPTURE has been on long enough to have "
                          "written sidecars"})
        return 1

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(summary.to_json() + "\n", encoding="utf-8")
    _print(summary.as_dict())
    return 0 if summary.passed else 1


def cmd_explain(args: argparse.Namespace) -> int:
    """Facts and decisions for one evaluation. Never private reasoning."""
    from app.alerts.health import iso
    from app.alerts.models import AlertEvaluation, AlertEvaluationRuleset, AlertEvent
    from app.db import session_scope

    with session_scope() as session:
        row = session.get(AlertEvaluation, args.evaluation_id)
        if row is None:
            _print({"error": "unknown evaluation id"})
            return 1
        rulesets = session.execute(
            select(AlertEvaluationRuleset).where(
                AlertEvaluationRuleset.evaluation_id == args.evaluation_id)
        ).scalars().all()
        events = session.execute(
            select(AlertEvent).where(AlertEvent.evaluation_id == args.evaluation_id)
            .order_by(AlertEvent.occurred_at.asc())
        ).scalars().all()
        _print({
            "evaluation_id": row.evaluation_id,
            "status": row.status,
            "mode": row.mode,
            "input_identity": row.input_identity,
            "current_rules_sha256": row.current_rules_sha256,
            "evaluated_ruleset_hashes": list(row.evaluated_ruleset_hashes or []),
            "attempt_count": row.attempt_count,
            "duration_ms": row.duration_ms,
            "plan_applied": bool(row.plan_applied),
            "error_code": row.error_code,
            "rulesets": [{"rules_sha256": r.rules_sha256, "role": r.role,
                          "status": r.status, "instances": r.instances_evaluated}
                         for r in rulesets],
            "decisions": [{"occurred_at": iso(e.occurred_at), "action": e.action,
                           "rule_id": e.rule_id, "episode_id": e.episode_id,
                           "detail": e.detail_redacted} for e in events],
        })
    return 0


def cmd_recover(_args: argparse.Namespace) -> int:
    from app.alerts.recovery import recover_evaluations
    from app.db import session_scope

    with session_scope() as session:
        report = recover_evaluations(session)
    _print({"abandoned": report.abandoned, "inconsistent": report.inconsistent,
            "in_progress": report.in_progress, "needs_operator": report.needs_operator})
    return 1 if report.needs_operator else 0


def cmd_reconcile(_args: argparse.Namespace) -> int:
    from app.alerts.recovery import reconcile_sidecars
    from app.db import session_scope

    with session_scope() as session:
        gaps = reconcile_sidecars(session)
    _print({"sidecar_gaps": gaps, "count": len(gaps)})
    return 1 if gaps else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bubblegauge alerts",
                                     description="Alert-system operator commands")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate the artifacts on disk")
    validate.add_argument("--rules")
    validate.add_argument("--phrases")
    validate.add_argument("--promote", action="store_true",
                          help="PROMOTE the validated ruleset (operator action)")
    validate.add_argument("--by", help="operator label recorded with the promotion")
    validate.set_defaults(func=cmd_validate)

    sub.add_parser("preflight", help="pre-stage checks").set_defaults(func=cmd_preflight)
    watchdog = sub.add_parser("watchdog", help="check for missed recompute slots")
    watchdog.add_argument("--once", action="store_true", default=True)
    watchdog.set_defaults(func=cmd_watchdog)
    dispatch = sub.add_parser("dispatch", help="run one pass of the delivery outbox")
    dispatch.add_argument("--once", action="store_true", default=True)
    dispatch.set_defaults(func=cmd_dispatch)
    retention = sub.add_parser("retention",
                               help="expire message bodies, keep the audit trail")
    retention.add_argument("--dry-run", action="store_true",
                           help="report what would be swept; commit nothing")
    retention.set_defaults(func=cmd_retention)
    sub.add_parser("ruleset", help="active ruleset summary").set_defaults(func=cmd_ruleset)
    sub.add_parser("health", help="health projection").set_defaults(func=cmd_health)
    sub.add_parser("pending", help="open episodes").set_defaults(func=cmd_pending)
    sub.add_parser("reconcile-sidecars", help="snapshots with no sidecar").set_defaults(
        func=cmd_reconcile)

    evaluate = sub.add_parser("evaluate", help="evaluate one captured sidecar")
    evaluate.add_argument("--input-identity", required=True)
    evaluate.add_argument("--shadow", action="store_true", default=True)
    evaluate.set_defaults(func=cmd_evaluate)

    dryrun = sub.add_parser("dryrun", help="replay captured sidecars (isolated, no send)")
    dryrun.add_argument("--state-db", required=True,
                        help="path for the ISOLATED replay database (recreated)")
    dryrun.add_argument("--from", dest="from_moment", help="ISO-8601 window start")
    dryrun.add_argument("--to", dest="to_moment", help="ISO-8601 window end")
    dryrun.add_argument("--source-db", help="source DB URL (default: the app's)")
    dryrun.add_argument("--rules")
    dryrun.add_argument("--phrases")
    dryrun.add_argument("--events", help="mandatory-event catalogue JSON")
    dryrun.add_argument("--stage", type=int,
                        help="gate rules at this rollout stage instead of the "
                             "committed one (dry-run only; changes nothing live)")
    dryrun.add_argument("--out", help="write the summary JSON here as well")
    dryrun.set_defaults(func=cmd_dryrun)

    explain = sub.add_parser("explain", help="facts and decisions for one evaluation")
    explain.add_argument("--evaluation-id", required=True)
    explain.set_defaults(func=cmd_explain)

    recover = sub.add_parser("recover-evaluations", help="sweep stale evaluation leases")
    recover.add_argument("--once", action="store_true", default=True)
    recover.set_defaults(func=cmd_recover)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
