"""v3.7.7 §3.1 — FINRA margin-rollover confirmation is CALENDAR-aware.

C-07 (v3.7.6) fixed only the YoY half of D2; rollover_confirmed() was still
positional. The margin rollover drives the D2 multiplier (0.6 <-> 1.0 =
tripwire ii), so a publication gap could flip it from mis-spaced positions.
RED-first on 5ca4500 (rollover_confirmed_calendar does not exist)."""

from __future__ import annotations


def test_rollover_calendar_unknown_on_gap():
    from app.indicators.d2_margin import rollover_confirmed, rollover_confirmed_calendar

    # 2026-04 missing; the present tail [...2026-03, 2026-05, 2026-06] is declining.
    months = ["2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
              "2026-01", "2026-02", "2026-03", "2026-05", "2026-06"]
    values = [100, 90, 95, 92, 130, 128, 126, 124, 122, 118, 110.0]  # declining tail
    # positional path WOULD confirm a rollover from the mis-spaced last three...
    assert rollover_confirmed(values) is True
    # ...but the calendar path must NOT: 2026-03 -> 2026-05 -> 2026-06 are not
    # consecutive -> UNKNOWN (None), so the caller keeps the conservative 0.6.
    assert rollover_confirmed_calendar(months, values) is None


def test_rollover_calendar_confirms_on_contiguous_decline():
    from app.indicators.d2_margin import rollover_confirmed_calendar

    months = [f"2025-{m:02d}" for m in range(1, 13)] + ["2026-01", "2026-02"]
    # peak mid-window, then two consecutive monthly declines to the consecutive tail
    values = [100, 105, 110, 150, 148, 140, 135, 130, 128, 126, 124, 122, 120, 118.0]
    assert rollover_confirmed_calendar(months, values) is True


def test_rollover_calendar_none_when_months_absent():
    from app.indicators.d2_margin import rollover_confirmed_calendar

    # length mismatch / no labels -> UNKNOWN, never a positional guess
    assert rollover_confirmed_calendar([], [1.0, 2.0, 3.0]) is None
