"""Historical Replay Infrastructure (RM-1..RM-5, v3.8.0) — evidence and policy
replay over persisted snapshots.

PURPOSE (operator's replay milestone): turn the open PINs from qualitative
judgement into measurable evidence. Nothing here changes any scored value —
every function READS persisted snapshots and recomputes candidate-policy
outcomes on the side. Golden 52.43 untouched.

PIN DISCIPLINE: candidate thresholds exercised here (B2 0.50, B3 2/3, B5
counts 2..4) are the operator's previously enumerated CANDIDATES, reported
side by side; nothing is recommended and nothing is pinned. The existing
two-thirds band gate is the only live rule.

  * RM-1  record_outcome()        append-only falsification evidence
          evidence_summary()      latest stamp + outcome count (acceptance)
  * RM-4  b_policy_report()       B0-B5 coverage policies over history
  * RM-5  assemble_decision_packages()  per-PIN evidence packages; host-
                                  dependent studies explicitly PENDING
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from app import methodology as _M
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import FalsificationOutcome, Snapshot

log = get_logger(__name__)

_WS = _M.as_dict("aggregation", "block_s_weights")
_WD = _M.as_dict("aggregation", "block_d_weights")
_DROP = _M.get_path("coverage", "drop_threshold")          # 1/3 -> degraded < 2/3


# ------------------------------------------------------------------- RM-1 ---


def record_outcome(criterion: str, detail: str | None = None) -> int:
    """Append one falsification outcome (manual recording path, spec 15).
    The table is append-only at the DB level (migration 0006 triggers)."""
    criterion = (criterion or "").strip()
    if not criterion:
        raise ValueError("criterion must be non-empty")
    from datetime import UTC

    with session_scope() as session:
        row = FalsificationOutcome(criterion=criterion[:500],
                                   tripped_at=datetime.now(UTC),
                                   detail=(detail or None))
        session.add(row)
        session.flush()
        return row.id


def evidence_summary() -> dict[str, Any]:
    """RM-1 acceptance surface: the newest snapshot's methodology stamp plus
    the append-only outcome count."""
    with session_scope() as session:
        snap = session.execute(
            select(Snapshot).order_by(Snapshot.computed_at.desc()).limit(1)
        ).scalars().first()
        n_outcomes = len(session.execute(select(FalsificationOutcome.id)).all())
        stamped = len(session.execute(
            select(Snapshot.id).where(Snapshot.methodology_sha256.is_not(None))).all())
        total = len(session.execute(select(Snapshot.id)).all())
        return {
            "latest_snapshot": None if snap is None else {
                "computed_at": snap.computed_at.isoformat(),
                "service_version": snap.service_version,
                "methodology_sha256": snap.methodology_sha256,
                "methodology_version": snap.methodology_version,
            },
            "snapshots_total": total,
            "snapshots_stamped": stamped,
            "falsification_outcomes": n_outcomes,
            "outcomes_append_only": True,   # enforced by DB triggers (0006)
            "current_artifact_sha256": _M.frozen_sha256(),
        }


# ------------------------------------------------------------------- RM-4 ---


def _block_coverage(indicators: dict[str, Any], weights: dict[str, float]) -> dict[str, Any]:
    """Quality-weighted obtained coverage for one block from PERSISTED payloads,
    mirroring compute._coverage_gate semantics (fresh means stale is False)."""
    total = sum(weights.values())
    obtained = 0.0
    resolved = 0
    lost: dict[str, float] = {}
    for key, w in weights.items():
        p = indicators.get(key) or {}
        fresh = (not p.get("dropped", True)) and p.get("stale") is False
        q = float(p.get("quality", 0.0) or 0.0)
        if fresh:
            obtained += w * max(0.0, min(1.0, q))
            if p.get("sub_score") is not None:
                resolved += 1
        contribution = w * (max(0.0, min(1.0, q)) if fresh else 0.0)
        lost[key] = w - contribution
    frac = obtained / total if total else 0.0
    # EXACT values, never rounded (panel round-4 finding on this PR): these
    # feed the B2/B3/B4 threshold comparisons, and round(0.66666, 4) = 0.6667
    # would read a below-2/3 snapshot as available. Rounding is display-only
    # and none of these values are emitted verbatim in the report.
    return {"fraction": frac, "resolved_count": resolved, "lost_weight": lost}


def _snapshot_rows(session) -> list[Snapshot]:
    return list(session.execute(
        select(Snapshot).order_by(Snapshot.computed_at)).scalars())


def b_policy_report() -> dict[str, Any]:
    """B0-B5 candidate coverage policies over every persisted snapshot.

    Per the operator's required outputs: headline availability, degraded-block
    rates, band availability, override behavior, worst suppression drivers,
    longest continuous unavailable period, and the B4 cross-block masking
    check. Thresholds are the operator's enumerated candidates only."""
    two_thirds = 1.0 - _DROP
    policies = ["B0", "B1", "B2@0.50", "B3@2/3", "B4@0.50", "B4@2/3",
                "B5@k=2", "B5@k=3", "B5@k=4"]
    stats: dict[str, dict[str, Any]] = {
        p: {"available": 0, "unavailable": 0, "longest_gap_days": 0,
            "_gap_dates": set()}
        for p in policies}
    n = one_degraded = both_degraded = overrides = suppressed_bands = 0
    drivers: dict[str, int] = {}
    masking: dict[str, int] = {"B4@0.50": 0, "B4@2/3": 0}

    def _mark(p: str, available: bool, day) -> None:
        # The unavailable streak is measured in DISTINCT snapshot days, not
        # consecutive rows (panel round-6 finding: the 4-hourly recompute
        # persists ~6 rows/day, so a one-day outage read as a 6-"period" gap).
        s = stats[p]
        if available:
            s["available"] += 1
            s["_gap_dates"].clear()
        else:
            s["unavailable"] += 1
            s["_gap_dates"].add(day)
            s["longest_gap_days"] = max(s["longest_gap_days"], len(s["_gap_dates"]))

    with session_scope() as session:
        for snap in _snapshot_rows(session):
            n += 1
            cov_s = _block_coverage((snap.block_s or {}).get("indicators") or {}, _WS)
            cov_d = _block_coverage((snap.block_d or {}).get("indicators") or {}, _WD)
            ws, wd = cov_s["fraction"], cov_d["fraction"]
            deg_s, deg_d = ws < two_thirds, wd < two_thirds
            if deg_s and deg_d:
                both_degraded += 1
            elif deg_s or deg_d:
                one_degraded += 1
            if deg_s or deg_d:
                suppressed_bands += 1
                worst = max(list(cov_s["lost_weight"].items()) +
                            list(cov_d["lost_weight"].items()), key=lambda kv: kv[1])
                if worst[1] > 0:
                    drivers[worst[0]] = drivers.get(worst[0], 0) + 1
            if snap.override_fired:
                overrides += 1
            day = snap.computed_at.date()
            _mark("B0", True, day)
            _mark("B1", not (deg_s or deg_d), day)
            _mark("B2@0.50", min(ws, wd) >= 0.50, day)
            _mark("B3@2/3", min(ws, wd) >= two_thirds, day)
            for theta, key in ((0.50, "B4@0.50"), (two_thirds, "B4@2/3")):
                combined_ok = (ws + wd) / 2 >= theta
                _mark(key, combined_ok, day)
                if combined_ok and min(ws, wd) < theta:
                    masking[key] += 1     # a full block masking a sparse one
            for k in (2, 3, 4):
                _mark(f"B5@k={k}", min(cov_s["resolved_count"],
                                       cov_d["resolved_count"]) >= k, day)

    for s in stats.values():
        s.pop("_gap_dates", None)
        s["available_pct"] = round(100.0 * s["available"] / n, 1) if n else None
    return {
        "snapshots": n,
        "policies": stats,
        "one_block_degraded": one_degraded,
        "both_blocks_degraded": both_degraded,
        "bands_suppressed": suppressed_bands,
        "overrides_fired": overrides,
        "worst_suppression_drivers": dict(
            sorted(drivers.items(), key=lambda kv: -kv[1])),
        "b4_masking_events": masking,
        "note": ("candidate thresholds only (operator's enumerated set); "
                 "no policy or constant is recommended or pinned by this report"),
    }


# ------------------------------------------------------------------- RM-5 ---


def assemble_decision_packages() -> dict[str, Any]:
    """Collate the per-PIN evidence packages. Studies that need the production
    host's network/data (EDGAR PIT grid, ALFRED vintages, price seeds, Atom
    G1-vs-mp benchmarks) are explicitly PENDING_HOST — never silently omitted."""
    pending = {"status": "PENDING_HOST",
               "run_on": "production host (network + data access required)"}
    return {
        "generated_from": "persisted snapshots (read-only)",
        "B_coverage_floor": {
            "status": "EVIDENCE_ACCUMULATING",
            "report": b_policy_report(),
        },
        "DE_d3_ocf_quorum": {**pending,
                             "harness": "docs/harnesses/de_d3_pit_harness.py"},
        "F_ath_basis": {**pending,
                        "harness": "docs/harnesses/f_ath_continuity_harness.py"},
        "G_lppls_execution": {**pending,
                              "needs": "Atom runtime benchmark: G1 deterministic "
                                       "reference vs multiprocessing path"},
        "C_ndx_identity": {**pending,
                           "harness": "docs/harnesses/c_ndx_drift_harness.py"},
    }
