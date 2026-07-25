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
  * RM-2  s5_tier_sufficiency()   qualifying-day counts per S5 source tier
                                  against the >=60-trading-day activation gate
  * RM-4  b_policy_report()       B0-B5 coverage policies over history
          s5_dual_report()        positional vs calendar S5, incl. the
                                  hypothetical headline delta (candidate sub
                                  swapped into the recorded sub-score set)
  * RM-5  assemble_decision_packages()  per-PIN evidence packages; host-
                                  dependent studies explicitly PENDING
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from app import methodology as _M
from app.db import session_scope
from app.engine.aggregate import RedFlags, deterministic_score
from app.logging_conf import get_logger
from app.models import FalsificationOutcome, Snapshot

log = get_logger(__name__)

# The S5 activation gate's day requirement (operator-set in the milestone:
# ">= 60 trading days AND adequate observations across all three source
# tiers"; per-tier adequacy is deliberately NOT pinned here).
S5_GATE_TRADING_DAYS = 60

_S5_TIERS = ("fed_ebp", "fred_BAA_DGS10", "fred_BAMLH0A0HYM2")


def _easter_sunday(year: int):
    """Gregorian Easter (Anonymous/Meeus computus) — pure arithmetic."""
    from datetime import date as _date

    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 -- canonical computus variable names
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return _date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int):
    from datetime import date as _date
    from datetime import timedelta as _td

    d = _date(year, month, 1)
    d += _td(days=(weekday - d.weekday()) % 7)
    return d + _td(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int):
    import calendar as _cal
    from datetime import date as _date
    from datetime import timedelta as _td

    d = _date(year, month, _cal.monthrange(year, month)[1])
    return d - _td(days=(d.weekday() - weekday) % 7)


def us_market_holidays(year: int) -> set:
    """NYSE full-day holidays (public calendar; pure date arithmetic).
    Observance: Saturday holidays are observed the preceding Friday EXCEPT
    New Year's (NYSE stays open at year-end); Sunday holidays the following
    Monday. One-off special closures (e.g. days of mourning) cannot be
    derived and remain a documented residual."""
    from datetime import date as _date
    from datetime import timedelta as _td

    def observed(d, new_years: bool = False):
        if d.weekday() == 5:                       # Saturday
            return None if new_years else d - _td(days=1)
        if d.weekday() == 6:                       # Sunday
            return d + _td(days=1)
        return d

    days = {
        observed(_date(year, 1, 1), new_years=True),
        _nth_weekday(year, 1, 0, 3),               # MLK: 3rd Monday of Jan
        _nth_weekday(year, 2, 0, 3),               # Washington's Birthday
        _easter_sunday(year) - _td(days=2),        # Good Friday
        _last_weekday(year, 5, 0),                 # Memorial Day
        observed(_date(year, 6, 19)),              # Juneteenth
        observed(_date(year, 7, 4)),               # Independence Day
        _nth_weekday(year, 9, 0, 1),               # Labor Day
        _nth_weekday(year, 11, 3, 4),              # Thanksgiving: 4th Thursday
        observed(_date(year, 12, 25)),             # Christmas
    }
    days.discard(None)
    return days


def is_trading_day(d) -> bool:
    """Mon-Fri and not an NYSE full-day holiday (panel round-2 finding on this
    PR: plain weekdays let holiday snapshots advance the activation gate)."""
    return d.weekday() < 5 and d not in us_market_holidays(d.year)

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


# ------------------------------------------------------------------- RM-2 ---


def _s5_payload(snap: Snapshot) -> dict | None:
    return ((snap.block_s or {}).get("indicators") or {}).get("s5")


