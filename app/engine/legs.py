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
VRP = VIX^2 - RealizedVariance_21d(SPY), annualized, flag if VRP <= 0;
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


def faber_state(monthly: list[float]) -> str:
    """IN if last monthly close > 10-month SMA of monthly closes, else OUT."""
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


@dataclass
class FastAlarm:
    term_structure: str
    vrp: float | None
    vrp_flag: bool
    skew: float | None
    skew_label: str = "coincident context only"

    def as_dict(self) -> dict[str, object]:
        return {
            "term_structure": self.term_structure,
            "vrp": self.vrp,
            "vrp_flag": self.vrp_flag,
            "skew": self.skew,
            "skew_label": self.skew_label,
        }


def fast_alarm(term_structure_state: str, vix_level: float | None,
               daily_spy_closes: list[float] | None, skew: float | None) -> FastAlarm:
    vrp: float | None = None
    if vix_level is not None and daily_spy_closes:
        vrp = variance_risk_premium(vix_level, daily_spy_closes)
    return FastAlarm(
        term_structure=term_structure_state,
        vrp=round(vrp, 2) if vrp is not None else None,
        vrp_flag=vrp is not None and vrp <= 0.0,
        skew=skew,
    )
