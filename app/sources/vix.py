"""VIX term-structure adapter (V multiplier + fast-alarm inputs).

The ratio is FRED's VIXCLS / VXVCLS, the daily closes of VIX and VIX3M. The
vixcentral.com scrape that led the chain until 2026-09 is gone: the site moved
to volchart.io and the page no longer carries the numbers.
"""

from __future__ import annotations

import math
from datetime import date

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult
from app.sources.fred import latest as fred_latest
from app.sources.fred import observations as fred_observations

CBOE_VIX_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
CBOE_SKEW_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_SKEW.json"


def _cboe_last(url: str, source: str) -> float:
    from app.logging_conf import get_logger

    data = fetch(source, url).json()
    inner = data.get("data", {})
    price = inner.get("current_price") or inner.get("last")
    get_logger(__name__).info("cboe_quote_raw", source=source,
                              current_price=inner.get("current_price"),
                              last=inner.get("last"), symbol=inner.get("symbol"))
    if not price:
        raise SourceError(f"{source}: no price in payload")
    return float(price)


def term_structure_ratio() -> SourceResult:
    """VIX / VIX3M: FRED's VIXCLS and VXVCLS closes of one ISO date, the newest
    with a positive close in both series' last five observations, whose ratio is
    a finite positive number, dated by that date; anything else is a SourceError.

    The pair of CBOE delayed quotes asked first is removed (a decision the owner
    delegated on 2026-11-25): CBOE refused it on every run from 2026-11-20, FRED's
    pair has been scored since 2026-11-23, and the day CBOE answered again the pair
    would be back, undated, and before New York's open a VIX of the morning over the
    VIX3M of the last close (21.06 / 20.85 = 1.0101 at 2026-11-10 14:00 UTC, where
    the closes of 11-09 give 0.9554)."""
    try:
        vix = dict(fred_observations("VIXCLS", limit=5))
        # FRED publishes CBOE's VIX3M as series VXVCLS (fred.stlouisfed.org/series/VXVCLS).
        # It has no series "VIX3M": that id was answered 400 Bad Request on all 23 runs that
        # reached this call (2026-11-20 02:00 to 11-23 18:00 UTC, CBOE answering 403),
        # while VIXCLS succeeded on the same key every time.
        vix3m = dict(fred_observations("VXVCLS", limit=5))
        # v3.7.6/X-01: VIX and VIX3M are two independent daily series — divide
        # them ONLY on an IDENTICAL observation date. Any mismatch (even 1 day)
        # means the term-structure ratio mixes two different sessions; reject
        # rather than mislead (a 3-day tolerance still divided mismatched dates).
        # The date is the newest both hold: each series' own newest close can be
        # a session ahead of the other's.
        common = [d for d in sorted(vix.keys() & vix3m.keys()) if vix[d] > 0 and vix3m[d] > 0]
        if not common:
            raise SourceError(f"VIX ({max(vix)}) and VIX3M ({max(vix3m)}) not on a common date")
        d1 = d2 = common[-1]
        ratio = vix[d1] / vix3m[d2]
        if not math.isfinite(ratio) or ratio <= 0:
            raise SourceError(f"VIX {vix[d1]} / VIX3M {vix3m[d2]} is no positive ratio")
        return SourceResult(ratio, Provenance(source="fred_vix_ratio",
                                              as_of=date.fromisoformat(d1).isoformat(),
                                              note=f"VIX {d1} / VIX3M {d2}"))
    except Exception as e:
        raise SourceError(f"VIX term structure: fred: {e}") from e


def vix_level() -> SourceResult:
    """Spot VIX (FRED VIXCLS primary for reproducibility; CBOE fallback)."""
    try:
        return fred_latest("VIXCLS")
    except Exception:
        return SourceResult(_cboe_last(CBOE_VIX_URL, "cboe_vix"),
                            Provenance(source="cboe_vix", fallback_used=True))


def skew_level() -> SourceResult:
    """CBOE SKEW index level. COINCIDENT CONTEXT ONLY."""
    return SourceResult(_cboe_last(CBOE_SKEW_URL, "cboe_skew"), Provenance(source="cboe_skew"))
