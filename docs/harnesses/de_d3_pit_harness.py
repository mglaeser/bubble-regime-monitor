"""PIN-D/E joint D3 harness — POINT-IN-TIME EDGAR replay (ANALYSIS ONLY).

Run on the production host (needs data.sec.gov). Read-only; prints ONE JSON
document. No production code path, mapping, or quorum is changed.

Point-in-time correctness: EDGAR companyfacts carries `end` AND `filed` per
duration fact, so for every historical evaluation date this harness uses ONLY
facts with filed <= evaluation_date — a true PIT vintage (no restated future
knowledge).

Grid evaluated at each quarterly evaluation date:
    OCF policies      D0_DROP, D1_DROP_AND_DEBIT_QUALITY, D2_DISTRESS_FLAG_ONLY,
                      D3_SATURATED_BASE, D4_DROP_ENTIRE_D3, D5_TWO_COMPONENT
    quorum            minimum usable issuers 1..5
    below-minimum     drop_and_renormalize | suppress_block_output
    aggregation order avg_raw_then_transform (production baseline)
                      vs transform_then_avg (bounded issuer stress first)

Per combination: D3 availability, D3 sub-score (gate OFF assumed: cap 0.30 —
the historical gate needs segment-revenue vintages, reported separately),
D-block coverage effect, leave-one-issuer-out sensitivity, and the audit flag
"distress made LESS alarming vs baseline".

Usage:  python docs/harnesses/de_d3_pit_harness.py [--start 2016-01-01]
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta

from app.indicators.d3_hyperscaler_fcf import CIKS, GATE_OFF_CAP
from app.sources import edgar

OCF_POLICIES = ("D0_DROP", "D1_DROP_AND_DEBIT_QUALITY", "D2_DISTRESS_FLAG_ONLY",
                "D3_SATURATED_BASE", "D4_DROP_ENTIRE_D3", "D5_TWO_COMPONENT")


def _pit_ttm(facts: dict, tags: list[str], as_of: date) -> float | None:
    """TTM sum of the newest four non-overlapping quarterly duration values
    with filed <= as_of (annual values used when quarters are unavailable)."""
    rows = []
    for tag in tags:
        for unit_rows in (facts.get("facts", {}).get("us-gaap", {})
                          .get(tag, {}).get("units", {}) or {}).values():
            for r in unit_rows:
                try:
                    filed = date.fromisoformat(r["filed"])
                    start = date.fromisoformat(r["start"])
                    end = date.fromisoformat(r["end"])
                except (KeyError, ValueError):
                    continue
                if filed <= as_of and end <= as_of:
                    rows.append((start, end, float(r["val"])))
        if rows:
            break
    if not rows:
        return None
    rows.sort(key=lambda r: r[1])
    quarters = [r for r in rows if 80 <= (r[1] - r[0]).days <= 100]
    picked: list[tuple[date, date, float]] = []
    for r in reversed(quarters):
        if all(r[1] <= p[0] or r[0] >= p[1] for p in picked):
            picked.append(r)
        if len(picked) == 4:
            break
    if len(picked) == 4 and (picked[0][1] - picked[3][0]).days <= 400:
        return sum(p[2] for p in picked)
    annuals = [r for r in rows if 350 <= (r[1] - r[0]).days <= 380]
    return annuals[-1][2] if annuals else None


def _issuer_states(all_facts: dict[str, dict], as_of: date) -> list[dict]:
    out = []
    for ticker, facts in all_facts.items():
        ocf = _pit_ttm(facts, edgar.OCF_TAGS, as_of)
        capex = _pit_ttm(facts, edgar.CAPEX_TAGS, as_of)
        if ocf is None or capex is None:
            continue
        out.append({"ticker": ticker, "ocf": ocf, "capex": capex,
                    "ratio": (capex / ocf) if ocf > 0 else None,
                    "distress": ocf <= 0.0})
    return out


def _base(r: float) -> float:
    return max(0.0, min(1.0, (r - 0.5) / 0.5))


def _score(issuers: list[dict], policy: str, order: str) -> dict:
    usable = [i for i in issuers if i["ratio"] is not None]
    distressed = [i for i in issuers if i["distress"]]
    n_reporting = len(issuers)
    if policy in ("D0_DROP", "D1_DROP_AND_DEBIT_QUALITY", "D2_DISTRESS_FLAG_ONLY"):
        pool = usable
    elif policy == "D3_SATURATED_BASE":
        pool = usable + [{"ratio": None, "stress": 1.0} for _ in distressed]
    elif policy == "D4_DROP_ENTIRE_D3":
        if distressed:
            return {"available": False, "reason": "nonpositive OCF present"}
        pool = usable
    else:  # D5_TWO_COMPONENT
        pool = usable
    if not pool:
        return {"available": False, "reason": "no usable issuers"}
    if order == "avg_raw_then_transform":
        ratios = [i["ratio"] for i in pool if i.get("ratio") is not None]
        stress_pad = [1.0] * (len(pool) - len(ratios))     # saturated members
        mean_r = sum(ratios) / len(ratios) if ratios else 0.0
        raw = _base(mean_r)
        if stress_pad:  # blend saturated members at the transformed layer
            raw = (raw * len(ratios) + sum(stress_pad)) / len(pool)
    else:               # transform_then_avg
        stresses = [(_base(i["ratio"]) if i.get("ratio") is not None else i.get("stress", 1.0))
                    for i in pool]
        raw = sum(stresses) / len(stresses)
    sub = min(raw, GATE_OFF_CAP)                            # gate OFF baseline
    result = {"available": True, "n_usable": len(usable), "n_distress": len(distressed),
              "n_reporting": n_reporting, "sub_score_gate_off": round(sub, 4),
              "raw_component": round(raw, 4)}
    if policy == "D1_DROP_AND_DEBIT_QUALITY":
        result["quality"] = round(len(usable) / len(CIKS), 2)
    if policy == "D2_DISTRESS_FLAG_ONLY":
        result["distress_flag"] = bool(distressed)
    if policy == "D5_TWO_COMPONENT":
        result["distress_component"] = round(len(distressed) / max(1, n_reporting), 4)
    return result


def main() -> None:
    start = date(2016, 1, 1)
    if "--start" in sys.argv:
        start = date.fromisoformat(sys.argv[sys.argv.index("--start") + 1])
    all_facts = {}
    for ticker, cik in CIKS.items():
        all_facts[ticker] = edgar._companyfacts(cik)
    evals = []
    d = start
    while d <= date.today():
        evals.append(d)
        d += timedelta(days=91)
    report: dict = {"evaluations": []}
    for as_of in evals:
        issuers = _issuer_states(all_facts, as_of)
        entry: dict = {"as_of": as_of.isoformat(),
                       "issuers": [{k: i[k] for k in ("ticker", "distress")} for i in issuers],
                       "grid": {}}
        for policy in OCF_POLICIES:
            for order in ("avg_raw_then_transform", "transform_then_avg"):
                for quorum in (1, 2, 3, 4, 5):
                    r = _score(issuers, policy, order)
                    if r.get("available") and r["n_usable"] < quorum:
                        r = {"available": False, "reason": f"below quorum {quorum}"}
                    entry["grid"][f"{policy}|{order}|q{quorum}"] = r
        # leave-one-issuer-out sensitivity for the production baseline
        loo = {}
        for skip in [i["ticker"] for i in issuers]:
            rest = [i for i in issuers if i["ticker"] != skip]
            loo[skip] = _score(rest, "D0_DROP", "avg_raw_then_transform")
        entry["leave_one_out_D0"] = loo
        report["evaluations"].append(entry)
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
