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


def test_the_registry_states_the_rule_the_code_applies():
    """The registry is the de-facto spec (AGENTS.md) and the API serves it. It
    said "two consecutive monthly declines from a trailing-12-month high"; the
    code has never required a high: two monthly declines decide alone (the two
    tests below)."""
    from app.references import REGISTRY

    how = REGISTRY["d2"].how
    assert "fell in each of the last two months" in how
    assert "12-month high" not in how


def test_the_rule_is_two_consecutive_declines_and_nothing_else():
    """REGISTRY d2 `how`: the multiplier is 1.0 if debit balances fell in each
    of the last two months. Every series of four levels over three to seven
    months, ties included (21,824 series): no other condition decides, in the
    positional or in the calendar form. A second condition added later turns
    this red."""
    import itertools

    from app.indicators.d2_margin import rollover_confirmed, rollover_confirmed_calendar

    months = [f"2025-{m:02d}" for m in range(1, 8)]
    for n in range(3, 8):
        for levels in itertools.product((1.0, 2.0, 3.0, 4.0), repeat=n):
            want = levels[-1] < levels[-2] < levels[-3]
            assert rollover_confirmed(list(levels)) is want
            assert rollover_confirmed_calendar(months[:n], list(levels)) is want


def test_two_small_declines_far_below_an_old_high_confirm():
    from app.indicators.d2_margin import multiplier, rollover_confirmed, rollover_confirmed_calendar

    # The high (140) lies ten months back; the last two months fall by 0.1 %
    # each, at 71 % of that high. The rule asks for the two declines only.
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    values = [100, 140, 120, 110, 105, 104, 103, 102, 101, 100.0, 99.9, 99.8]
    assert rollover_confirmed(values) is True
    assert rollover_confirmed_calendar(months, values) is True
    assert multiplier(True) == 1.0
