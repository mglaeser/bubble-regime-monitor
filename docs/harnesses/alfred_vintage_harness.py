"""RM-3 — ALFRED point-in-time vintage harness (ANALYSIS ONLY, production host).

FRED's ALFRED layer serves ARCHIVED VINTAGES: passing realtime_start/
realtime_end returns each observation as it was published AT that date, i.e. a
true point-in-time series with no look-ahead. This is the evidence source for
the S5 vintage-policy PIN (S5_EBP_VINTAGE_POLICY / S5_HISTORICAL_REVISION_
POLICY) and for PIT-safe B/F backtests.

Read-only; prints ONE JSON document. Requires FRED_API_KEY (production host).

Usage:
  python docs/harnesses/alfred_vintage_harness.py --series BAA --as-of 2024-06-30
  python docs/harnesses/alfred_vintage_harness.py --series BAMLH0A0HYM2 --vintages
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request

from app.config import get_settings

BASE = "https://api.stlouisfed.org/fred"


def _get(path: str, **params: str) -> dict:
    key = get_settings().fred_api_key
    if not key:
        print("FRED_API_KEY not configured — run on the production host", file=sys.stderr)
        sys.exit(2)
    qs = urllib.parse.urlencode({"api_key": key, "file_type": "json", **params})
    with urllib.request.urlopen(f"{BASE}/{path}?{qs}", timeout=60) as resp:  # noqa: S310 -- fixed https base
        return json.loads(resp.read().decode())


def vintage_dates(series: str) -> list[str]:
    data = _get("series/vintagedates", series_id=series)
    return data.get("vintage_dates", [])


def pit_observations(series: str, as_of: str) -> list[dict]:
    """Observations exactly as published on `as_of` (realtime window pinned)."""
    data = _get("series/observations", series_id=series,
                realtime_start=as_of, realtime_end=as_of)
    return [{"date": o["date"], "value": o["value"]}
            for o in data.get("observations", []) if o.get("value") not in (".", None)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", required=True, help="e.g. BAA, DGS10, BAMLH0A0HYM2")
    ap.add_argument("--as-of", help="PIT vintage date YYYY-MM-DD")
    ap.add_argument("--vintages", action="store_true", help="list archived vintage dates")
    args = ap.parse_args()
    out: dict = {"series": args.series}
    if args.vintages:
        out["vintage_dates"] = vintage_dates(args.series)
    if args.as_of:
        obs = pit_observations(args.series, args.as_of)
        out["as_of"] = args.as_of
        out["n_observations"] = len(obs)
        out["observations"] = obs
    if not args.vintages and not args.as_of:
        print("pass --as-of and/or --vintages", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
