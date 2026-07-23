"""PIN-F ATH source/continuity harness (ANALYSIS ONLY — run on the production host).

Compares the three ATH-basis candidates and audits seed continuity. Read-only;
the production red flag is NOT touched. Prints ONE JSON document to stdout.

    F0  current basis: max of the ADJUSTED SPY closes in the provider cache
        window (<= 900 rows) — what compute.py:478 actually evaluates today
    F1  native S&P 500 price index with a long-history seed (stooq ^spx when
        STOOQ_ENABLED=true; otherwise supply a CSV via --spx-csv PATH with
        rows date,close)
    F2  unadjusted SPY long history (Twelve Data close / Alpha Vantage
        TIME_SERIES_DAILY are unadjusted on the tiers this service uses)

For each basis: the running watermark, the daily within_2pct_of_ath series,
pairwise disagreement dates, and gap/staleness audit. The within-flag feeds the
breadth<50-near-ATH red flag, so disagreement dates are exactly the dates the
red-flag INPUT would differ; combine with historical breadth to get red-flag
count and >=3-of-4 override differences.

Usage:  python docs/harnesses/f_ath_continuity_harness.py [--spx-csv PATH]
"""

from __future__ import annotations

import csv
import json
import sys

from app.sources import prices

NEAR_ATH_FRAC = 0.98   # mirrors red_flags.index_near_ath_frac (read-only copy for analysis)


def _within_series(rows: list[tuple[str, float]]) -> tuple[dict[str, bool], dict]:
    """Running-watermark within-2% flags + watermark audit for one basis."""
    flags: dict[str, bool] = {}
    watermark, wm_date = float("-inf"), None
    for d, c in rows:
        if c > watermark:
            watermark, wm_date = c, d
        flags[d] = c >= NEAR_ATH_FRAC * watermark
    gaps = sum(1 for i in range(1, len(rows))
               if (_ord(rows[i][0]) - _ord(rows[i - 1][0])) > 7)
    return flags, {"ath_value": watermark, "ath_date": wm_date,
                   "history_start": rows[0][0] if rows else None,
                   "history_end": rows[-1][0] if rows else None,
                   "n_obs": len(rows), "gaps_gt_7d": gaps}


def _ord(iso: str) -> int:
    from datetime import date
    return date.fromisoformat(iso).toordinal()


def _f0_current() -> list[tuple[str, float]]:
    r = prices.get_daily_closes("SPY")           # adjusted, provider-window (<=900 rows)
    return list(r.value)


def _f1_native_spx(csv_path: str | None) -> list[tuple[str, float]]:
    if csv_path:
        with open(csv_path) as fh:
            return sorted((row["date"][:10], float(row["close"]))
                          for row in csv.DictReader(fh))
    from app.sources import stooq
    return sorted(stooq.fetch_with_pow("^spx"))   # requires STOOQ_ENABLED path working


def _f2_spy_unadjusted() -> list[tuple[str, float]]:
    return prices.fetch_twelvedata_series("SPY", outputsize=5000)   # unadjusted close


def main() -> None:
    csv_path = None
    if "--spx-csv" in sys.argv:
        csv_path = sys.argv[sys.argv.index("--spx-csv") + 1]
    bases: dict[str, list[tuple[str, float]]] = {}
    errors: dict[str, str] = {}
    for name, fn in (("F0_adjusted_spy_window", _f0_current),
                     ("F1_native_spx_long", lambda: _f1_native_spx(csv_path)),
                     ("F2_unadjusted_spy_long", _f2_spy_unadjusted)):
        try:
            bases[name] = fn()
        except Exception as exc:
            errors[name] = str(exc)[:200]

    report: dict = {"errors": errors, "watermarks": {}, "flag_disagreements": {}}
    flag_maps: dict[str, dict[str, bool]] = {}
    for name, rows in bases.items():
        flags, audit = _within_series(rows)
        flag_maps[name] = flags
        report["watermarks"][name] = audit

    names = sorted(flag_maps)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            common = sorted(set(flag_maps[a]) & set(flag_maps[b]))
            dis = [d for d in common if flag_maps[a][d] != flag_maps[b][d]]
            report["flag_disagreements"][f"{a}|{b}"] = {
                "overlap_days": len(common),
                "disagreement_days": len(dis),
                "disagreement_rate": round(len(dis) / len(common), 4) if common else None,
                "sample_dates": dis[:25],
            }
    # Safety-rule audit hooks (operator's watermark rules)
    report["safety_notes"] = {
        "no_state_before_seed": "F1/F2 watermarks above start at history_start; "
                                "a production watermark must refuse ATH state until the "
                                "seed range is confirmed complete.",
        "identity_mixing": "each basis above is computed from a single identity; "
                           "the production watermark must persist the identity and refuse "
                           "cross-identity inheritance.",
    }
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