def s5_tier_sufficiency() -> dict[str, Any]:
    """Distinct TRADING days (Mon-Fri minus NYSE holidays) of persisted snapshots, overall and per
    S5 source tier, against the >=60-day activation window. Per-tier adequacy
    thresholds are an operator decision and deliberately not asserted."""
    snapshot_weekdays: set = set()      # trading days with any snapshot (holidays excluded)
    s5_days: set = set()
    days_by_tier: dict[str, set] = {t: set() for t in _S5_TIERS}
    dual_days: set = set()
    with session_scope() as session:
        for snap in session.execute(select(Snapshot)).scalars():
            d = snap.computed_at.date()
            if not is_trading_day(d):
                continue
            snapshot_weekdays.add(d)
            s5 = _s5_payload(snap)
            if not s5 or s5.get("sub_score") is None:
                continue
            s5_days.add(d)
            src = s5.get("data_source")
            if src in days_by_tier:
                days_by_tier[src].add(d)
            if isinstance(s5.get("s5_dual_report"), dict):
                dual_days.add(d)
    # The GATE counts only days on which the dual comparison actually ran
    # (panel finding on this PR: counting every weekday snapshot let 60
    # S5-less days read as gate progress). Tier adequacy is NEVER auto-
    # satisfied here — nonempty is reported, adequacy is the operator's call.
    return {
        "gate_trading_days": S5_GATE_TRADING_DAYS,
        "gate_day_count": len(dual_days),
        "days_remaining_to_gate": max(0, S5_GATE_TRADING_DAYS - len(dual_days)),
        "snapshot_weekdays_observed": len(snapshot_weekdays),
        "s5_observation_days": len(s5_days),
        "days_with_dual_report": len(dual_days),
        "days_by_tier": {t: len(s) for t, s in days_by_tier.items()},
        "all_tiers_observed": all(days_by_tier[t] for t in _S5_TIERS),
        "note": ("gate counts DUAL-REPORT days on NYSE trading days only "
                 "(weekends + full-day exchange holidays excluded; one-off "
                 "special closures are a documented residual); per-tier "
                 "adequacy is an operator decision (not pinned) — 60 days of "
                 "EBP-only success does not validate fallback behavior"),
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
        lost[key] = round(w - contribution, 6)
    frac = obtained / total if total else 0.0
    return {"fraction": round(frac, 4), "resolved_count": resolved, "lost_weight": lost}


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
    stats = {p: {"available": 0, "unavailable": 0, "longest_gap": 0, "_gap": 0}
             for p in policies}
    n = one_degraded = both_degraded = overrides = suppressed_bands = 0
    drivers: dict[str, int] = {}
    masking: dict[str, int] = {"B4@0.50": 0, "B4@2/3": 0}

    def _mark(p: str, available: bool) -> None:
        s = stats[p]
        if available:
            s["available"] += 1
            s["_gap"] = 0
        else:
            s["unavailable"] += 1
            s["_gap"] += 1
            s["longest_gap"] = max(s["longest_gap"], s["_gap"])

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
            _mark("B0", True)
            _mark("B1", not (deg_s or deg_d))
            _mark("B2@0.50", min(ws, wd) >= 0.50)
            _mark("B3@2/3", min(ws, wd) >= two_thirds)
            for theta, key in ((0.50, "B4@0.50"), (two_thirds, "B4@2/3")):
                combined_ok = (ws + wd) / 2 >= theta
                _mark(key, combined_ok)
                if combined_ok and min(ws, wd) < theta:
                    masking[key] += 1     # a full block masking a sparse one
            for k in (2, 3, 4):
                _mark(f"B5@k={k}", min(cov_s["resolved_count"],
                                       cov_d["resolved_count"]) >= k)

    for s in stats.values():
        s.pop("_gap", None)
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


def _flags_from_detail(detail: dict | None) -> RedFlags:
    d = detail or {}
    return RedFlags(
        gsadf_explosive_noncontested=bool(d.get("gsadf_explosive_noncontested")),
        semi_runup_ge_150pp=bool(d.get("semi_runup_ge_150pp")),
        hy_oas_widen_gt_100bps=bool(d.get("hy_oas_widen_gt_100bps")),
        breadth_lt_50_near_ath=bool(d.get("breadth_lt_50_near_ath")),
    )


def s5_dual_report() -> dict[str, Any]:
    """Positional (production) vs calendar-candidate S5 over every snapshot
    carrying the PIN-H shadow dual report, INCLUDING the hypothetical
    deterministic-headline delta: the candidate sub-score is swapped into the
    RECORDED sub-score set and the frozen deterministic pipeline re-run on the
    side. Read-only; the shadow stays included_in_score=false everywhere."""
    rows: list[dict[str, Any]] = []
    statuses: dict[str, int] = {}
    tiers: dict[str, int] = {}
    with session_scope() as session:
        for snap in _snapshot_rows(session):
            s5 = _s5_payload(snap) or {}
            dual = s5.get("s5_dual_report")
            if not isinstance(dual, dict):
                continue
            cand = dual.get("candidate_v4") or {}
            status = cand.get("selection_status") or "UNKNOWN"
            statuses[status] = statuses.get(status, 0) + 1
            tier = cand.get("source_tier") or "unknown"
            tiers[tier] = tiers.get(tier, 0) + 1
            prod_sub = (dual.get("production") or {}).get("sub_score")
            cand_sub = cand.get("sub_score")
            entry: dict[str, Any] = {
                "computed_at": snap.computed_at.isoformat(),
                "tier": tier, "status": status,
                "production_sub": prod_sub, "candidate_sub": cand_sub,
                "sub_delta": None, "headline_delta": None,
            }
            if prod_sub is not None and cand_sub is not None:
                entry["sub_delta"] = round(cand_sub - prod_sub, 6)
                sub_s = {k: v.get("sub_score") for k, v in
                         ((snap.block_s or {}).get("indicators") or {}).items()
                         if v.get("sub_score") is not None}
                sub_d = {k: v.get("sub_score") for k, v in
                         ((snap.block_d or {}).get("indicators") or {}).items()
                         if v.get("sub_score") is not None}
                if "s5" in sub_s and sub_d:
                    flags = _flags_from_detail(snap.red_flag_detail)
                    base = deterministic_score(dict(sub_s), dict(sub_d),
                                               snap.v_multiplier, flags, _WS, _WD).score
                    swapped = dict(sub_s)
                    swapped["s5"] = cand_sub
                    cand_score = deterministic_score(swapped, dict(sub_d),
                                                     snap.v_multiplier, flags,
                                                     _WS, _WD).score
                    entry["headline_delta"] = round(cand_score - base, 4)
            rows.append(entry)

    deltas = [r["sub_delta"] for r in rows if r["sub_delta"] is not None]
    hdeltas = [r["headline_delta"] for r in rows if r["headline_delta"] is not None]

    def _agg(xs: list[float]) -> dict[str, float] | None:
        if not xs:
            return None
        return {"n": len(xs), "mean": round(sum(xs) / len(xs), 6),
                "min": round(min(xs), 6), "max": round(max(xs), 6),
                "mean_abs": round(sum(abs(x) for x in xs) / len(xs), 6)}

    return {
        "snapshots_with_dual_report": len(rows),
        "selection_statuses": statuses,
        "source_tiers": tiers,
        "sub_score_delta": _agg(deltas),
        "headline_delta": _agg(hdeltas),
        "series": rows,
        "note": ("hypothetical side-computation over recorded sub-scores; the "
                 "candidate remains included_in_score=false in production and "
                 "its parameters remain unapproved operator PINs"),
    }


# ------------------------------------------------------------------- RM-5 ---


def assemble_decision_packages() -> dict[str, Any]:
    """Collate the per-PIN evidence packages. Studies that need the production
    host's network/data (EDGAR PIT grid, ALFRED vintages, price seeds, Atom
    G1-vs-mp benchmarks) are explicitly PENDING_HOST — never silently omitted."""
    sufficiency = s5_tier_sufficiency()
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
        "H_s5_calendar": {
            # Never a mechanical GATE_MET (panel finding): the day condition is
            # computable, tier ADEQUACY is an operator judgment by design.
            "status": ("DAY_GATE_MET_TIER_ADEQUACY_OPERATOR_JUDGMENT"
                       if sufficiency["gate_day_count"] >= S5_GATE_TRADING_DAYS
                       else "EVIDENCE_ACCUMULATING"),
            "sufficiency": sufficiency,
            "dual_report": s5_dual_report(),
            "activation_requires": [
                "operator approval of all six S5 constants (incl. the artifact "
                "<PIN> S5_EMPIRICAL_CDF_TIE_METHOD)",
                "vintage policy decision (see ALFRED harness)",
                "new methodology version + falsification clock + regenerated "
                "goldens + dual reporting + rollback plan",
            ],
        },
        "C_ndx_identity": {**pending,
                           "harness": "docs/harnesses/c_ndx_drift_harness.py"},
    }
