"""Real (CPI-deflated) monthly index levels from FRED, for the S4 GSADF input.

WHY THIS EXISTS. S4 runs the Phillips-Shi-Yu (2015) right-tailed unit-root test
for explosive dynamics. The papers the service cites run it on a REAL price
index; the service runs it on NOMINAL, DIVIDEND-ADJUSTED QQQ closes:

  * PSY (2015) apply it to the real (CPI-deflated) S&P 500 and its
    price-dividend ratio.
  * Chen et al. (2026, arXiv:2604.25826) run PSY on the log REAL NASDAQ series
    in sec 5.5.1. Their dependent variable, sec 5.6.2 verbatim: "The dependent
    variable is the log NASDAQ Composite Index, constructed from CRSP
    value-weighted NASDAQ returns." FRED supplies their COVARIATES only
    ("...CPI, and industrial production (all from FRED)").

    CORRECTION OF RECORD. An earlier version of this file, and commit c214f1c,
    attributed to them the phrase "the log real NASDAQ Composite Index, obtained
    from FRED and deflated by the CPI". That phrase is NOT IN THE PAPER --
    checked against the full text of arXiv:2604.25826v1: "obtained from FRED" 0
    hits, "deflated by the CPI" 0 hits. It came from an agent report that
    presented it as a verbatim retrieval and was propagated here without being
    opened. The deflation PREMISE survives on PSY (2015) and on Chen et al.'s own
    real-NASDAQ application; only the quotation and the sourcing were wrong.

    THE REAL DEVIATION, which the invented one obscured: their target is the
    NASDAQ COMPOSITE (CRSP value-weighted); this module uses the NASDAQ-100
    (FRED NASDAQ100). That substitution is deliberate -- s4 has always scored the
    Nasdaq-100 -- and it is the deviation worth naming.

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

  NASDAQ100   10,239 NON-MISSING daily obs, 1986-01-02 -> 2026-08-20
                          (QQQ starts 1999-03)
  CPIAUCSL       954 NON-MISSING monthly obs, 1947-01 -> 2026-07
  SP500        a ROLLING 10-YEAR window (121 month-ends). It CLEARS the T >= 100
                          gate -- an earlier version of this comment claimed it
                          did not, which was wrong. It is excluded because a
                          10-year window cannot contain the episodes the test
                          exists to find, not because it is too short to run.

  Counts are NON-MISSING observations, which is what fred.observations() returns.
  An earlier version quoted `wc -l` figures (10,602 / 956) as observation counts
  -- inside a correction-of-record about unverified facts. Measured 2026-08-22.

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

    Month-END, matching the service's existing monthly convention -- and, as it
    happens, matching Chen et al., whose variable table reads "p_t Log NASDAQ
    Composite index (monthly close)". An earlier version of this docstring
    claimed they used FRED's monthly AVERAGE and recorded that as a known
    deviation; "monthly average" has 0 hits in the paper. There was no deviation
    to disclose, and inventing one was worse than missing it.
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

    index_months = [m for m in idx if int(m[:4]) >= start_year]
    if len(index_months) < 100:            # PSY tabulations start at T=100
        raise SourceError(
            f"FRED real index: only {len(index_months)} index months from {start_year} "
            "(need >= 100 for a calibrated GSADF)")

    # CONTIGUITY. radf() reads POSITION, not date: element i+1 is treated as one
    # month after element i. fred.observations() drops missing values ('.'), so a
    # hole in EITHER series would silently store a two-month log return in a
    # one-month slot -- about 40% of a monthly standard deviation on the index,
    # inside the recent windows GSADF is most sensitive to.
    #
    # CPIAUCSL genuinely has such a hole at 2025-10, and it is PERMANENT, not a
    # publication lag: 2025-11 through 2026-07 are all published (measured
    # 2026-08-22). An earlier version of this module refused on any gap and
    # described the hole as temporary. That was the wrong remedy twice over --
    # no start year escapes 2025-10, so it made the series permanently
    # unbuildable, and the earliest gap-free window reaches T >= 100 around 2034.
    #
    # The magnitudes decide it. NASDAQ100 HAS 2025-10; only the deflator is
    # missing, and the whole two-month CPI move is log(325.063/324.245) = 0.0025,
    # roughly 3.7% of a monthly index standard deviation -- about eleven times
    # SMALLER than the artefact the check exists to prevent. Dropping 330 usable
    # months to avoid that is a worse error than carrying the deflator forward.
    #
    # So: carry a SINGLE missing deflator month forward, explicitly and counted.
    # A run of two or more consecutive missing months, or any hole in the INDEX
    # itself, still refuses -- those are not one-off publication artefacts and
    # the index gap cannot be imputed from the index.
    filled: list[str] = []
    for a, b in zip(index_months, index_months[1:], strict=False):
        step = (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])
        if step != 1:
            raise SourceError(
                f"FRED real index: non-contiguous months {a} -> {b} in the INDEX. "
                "The test reads position as time, so a gap would enter as a "
                "mis-sized return, and an index gap cannot be imputed.")

    # A missing CPI month shows up as an index month with no deflator, never as a
    # gap in the index itself -- which is why the contiguity gate above runs on
    # index_months and the carry-forward below runs on the deflator.
    # TRAILING months with no deflator are TRUNCATED, never carried. CPI is
    # published with a lag, so the newest index months routinely have none -- and
    # the newest month is also the one whose index value is still in progress
    # (FRED stamps it at month-end, which is how s4's as_of came to be
    # future-dated). Carrying a deflator there would invent a real level for a
    # month that has neither a published deflator nor a settled index. Only an
    # INTERIOR single-month hole is carried.
    last_with_cpi = max((i for i, m in enumerate(index_months) if m in cpi), default=-1)
    if last_with_cpi < 0:
        raise SourceError("FRED real index: no month has a deflator")
    index_months = index_months[:last_with_cpi + 1]

    deflator: dict[str, float] = {}
    prev: float | None = None
    for i, m in enumerate(index_months):
        if m in cpi:
            deflator[m] = cpi[m]
            prev = cpi[m]
            continue
        nxt = index_months[i + 1] if i + 1 < len(index_months) else None
        if prev is None or (nxt is not None and nxt not in cpi):
            raise SourceError(
                f"FRED real index: deflator missing at {m} and not a single-month "
                "interior hole (no prior value, or the following month is missing too)")
        deflator[m] = prev
        filled.append(m)

    months = [m for m in index_months if m in deflator]
    if len(months) < 100:
        raise SourceError(
            f"FRED real index: only {len(months)} usable months from {start_year} "
            "(need >= 100 for a calibrated GSADF)")

    series = [math.log(idx[m] / deflator[m]) for m in months]
    if not all(math.isfinite(v) for v in series):
        raise SourceError("FRED real index: non-finite value after deflation")
    as_of = months[-1] if not filled else f"{months[-1]} (deflator carried at {','.join(filled)})"
    return as_of, series
