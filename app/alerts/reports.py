"""Read-only point-in-time exports and governance statistics.

Every report is derived from persisted alert sidecars and alert metadata.  No
provider is imported, no current market state is queried, and no score is
recomputed.  Nested evidence remains typed in the source payload and is stored
as canonical JSON cells when a flat Parquet row is required.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts import observation as obs
from app.alerts.dto import AlertInput, EvidenceModel
from app.alerts.enums import DataState, InputOrigin, TransportStatus
from app.alerts.models import (
    AlertDelivery,
    AlertEpisode,
    AlertEvaluation,
    AlertInputSnapshot,
)
from app.models import Snapshot

REPORT_SCHEMA_VERSION = 1


def _json_cell(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def _inputs(session: Session) -> list[tuple[AlertInputSnapshot, AlertInput]]:
    rows = session.execute(
        select(AlertInputSnapshot).order_by(
            func.coalesce(
                AlertInputSnapshot.computed_at,
                AlertInputSnapshot.built_at,
            ).asc(),
            AlertInputSnapshot.input_identity.asc(),
        )
    ).scalars().all()
    return [
        (row, AlertInput.model_validate(json.loads(row.payload)))
        for row in rows
    ]


def snapshot_export_rows(session: Session) -> list[dict[str, Any]]:
    """One flat, lossless row per immutable point-in-time sidecar."""
    exported: list[dict[str, Any]] = []
    for row, item in _inputs(session):
        exported.append({
            "input_identity": item.input_identity,
            "payload_sha256": row.payload_sha256,
            "snapshot_id": item.snapshot_id,
            "prev_snapshot_id": item.prev_snapshot_id,
            "origin": str(item.origin),
            "built_at": item.built_at,
            "computed_at": item.computed_at,
            "expected_recompute_slot": item.expected_recompute_slot,
            "alert_input_schema_version": item.schema_version,
            "service_version": item.service_version,
            "methodology_version": item.methodology_version,
            "methodology_sha256": item.methodology_sha256,
            "headline_median": item.headline_median,
            "point_score": item.point_score,
            "iqr_lo": item.iqr_lo,
            "iqr_hi": item.iqr_hi,
            "band5": item.band5,
            "band95": item.band95,
            "score_action_band": item.score_action_band,
            "base_action_band": item.base_action_band,
            "effective_action_state": item.effective_action_state,
            "band_suppressed_by_coverage": item.band_suppressed_by_coverage,
            "data_degraded": item.data_degraded,
            "override_fired": item.override_fired,
            "override_required_count": item.override_required_count,
            "override_fireable_universe_count": (
                item.override_fireable_universe_count
            ),
            "evaluation_eligibility": str(item.evaluation_eligibility),
            "reconstructed": item.reconstructed,
            "ineligibility_reasons_json": _json_cell(item.ineligibility_reasons),
            "red_flags_json": _json_cell(
                [value.model_dump(mode="json") for value in item.red_flags]
            ),
            "indicators_json": _json_cell(
                [value.model_dump(mode="json") for value in item.indicators]
            ),
            "legs_json": _json_cell(
                [value.model_dump(mode="json") for value in item.legs]
            ),
            "credit_sidecar_json": _json_cell(
                [value.model_dump(mode="json") for value in item.credit_sidecar]
            ),
            "coverage_json": _json_cell(item.coverage),
            "blocks_json": _json_cell(item.blocks),
            "fast_alarm_json": _json_cell(item.fast_alarm),
            "release_calendar_json": _json_cell(item.release_calendar),
            "falsification_events_json": _json_cell(item.falsification_events),
            "source_health_summary_json": _json_cell(item.source_health_summary),
        })
    return exported


def write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    """Write rows when the explicitly optional Parquet engine is installed."""
    from app.services.backfill import _parquet_available

    if not _parquet_available():
        raise RuntimeError(
            "Parquet support is unavailable on this host; install "
            "bubblegauge[parquet] on a CPU supported by pyarrow"
        )
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)


def write_json_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _all_evidence(item: AlertInput) -> list[EvidenceModel]:
    return [*item.indicators, *item.legs, *item.credit_sidecar]


def economic_observation_statistics(session: Session) -> dict[str, Any]:
    """Keep observations, source revisions, computations, and runs distinct."""
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "economic": set(),
        "revisions": set(),
        "computations": set(),
        "input_ids": set(),
        "recompute_input_ids": set(),
        "occurrences": 0,
        "economic_revisions": defaultdict(set),
        "revision_computations": defaultdict(set),
    })
    all_economic: set[str] = set()
    all_revisions: set[str] = set()
    all_computations: set[str] = set()
    all_inputs: set[str] = set()
    recompute_inputs: set[str] = set()
    occurrences = 0

    for _row, item in _inputs(session):
        all_inputs.add(item.input_identity)
        if item.origin == InputOrigin.RECOMPUTE:
            recompute_inputs.add(item.input_identity)
        for evidence in _all_evidence(item):
            bucket = buckets[evidence.observation_domain_id]
            bucket["economic"].add(evidence.economic_observation_key)
            bucket["revisions"].add(evidence.source_revision_key)
            bucket["computations"].add(evidence.computation_fingerprint)
            bucket["input_ids"].add(item.input_identity)
            if item.origin == InputOrigin.RECOMPUTE:
                bucket["recompute_input_ids"].add(item.input_identity)
            bucket["occurrences"] += 1
            bucket["economic_revisions"][evidence.economic_observation_key].add(
                evidence.source_revision_key
            )
            bucket["revision_computations"][evidence.source_revision_key].add(
                evidence.computation_fingerprint
            )
            all_economic.add(evidence.economic_observation_key)
            all_revisions.add(evidence.source_revision_key)
            all_computations.add(evidence.computation_fingerprint)
            occurrences += 1

    by_domain: list[dict[str, Any]] = []
    for domain, bucket in sorted(buckets.items()):
        revision_deltas = sum(
            max(0, len(values) - 1)
            for values in bucket["economic_revisions"].values()
        )
        computation_deltas = sum(
            max(0, len(values) - 1)
            for values in bucket["revision_computations"].values()
        )
        by_domain.append({
            "observation_domain_id": domain,
            "economic_observation_count": len(bucket["economic"]),
            "source_revision_count": len(bucket["revisions"]),
            "computation_fingerprint_count": len(bucket["computations"]),
            "input_occurrence_count": len(bucket["input_ids"]),
            "recomputation_count": len(bucket["recompute_input_ids"]),
            "evidence_occurrence_count": int(bucket["occurrences"]),
            "repeat_occurrence_count": max(
                0, int(bucket["occurrences"]) - len(bucket["economic"])
            ),
            "source_revision_delta_count": revision_deltas,
            "computation_delta_count": computation_deltas,
        })

    return {
        "report": "economic-observation-deltas",
        "schema_version": REPORT_SCHEMA_VERSION,
        "source": "alert_input_snapshot payloads",
        "totals": {
            "input_count": len(all_inputs),
            "recomputation_count": len(recompute_inputs),
            "evidence_occurrence_count": occurrences,
            "economic_observation_count": len(all_economic),
            "source_revision_count": len(all_revisions),
            "computation_fingerprint_count": len(all_computations),
        },
        "by_domain": by_domain,
    }


def _moment(item: AlertInput) -> datetime:
    raw = item.computed_at or item.built_at
    parsed = datetime.fromisoformat(raw)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _faber_value(item: AlertInput, domain: str) -> str | None:
    evidence = item.evidence_for(domain)
    if evidence is None or evidence.value is None \
            or evidence.data_state == DataState.MISSING:
        return None
    return str(evidence.value)


def _rf_value(item: AlertInput, flag_id: str) -> bool | None:
    flag = item.red_flag(flag_id)
    if flag is None or not flag.fireable \
            or flag.state in {"UNKNOWN", "BLOCKED"}:
        return None
    return bool(flag.active)


def transition_statistics(session: Session) -> dict[str, Any]:
    """Governance transition inventory from sidecars plus persisted outcomes."""
    items = [
        item for _row, item in _inputs(session)
        if item.origin == InputOrigin.RECOMPUTE
    ]
    items.sort(key=lambda item: (_moment(item), item.input_identity))

    entering_derisk: Counter[str] = Counter({
        "hold": 0,
        "trim": 0,
        "suppressed": 0,
    })
    base_effective_divergence = 0
    transient_derisk = 0
    rf_activations: Counter[str] = Counter({"rf3": 0, "rf4": 0})
    faber_transitions: Counter[str] = Counter()
    evidence_states: Counter[str] = Counter()

    for item in items:
        if item.base_action_band is not None \
                and item.effective_action_state is not None \
                and item.base_action_band != item.effective_action_state:
            base_effective_divergence += 1
        for evidence in _all_evidence(item):
            if evidence.data_state != DataState.FRESH:
                evidence_states[str(evidence.data_state)] += 1

    for previous, current in zip(items, items[1:], strict=False):
        origin = previous.effective_action_state
        if origin is not None and origin in entering_derisk \
                and current.effective_action_state == "de-risk":
            entering_derisk[origin] += 1
        for flag_id in ("rf3", "rf4"):
            if _rf_value(previous, flag_id) is False \
                    and _rf_value(current, flag_id) is True:
                rf_activations[flag_id] += 1
        for asset, domain in (
            ("SPY", obs.DOMAIN_LEG_SPY_FABER),
            ("QQQ", obs.DOMAIN_LEG_QQQ_FABER),
        ):
            before = _faber_value(previous, domain)
            after = _faber_value(current, domain)
            if before is not None and after is not None and before != after:
                faber_transitions[f"{asset}:{before}->{after}"] += 1

    for index in range(1, len(items) - 1):
        before = items[index - 1].effective_action_state
        current_state = items[index].effective_action_state
        after = items[index + 1].effective_action_state
        if current_state == "de-risk" and before not in {None, "de-risk"} \
                and after not in {None, "de-risk"}:
            transient_derisk += 1

    episodes = session.execute(select(AlertEpisode)).scalars().all()
    stale_episodes = sum(
        1 for episode in episodes if episode.episode_status == "CANCELLED_STALE"
    )
    data_quality_episodes = sum(
        1 for episode in episodes
        if "DATA_QUALITY_GUARD" in (episode.suppression_reasons or [])
    )
    unknown_block_episodes = sum(
        1 for episode in episodes
        if "UNKNOWN_BLOCK" in (episode.suppression_reasons or [])
    )

    sidecar_gaps = session.execute(
        select(func.count(Snapshot.id))
        .select_from(Snapshot)
        .outerjoin(
            AlertInputSnapshot,
            AlertInputSnapshot.snapshot_id == Snapshot.id,
        )
        .where(AlertInputSnapshot.input_identity.is_(None))
    ).scalar_one()
    evaluation_statuses = Counter(
        str(status) for status in session.execute(
            select(AlertEvaluation.status)
        ).scalars().all()
    )
    unknown_deliveries = session.execute(
        select(func.count(AlertDelivery.delivery_id)).where(
            AlertDelivery.transport_status == TransportStatus.UNKNOWN
        )
    ).scalar_one()

    return {
        "report": "alert-transition-statistics",
        "schema_version": REPORT_SCHEMA_VERSION,
        "source": "alert sidecars and persisted alert metadata",
        "recompute_input_count": len(items),
        "entering_derisk_by_origin": dict(sorted(entering_derisk.items())),
        "one_snapshot_derisk_reversal_count": transient_derisk,
        "base_effective_divergence_count": base_effective_divergence,
        "red_flag_activations": dict(sorted(rf_activations.items())),
        "faber_transitions": dict(sorted(faber_transitions.items())),
        "non_fresh_evidence_by_state": dict(sorted(evidence_states.items())),
        "stale_episode_count": stale_episodes,
        "data_quality_guard_episode_count": data_quality_episodes,
        "unknown_block_episode_count": unknown_block_episodes,
        "sidecar_gap_count": int(sidecar_gaps),
        "evaluation_status_counts": dict(sorted(evaluation_statuses.items())),
        "evaluation_timeout_count": evaluation_statuses["TIMED_OUT"],
        "evaluation_conflict_count": evaluation_statuses["CONFLICT"],
        "unknown_delivery_count": int(unknown_deliveries),
    }
