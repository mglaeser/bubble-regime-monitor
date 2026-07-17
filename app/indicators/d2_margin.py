"""D2 — Margin-Debt Rollover. weight = 0.13. JUDGMENTAL / confirmation-only.

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["d2"]; summary:

    yoy = 12-month % change of FINRA debit balances
    base = clip((yoy - 25)/35, 0, 1)
    mult = 1.0 if two consecutive monthly declines from a trailing-12-month
           high, else 0.6
    sub_score = base * mult

CAVEAT (verbatim): CXO Advisory finds ~0.00 correlation between margin-debt
changes and next-month returns and a 1-2 month lag versus stocks -> this is
confirmation-only, low weight. There is a 3-4 week publication lag (published
~third week of the following month) and no true fallback source — cache and
tolerate staleness (MacroMicro mirrors the same series for display only).

EPISTEMIC GUARDRAILS (verbatim):
1. NOT-A-PROBABILITY. The headline is a 0-100 regime heuristic = structured
   expert judgment; it is uncalibrated and is not investment advice.
2. n ~= 4 CALIBRATION IMPOSSIBILITY. The reference class of comparable US
   equity manias is ~= {1929, 2000, 2007, 2021}. With ~4 events, no honest
   probability calibration is possible.
3. REFERENCE-CLASS CAVEAT. The current episode may be rational
   general-purpose-technology (GPT) repricing rather than a bubble. Chen,
   Chen & Huang (2026, arXiv 2604.25826) show GSADF-type tests spuriously
   reject the no-bubble null 93-100% of the time under hump-shaped GPT
   fundamentals; hence the GSADF indicator carries a low weight and a
   permanent CONTESTED flag.
4. NOMINAL != EFFECTIVE WEIGHTS. Nominal weights rarely equal a variable's
   realized influence (Paruolo, Saisana & Saltelli 2013). The service ships
   an annual sensitivity script computing first-order main effects and
   comparing them to nominal weights, flagging any |nominal - effective| > 0.10.
5. NEVER HTTP 500 ON DATA FAILURE. On any upstream data failure the service
   must fall back down a defined chain, or drop the indicator and renormalize
   its block, always attaching a provenance note. Upstream failure must never
   surface as a 500.
"""

from __future__ import annotations

ROLLOVER_MULT = 1.0
NO_ROLLOVER_MULT = 0.6


def yoy_pct(debit_balances_monthly: list[float]) -> float:
    """12-month % change of the debit-balance series (chronological order).

    POSITIONAL — correct only on a gap-free monthly series. Prefer
    yoy_pct_calendar when dated months are available (v3.7.6/C-07)."""
    if len(debit_balances_monthly) < 13:
        raise ValueError("need >= 13 monthly observations for YoY")
    latest, prior = debit_balances_monthly[-1], debit_balances_monthly[-13]
    return (latest / prior - 1.0) * 100.0


def yoy_pct_calendar(months: list[str], values: list[float]) -> float:
    """12-month % change matched by CALENDAR month (v3.7.6/C-07).

    Finds the latest month and the month EXACTLY 12 calendar months earlier; if
    that reference month is absent from the series (a publication gap), it raises
    rather than silently comparing the wrong month via a positional [-13] offset
    (on a gapped list, [-13] is 12 list-positions back, not 12 months back)."""
    if len(values) < 13 or len(months) != len(values):
        raise ValueError("need >= 13 dated monthly observations for YoY")
    by_month = dict(zip(months, values, strict=True))
    latest = max(by_month)                      # "YYYY-MM"
    y, m = int(latest[:4]), int(latest[5:7])
    target = f"{y - 1:04d}-{m:02d}"             # same month, one calendar year earlier
    if target not in by_month:
        raise ValueError(f"YoY reference month {target} missing (gap) — refusing positional fallback")
    return (by_month[latest] / by_month[target] - 1.0) * 100.0


def rollover_confirmed(debit_balances_monthly: list[float]) -> bool:
    """Two consecutive monthly declines from a trailing-12-month high."""
    if len(debit_balances_monthly) < 3:
        return False
    trailing = debit_balances_monthly[-12:]
    peak_idx = max(range(len(trailing)), key=lambda i: trailing[i])
    last3 = debit_balances_monthly[-3:]
    declines = last3[2] < last3[1] < last3[0]
    peak_not_latest = peak_idx < len(trailing) - 2
    return declines and peak_not_latest


def sub_score(yoy: float, rollover: bool) -> float:
    base = max(0.0, min(1.0, (yoy - 25.0) / 35.0))
    return base * (ROLLOVER_MULT if rollover else NO_ROLLOVER_MULT)
