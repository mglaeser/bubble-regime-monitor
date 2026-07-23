"""PIN-C drift harness (ANALYSIS ONLY — run on the production host).

Builds the four candidate NDX source-identity policies and reports the drift
between them. Read-only: no DB writes, no cache mutation, no scored path is
altered. Prints ONE JSON document to stdout for operator review.

    C0_CURRENT_CHAIN   whatever get_daily_closes("NDX") serves today
    C1_QQQ_ADJUSTED    Tiingo QQQ adjClose
    C2_QQQ_UNADJUSTED  Twelve Data QQQ close (free tier is unadjusted)
    C3_NATIVE_NDX      native index if any configured path serves it
                       (Twelve Data Grow NDX / yfinance ^NDX / stooq ^ndx)

Per-policy-pair stats over overlapping dates: price-level correlation, daily &
monthly log-return correlation and mean|diff|, max tracking divergence (of
normalized levels). Per-policy S4 (GSADF via the service's R bridge, if
Rscript+exuber are installed) and D4 (lppls confidence via d4_lppls) runs.

Usage:  python docs/harnesses/c_ndx_drift_harness.py [--skip-gsadf] [--skip-lppls]
"""

from __future__ import annotations

import json
import math
import sys

from app.indicators import d4_lppls
from app.sources import prices


def _pairs_to_map(rows: list[tuple[str, float]]) -> dict[str, float]:
    return dict(rows)


def _collect_policies() -> dict[str, list[tuple[str, float]]]:
    out: dict[str, list[tuple[str, float]]] = {}
    try:
        r = prices.get_daily_closes("NDX")
        out["C0_CURRENT_CHAIN"] = list(r.value)
        out["_c0_provenance"] = {"source": r.provenance.source,  # type: ignore[assignment]
                                 "as_of": r.provenance.as_of,
                                 "note": r.provenance.note}
    except Exception as exc:
        out["_c0_error"] = str(exc)[:200]  # type: ignore[assignment]
    try:
        rows, vendor, proxy = prices.fetch_tiingo("QQQ")
        out["C1_QQQ_ADJUSTED"] = rows
    except Exception as exc:
        out["_c1_error"] = str(exc)[:200]  # type: ignore[assignment]
    try:
        rows = prices.fetch_twelvedata_series("QQQ", outputsize=5000)
        out["C2_QQQ_UNADJUSTED"] = rows
    except Exception as exc:
        out["_c2_error"] = str(exc)[:200]  # type: ignore[assignment]
    for label, fn in (("twelvedata_native", lambda: prices.fetch_twelvedata("NDX")),
                      ("yfinance_native", lambda: prices.fetch_yfinance("NDX"))):
        try:
            rows, vendor, proxy = fn()
            if not proxy:                       # only accept a genuinely native series
                out["C3_NATIVE_NDX"] = rows
                out["_c3_vendor"] = {"path": label, "vendor": vendor}  # type: ignore[assignment]
                break
        except Exception:  # noqa: S112 -- best-effort native-path discovery; absence is a valid result
            continue
    return out


def _stats(a: list[tuple[str, float]], b: list[tuple[str, float]]) -> dict:
    ma, mb = _pairs_to_map(a), _pairs_to_map(b)
    common = sorted(set(ma) & set(mb))
    if len(common) < 60:
        return {"overlap_days": len(common), "note": "insufficient overlap"}
    xa = [ma[d] for d in common]
    xb = [mb[d] for d in common]

    def corr(u: list[float], v: list[float]) -> float:
        n = len(u)
        mu, mv = sum(u) / n, sum(v) / n
        cov = sum((ui - mu) * (vi - mv) for ui, vi in zip(u, v, strict=True))
        su = math.sqrt(sum((ui - mu) ** 2 for ui in u))
        sv = math.sqrt(sum((vi - mv) ** 2 for vi in v))
        return cov / (su * sv) if su and sv else float("nan")

    ra = [math.log(xa[i] / xa[i - 1]) for i in range(1, len(xa))]
    rb = [math.log(xb[i] / xb[i - 1]) for i in range(1, len(xb))]
    na = [v / xa[0] for v in xa]
    nb = [v / xb[0] for v in xb]
    monthly_a, monthly_b, seen = [], [], set()
    for i, d in enumerate(common):
        if d[:7] not in seen:
            seen.add(d[:7])
        if i + 1 == len(common) or common[i + 1][:7] != d[:7]:
            monthly_a.append(xa[i])
            monthly_b.append(xb[i])
    mra = [math.log(monthly_a[i] / monthly_a[i - 1]) for i in range(1, len(monthly_a))]
    mrb = [math.log(monthly_b[i] / monthly_b[i - 1]) for i in range(1, len(monthly_b))]
    return {
        "overlap_days": len(common),
        "level_correlation": round(corr(xa, xb), 6),
        "daily_return_correlation": round(corr(ra, rb), 6),
        "daily_return_mean_abs_diff": round(sum(abs(u - v) for u, v in zip(ra, rb, strict=True)) / len(ra), 8),
        "monthly_return_correlation": round(corr(mra, mrb), 6) if len(mra) > 11 else None,
        "max_tracking_divergence_pct": round(max(abs(u - v) / v for u, v in zip(na, nb, strict=True)) * 100, 4),
    }


def main() -> None:
    skip_gsadf = "--skip-gsadf" in sys.argv
    skip_lppls = "--skip-lppls" in sys.argv
    policies = _collect_policies()
    series = {k: v for k, v in policies.items() if not k.startswith("_")}
    report: dict = {"provenance": {k: v for k, v in policies.items() if k.startswith("_")},
                    "pairwise": {}, "s4": {}, "d4": {}}
    keys = sorted(series)
    for i, ka in enumerate(keys):
        for kb in keys[i + 1:]:
            report["pairwise"][f"{ka}|{kb}"] = _stats(series[ka], series[kb])
    if not skip_gsadf:
        from app.engine import gsadf_runner
        from app.services import legs
        for k, rows in series.items():
            try:
                monthly = legs.monthly_closes(rows)
                out = gsadf_runner.run([math.log(v) for v in monthly[-360:]])
                report["s4"][k] = (None if out is None else
                                   {"gsadf": out.gsadf, "cv90": out.cv90, "cv95": out.cv95,
                                    "explosive_90": out.gsadf > out.cv90,
                                    "explosive_95": out.gsadf > out.cv95})
            except Exception as exc:
                report["s4"][k] = {"error": str(exc)[:200]}
    if not skip_lppls:
        for k, rows in series.items():
            try:
                closes = [c for _, c in rows][-d4_lppls.LPPLS_WINDOW_MAX:]
                report["d4"][k] = d4_lppls.compute_confidence(closes)
            except Exception as exc:
                report["d4"][k] = {"error": str(exc)[:200]}
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
