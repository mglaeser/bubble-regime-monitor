"""VIX term-structure adapter (V multiplier + fast-alarm inputs).

Source order: vixcentral.com scrape (primary, anonymously accessible July
2026) -> CBOE delayed CSV (>= 20 min delay) -> FRED VIXCLS/VIX3M ratio.
"""

from __future__ import annotations

import re

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult
from app.sources.fred import latest as fred_latest

VIXCENTRAL_URL = "https://vixcentral.com"
CBOE_VIX_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
CBOE_VIX3M_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX3M.json"
CBOE_SKEW_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_SKEW.json"


def _ratio_from_vixcentral(html: str) -> float:
    m = re.search(r"VIX[:\s]*([\d.]+).{0,400}?VIX3M[:\s]*([\d.]+)", html, re.S)
    if not m:
        raise SourceError("vixcentral: could not parse VIX/VIX3M")
    vix, vix3m = float(m.group(1)), float(m.group(2))
    if vix3m <= 0:
        raise SourceError("vixcentral: bad VIX3M")
    return vix / vix3m


def _cboe_last(url: str, source: str) -> float:
    data = fetch(source, url).json()
    price = data.get("data", {}).get("current_price") or data.get("data", {}).get("last")
    if not price:
        raise SourceError(f"{source}: no price in payload")
    return float(price)


def term_structure_ratio() -> SourceResult:
    """VIX / VIX3M ratio, walking the fallback chain."""
    errors: list[str] = []
    try:
        return SourceResult(_ratio_from_vixcentral(fetch("vixcentral", VIXCENTRAL_URL).text),
                            Provenance(source="vixcentral"))
    except Exception as e:
        errors.append(f"vixcentral: {e}")
    try:
        vix = _cboe_last(CBOE_VIX_URL, "cboe_vix")
        vix3m = _cboe_last(CBOE_VIX3M_URL, "cboe_vix3m")
        return SourceResult(vix / vix3m, Provenance(source="cboe_delayed", fallback_used=True,
                                                    note=">=20-min delayed"))
    except Exception as e:
        errors.append(f"cboe: {e}")
    try:
        vix = fred_latest("VIXCLS")
        vix3m = fred_latest("VIX3M")
        return SourceResult(float(vix.value) / float(vix3m.value),
                            Provenance(source="fred_vix_ratio", fallback_used=True))
    except Exception as e:
        errors.append(f"fred: {e}")
    raise SourceError("VIX term structure: all sources failed: " + "; ".join(errors))


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
