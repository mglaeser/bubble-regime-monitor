"""Real (CPI-deflated) monthly index levels from FRED, for the S4 GSADF input.

WHY THIS EXISTS. S4 runs the Phillips-Shi-Yu (2015) right-tailed unit-root test
for explosive dynamics. The papers the service cites run it on a REAL price
index; the service runs it on NOMINAL, DIVIDEND-ADJUSTED QQQ closes:

  * Chen et al. (2026, arXiv:2604.25826) sec 5.6 use "the log real NASDAQ
    Composite Index, obtained from FRED and deflated by the CPI".
  * PSY (2015) apply it to the real (CPI-deflated) S&P 500 and its
    price-dividend ratio.

Two mismatches follow. Inflation adds growth the asset did not have, and a
dividend-reinvested series adds a second drift a price index does not carry.
A test that hunts ACCELERATION is not fooled much by either -- both are close to
smooth -- which is why this module exists to MEASURE the difference rather than
to assume it.

CORRECTS A DOCUMENTED FALSEHOOD. README.md:31 states "Neither free tier serves
raw stock-index levels" and calls yfinance "the one free source of raw index
levels"; app/services/compute.py:542 says "no free raw index source". FRED
serves the native Nasdaq-100 level on the free key this service already holds,
with more history than the proxy:

  NASDAQ100   10,602 obs, 1986-01-02 -> 2026-08-20   (QQQ starts 1999-03)
  CPIAUCSL       956 obs, 1947-01-01 -> 2026-07-01
  SP500        2,610 obs, 2016-08-22 only -- a ROLLING 10-YEAR window, and
                          therefore useless for a test that needs T >= 100
                          monthly observations. Nasdaq only, deliberately.

LICENCE. FRED marks the Nasdaq OMX series as copyrighted and for personal use,
with redistribution requiring permission. This service publishes an API, so the
INDEX LEVEL itself must not be re-served; only the derived statistic is. That
is why nothing here returns the level to a caller outside the engine.

NO KEYLESS ROUTE. fredgraph.csv answers 200 to curl's and httpx's own
User-Agents and 000 to any custom one (measured 2026-08-21, 3/3, including
'foo (research monitor)'), so reaching it would mean sending a User-Agent this
service is not. api.stlouisfed.org is not UA-filtered. The keyed API is the
only honest route, and FRED_API_KEY is already required for eight other series.
"""

from __future__ import annotations

import math
from collections import OrderedDict

from app.sources import SourceError
from app.sources.fred import observations

NASDAQ_100 = "NASDAQ100"
CPI = "CPIAUCSL"


def _month_end(pairs: list[tuple[str, float]]) -> OrderedDict[str, float]:
    """Last observation of each calendar month, keyed 'YYYY-MM'.

    Month-END, matching the service's existing monthly convention. Chen et al.
    use FRED's monthly AVERAGE aggregation instead; that difference is recorded
    as a known deviation rather than silently adopted, because changing the
    aggregation is a separate methodology question from changing the deflator.
    """
    out: OrderedDict[str, float] = OrderedDict()
    for date, value in pairs:              # oldest first
        out[date[:7]] = value              # later obs overwrite earlier
    return out


def real_monthly_log_index(start_year: int = 1999,
                           index_series: str = NASDAQ_100,
                           cpi_series: str = CPI) -> tuple[str, list[float]]:
    """(as_of 'YYYY-MM', log real monthly index levels), oldest first.

    Deflation is level-consistent, not base-normalised: dividing by CPI rescales
    the whole series by a constant, and a constant scale factor cancels in a
    log-price unit-root test. What matters is the SHAPE change from removing a
    time-varying price level, which is exactly what this does.
    """
    idx = _month_end(observations(index_series))
    cpi = _month_end(observations(cpi_series))
    if not idx or not cpi:
        raise SourceError("FRED real index: empty index or CPI series")

    months = [m for m in idx if m in cpi and int(m[:4]) >= start_year]
    if len(months) < 100:                  # PSY tabulations start at T=100
        raise SourceError(
            f"FRED real index: only {len(months)} overlapping months from {start_year} "
            "(need >= 100 for a calibrated GSADF)")

    series = [math.log(idx[m] / cpi[m]) for m in months]
    if not all(math.isfinite(v) for v in series):
        raise SourceError("FRED real index: non-finite value after deflation")
    return months[-1], series
