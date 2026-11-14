"""Legs 2-3: Faber trend trigger and fast volatility alarm.

The three legs are NOT averaged. The headline is the Leg 1 (Strategic Gauge)
median; Legs 2-3 are executable overlays. Action bands: < 45 hold; 45-60
trim; >= 60 or override -> de-risk.

Leg 2 — Tactical Trend Trigger (executes de-risking; the score only sets the
strategic ceiling). Faber 10-month SMA rule on SPY and QQQ: state = IN if
last_monthly_close > SMA_10month(monthly_close) else OUT; the 200-day daily
variant is also exposed.
REFERENCE: Faber (2007), Journal of Wealth Management 9(4): 69-79.
CAVEAT (verbatim): Zakamulin (2014) — out-of-sample outperformance of
moving-average/time-series-momentum timing rules is data-mining-fragile; the
honest expectation is drawdown reduction, not return enhancement. See also
Moskowitz, Ooi & Pedersen (2012, JFE) and Huang, Li, Wang & Zhou (2020, JFE).

Leg 3 — Fast Alarm: (a) VIX term-structure state; (b) Variance Risk Premium
VRP = VIX^2 - RealizedVariance_21d(SPY), annualized, flag if VRP <= 0, both
legs of one session (closes_through);
(c) CBOE SKEW level. CAVEAT (verbatim): Bilello's analysis shows SKEW in its
top 5% (>~131) has had ~zero forward predictive value -> LABEL: COINCIDENT
CONTEXT ONLY.
REFERENCE: Bollerslev & Todorov (2011), Journal of Finance 66(6): 2165-2211.

Standing caveat: Cederburg, O'Doherty, Wang & Yan (2020, JFE 138(1): 95-117)
— any de-risking rule may destroy value net of costs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def monthly_closes(daily: list[tuple[str, float]]) -> list[float]:
    """Collapse (date_iso, close) dailies to month-end closes, oldest first."""
    out: list[float] = []
    current_month: str | None = None
    for date, close in daily:
        month = date[:7]
        if month != current_month:
            out.append(close)
            current_month = month
        else:
            out[-1] = close
    return out


def monthly_closes_dated(daily: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """(month, last close in that month), oldest first — same collapse as
    `monthly_closes`, keeping the month label the caller needs to date it."""
    out: list[tuple[str, float]] = []
    current_month: str | None = None
    for date, close in daily:
        month = date[:7]
        if month != current_month:
            out.append((month, close))
            current_month = month
        else:
            out[-1] = (month, close)
    return out


def _is_next_month(month: str, following: str) -> bool:
    """Is `following` the calendar month immediately after `month`?"""
    try:
        year, mon = (int(part) for part in month.split("-")[:2])
        nyear, nmon = (int(part) for part in following.split("-")[:2])
    except ValueError:
        return False
    return (nyear, nmon) == ((year + 1, 1) if mon == 12 else (year, mon + 1))


def month_end_faber(daily: list[tuple[str, float]], *, as_of_month: str,
                    feed_current: bool = False,
                    closed_months: frozenset[str] | None = None
                    ) -> tuple[str | None, str | None]:
    """The AUTHORITATIVE Faber state: (state, month), completed months only.

    `faber_state` deliberately stands the in-progress month's latest close in
    for its month-end close — a useful live preview, and the wrong thing to
    alert on. A rule that confirms on a new month-end period must see a state
    that changes only when a month has actually ENDED, or an intramonth wobble
    presents as a completed flip and the most severe alert in the system fires
    on a month that is still running.

    A month is completed when it is strictly earlier than the month we are
    computing in; the in-progress month is dropped whatever its closes say.
    Returns (None, None) when fewer than ten completed months exist.
    """
    dated = monthly_closes_dated(daily)
    if not dated:
        return None, None

    # A month is completed only when the DATA shows it closed — that is, when a
    # LATER bar exists. The calendar is not enough on its own: a stale feed
    # whose newest bar is mid-July is still "past" once September arrives, and
    # promoting July then would publish a mid-July close as July's month-end
    # close. That is the very substitution this function exists to refuse,
    # returning by way of the clock instead of the bar.
    #
    # The cost is a bounded lag: a month that ends on a Friday is not
    # authoritative until the next session's bar lands. That is deliberate. A
    # late correct state is recoverable; a P1 fired on a partial month is not,
    # and no staleness cutoff can separate the two without inventing a
    # threshold no artifact defines.
    #
    # `as_of_month` can only ever WITHHOLD, never promote: a feed dated in the
    # future relative to the evaluation is not evidence a month has closed.
    # A month is closed when the month IMMEDIATELY AFTER it also has a bar.
    #
    # "Some later bar exists" is not enough. A feed that stops mid-July and
    # resumes in September leaves July as the second-to-last month with a
    # September bar behind it — and July's last close is then a MID-JULY close,
    # promoted as if it were July's month-end. That is the same partial-month
    # substitution this function exists to refuse, arriving through the gap
    # instead of through the clock.
    #
    # A bar in the next calendar month proves the previous one ran to its end;
    # a bar two months later proves nothing about the month in between.
    if closed_months is not None:
        # ONE rule for every month, newest included. A month is closed when its
        # last bar is that month's last trading day, and that answer does not
        # depend on whether the feed is still publishing today: a feed whose
        # final bar IS 31 July has genuinely closed July, stale or not, while
        # one that stopped on 2 July has not, however current it looks.
        #
        # Judging the newest month by a freshness heuristic instead would
        # contradict the exact test applied to every other month — promoting a
        # partial month the calendar rejects, or withholding a complete one it
        # accepts.
        # STRICTLY earlier than the month being evaluated in. On the final
        # trading day itself the bar for that date exists but may still be an
        # intraday price rather than the close — the feed publishes a daily bar
        # that is only final once the session is over. Promoting then puts an
        # unfinished price into an authoritative month-end state on exactly one
        # day a month, which is the intramonth defect this branch exists to
        # remove, surviving in its last hiding place.
        #
        # The cost is at most a day or two of lag once the month rolls over.
        # A late correct state is recoverable; a P1 fired on a mid-session
        # price is not.
        completed = [(m, c) for m, c in dated
                     if m in closed_months and m < as_of_month]
    else:
        # Calendar-free fallback: a bar in the month immediately after proves
        # closure for all but the newest month, and the newest is taken only on
        # the caller's word that the feed is publishing normally. Weaker on
        # both counts, and only for callers without a calendar.
        adjacent = [
            (month, close)
            for i, (month, close) in enumerate(dated[:-1])
            if _is_next_month(month, dated[i + 1][0])
        ]
        newest_month, newest_close = dated[-1]
        if newest_month < as_of_month and feed_current:
            adjacent.append((newest_month, newest_close))
        completed = [(m, c) for m, c in adjacent if m < as_of_month]

    if len(completed) < 10:
        return None, None

    # The Faber rule is a TEN-MONTH SMA. Ten entries are not ten months if the
    # feed lost some: a series missing four months would average ten readings
    # spread over fourteen, and `faber_state` cannot tell — it receives a list
    # of closes with the labels already stripped. That is a different statistic
    # wearing the same name, and it would be published as an authoritative
    # month-end state and can fire a P1.
    #
    # Refuse instead of averaging what is there. A gap in the price history is
    # exactly the condition under which "we do not know" is the honest answer.
    window = completed[-10:]
    for (month, _close), (following, _next) in zip(window, window[1:], strict=False):
        if not _is_next_month(month, following):
            return None, None

    return faber_state([c for _, c in window]), window[-1][0]


def faber_distance_pct(monthly: list[float]) -> float | None:
    """Signed distance of the last close from the 10-month SMA, in percent.

    Evidence for a prewarning rule, never a state: the crossing itself is
    `faber_state`'s to decide.
    """
    if len(monthly) < 10:
        return None
    sma10 = sum(monthly[-10:]) / 10.0
    if sma10 == 0:
        return None
    return (monthly[-1] - sma10) / sma10 * 100.0


def faber_state(monthly: list[float]) -> str:
    """IN if last monthly close > 10-month SMA of monthly closes, else OUT.

    The in-progress month's latest close stands in for its month-end close
    (the common practical reading of Faber's month-end rule between month
    ends)."""
    if len(monthly) < 10:
        raise ValueError("need >= 10 monthly closes for the Faber rule")
    sma10 = sum(monthly[-10:]) / 10.0
    return "IN" if monthly[-1] > sma10 else "OUT"


def sma200_state(daily_closes: list[float]) -> str:
    """Daily variant: IN if last close > 200-day SMA."""
    if len(daily_closes) < 200:
        raise ValueError("need >= 200 daily closes")
    sma = sum(daily_closes[-200:]) / 200.0
    return "IN" if daily_closes[-1] > sma else "OUT"


def realized_variance_21d(daily_closes: list[float]) -> float:
    """Annualized realized variance: (252/21) * sum(r_t^2) over 21 trading days."""
    if len(daily_closes) < 22:
        raise ValueError("need >= 22 daily closes")
    window = daily_closes[-22:]
    rets = [math.log(window[i + 1] / window[i]) for i in range(21)]
    return (252.0 / 21.0) * sum(r * r for r in rets)


def variance_risk_premium(vix_level: float, daily_spy_closes: list[float]) -> float:
    """VRP = VIX^2 - RealizedVariance_21d(SPY), annualized, in variance points
    ((VIX/100)^2 scale, reported x10^4 for readability parity with VIX pts)."""
    iv = (vix_level / 100.0) ** 2
    rv = realized_variance_21d(daily_spy_closes)
    return (iv - rv) * 1e4


# The VRP is annualised implied less annualised realised variance, in percent
# squared: a VIX of 16 against 10 % realised volatility reads 256 - 100 = 156.
# The unit is published beside the value.
#
# No verdict on the magnitude is published with it. Until 2026-11 `vrp_sane`
# called a value outside -50..150 "a units/data error", a band that fits the
# monthly scale (VIX^2 / 12, "single digits to low tens") and not this one: in
# production it read false on 320 of the 605 snapshots that carried it, the
# median reading (153.6) among them, and on all six whose vrp_flag was set.
# Nothing read it. No wider band replaces it: the value runs from thousands
# above zero on a jump of the VIX (80^2 = 6400) to thousands below in a crash
# (VIX 60 against 90 % realised: -4500), so a magnitude cannot tell a wrong unit.
VRP_UNITS = "annualized_variance_pts_pct2"


@dataclass
class FastAlarm:
    term_structure: str
    vrp: float | None
    vrp_flag: bool
    skew: float | None
    skew_label: str = "coincident context only"
    vrp_as_of: str | None = None   # the session both legs of the VRP close on

    def as_dict(self) -> dict[str, object]:
        return {
            "term_structure": self.term_structure,
            "vrp": self.vrp,
            "vrp_as_of": self.vrp_as_of,
            "vrp_units": VRP_UNITS,
            "vrp_flag": self.vrp_flag,
            "skew": self.skew,
            "skew_label": self.skew_label,
        }


def closes_through(daily: list[tuple[str, float]] | None, day: str | None) -> list[float] | None:
    """The closes up to and including the bar dated `day`, or None when the
    series has no bar on that day.

    The VRP is one session's: implied variance at a day's close less the
    variance realised up to that same close. The VIX level is FRED's VIXCLS,
    which FRED posts the next business afternoon (first read by the 14:00 or
    18:00 UTC run); the price series holds that close from the night before.
    Until 2026-11 the newest of each was taken, whatever the two dates.
    Production, 213 recomputes from 2026-10-06 to 11-10: the dates agreed in
    118. On 2026-11-05 SPY fell 4.4 % and the VIX closed at 28.18; the next
    four runs published -119.00 with the flag set (16.61^2, the close of
    11-04, less the 394.9 realised through 11-05), where the pair of 11-04 is
    +124.32 and that of 11-05 +399.22. On 2026-11-10 four runs published
    +202.16 (25.64^2 of 11-06 less 455.3 through 11-09) where the pair of
    11-09 is -58.45.

    No bar on the day, or no day (the CBOE quote that stands in when FRED
    fails has none), is no pair and gives no premium: never the nearest other
    day's closes. On 2026-10-21 18:00 to 10-22 02:00 UTC FRED was down and the
    undated quote gave 175.41, 177.08, 177.08 between 198.49 before and after.

    Those 213 runs under this rule: 163 stay unflagged, the four of -119.00
    read +124.32 dated 11-04, the two of -58.45 keep their value and flag, and
    44 have no premium - the 3 of the undated quote, and 41 whose price series
    did not yet hold the day FRED's close was of."""
    if not daily or not day:
        return None
    for i in range(len(daily) - 1, -1, -1):
        if daily[i][0][:10] == day[:10]:
            return [close for _, close in daily[: i + 1]]
    return None


def fast_alarm(term_structure_state: str, vix_level: float | None,
               daily_spy_closes: list[float] | None, skew: float | None,
               vrp_as_of: str | None = None) -> FastAlarm:
    """`daily_spy_closes` are SPY's closes through the day `vix_level` closed
    on (closes_through), and `vrp_as_of` is that day."""
    vrp: float | None = None
    if vix_level is not None and daily_spy_closes:
        vrp = variance_risk_premium(vix_level, daily_spy_closes)
    return FastAlarm(
        term_structure=term_structure_state,
        vrp=round(vrp, 2) if vrp is not None else None,
        vrp_flag=vrp is not None and vrp <= 0.0,
        skew=skew,
        vrp_as_of=vrp_as_of[:10] if vrp is not None and vrp_as_of else None,
    )
