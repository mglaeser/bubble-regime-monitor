"""VIX term-structure adapter (V multiplier + fast-alarm inputs).

Source order: CBOE delayed quotes (>= 20 min delay) -> FRED VIXCLS/VIX3M
ratio. The CBOE pair is measured only while both indices are of one New York
session (session_of_pair); a run before the open is scored with the last
measured reading (app/services/compute.py:_v_reading). The vixcentral.com
scrape that led the chain until 2026-09 is gone: the site moved to
volchart.io and the page no longer carries the numbers.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.alerts.calendars import is_trading_day
from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult
from app.sources.fred import latest as fred_latest

CBOE_VIX_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
CBOE_VIX3M_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX3M.json"
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


NEW_YORK = ZoneInfo("America/New_York")
# CBOE publishes VIX from the early New York morning and VIX3M in the regular
# session only, so a run before the open divided a VIX of today by the VIX3M of
# the last close. The 222 recomputes logged from 2026-10-05 to 11-10
# (cboe_quote_raw), weekdays by New York time, each against the run before it:
#   21:00 to 02:00          VIX moved in 0 of 53, VIX3M in 0 of 53
#   05:00, 06:00 and 09:00  VIX in 33 of 34, VIX3M in 0 of 34
#   10:00                   both in 20 of 20
#   13:00 and 14:00         both in 27 of 27
# Ten of those 33 pairs had another state than the last pair of one session,
# and 21.06 / 20.85 = 1.0101 sent "Terminstruktur in Backwardation" on
# 2026-11-10 14:05 UTC (the pair of the evening before: 0.9554; both live at
# 18:00 UTC: 0.9650).
# The session opens at 09:30 and the quotes are delayed. The recomputes meet
# New York at 09:00 or at 10:00 and at no time between, so 10:00 is the
# earliest hour the log shows both indices published.
BOTH_PUBLISHED_FROM = time(10, 0)


def session_of_pair(now: datetime) -> date | None:
    """The New York session both CBOE quotes belong to at `now`, or None.

    From 10:00 New York to midnight of an NYSE trading day both quotes are of
    that day's session, live or closed. At any other hour VIX may be of a later
    morning than VIX3M, and the pair is not measured."""
    local = now.astimezone(NEW_YORK)
    if is_trading_day(local.date()) and local.time() >= BOTH_PUBLISHED_FROM:
        return local.date()
    return None


def term_structure_ratio(session: date | None = None) -> SourceResult:
    """VIX / VIX3M ratio, walking the fallback chain. `session` is the New York
    session the caller measures in (session_of_pair); it dates the CBOE pair.
    The FRED pair keeps FRED's own observation date."""
    errors: list[str] = []
    try:
        vix = _cboe_last(CBOE_VIX_URL, "cboe_vix")
        vix3m = _cboe_last(CBOE_VIX3M_URL, "cboe_vix3m")
        return SourceResult(vix / vix3m, Provenance(
            source="cboe_delayed", note=">=20-min delayed",
            as_of=session.isoformat() if session else None))
    except Exception as e:
        errors.append(f"cboe: {e}")
    try:
        vix = fred_latest("VIXCLS")
        vix3m = fred_latest("VIX3M")
        # v3.7.6/X-01: VIX and VIX3M are two independent daily series — divide
        # them ONLY on an IDENTICAL observation date. Any mismatch (even 1 day)
        # means the term-structure ratio mixes two different sessions; reject
        # rather than mislead (a 3-day tolerance still divided mismatched dates).
        d1, d2 = vix.provenance.as_of, vix3m.provenance.as_of
        if d1 != d2:
            raise SourceError(f"VIX ({d1}) and VIX3M ({d2}) not on a common date")
        return SourceResult(float(vix.value) / float(vix3m.value),
                            Provenance(source="fred_vix_ratio", fallback_used=True,
                                       as_of=d1, note=f"VIX {d1} / VIX3M {d2}"))
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
