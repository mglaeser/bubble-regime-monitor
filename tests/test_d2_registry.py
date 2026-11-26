"""REGISTRY d2 says what the D2 number does.

Written from the formula the registry's `how` states (base = clip((yoy -
anchor)/span, 0, 1), times 1.0 on a confirmed rollover, else the no-rollover
multiplier) with the constants of frozen_methodology.json. The registry's
`what`, `why` and first caveat are the reference text the digest's model reads
for D2 (app/message_engine/context.py: references_for), and the methodology
endpoint serves the whole record.

Production, 615 readings from 2026-07-11 to 2026-11-10: margin-debt growth
went 53.7 -> 49.0 -> 38.6 -> 37.2 -> 31.4 % with no rollover ("no rollover"
609 times, unknown 6, confirmed 0). The sub-score went from 0.492 to 0.109,
which took 3.30 points off the headline (52.11 with July's sub-score against
the published 48.81 on 2026-11-10). The registry called the number "a
deleveraging-confirmation signal" and the rollover "margin debt YoY turning
down". A reader of those two sentences who sees growth fall concludes that
deleveraging is confirmed, while the monitor read "no rollover" every time.
None of the 54 digests sent so far mentions margin debt.
"""

from __future__ import annotations

import pytest

from app import methodology
from app.indicators import d2_margin
from app.message_engine import context
from app.references import REGISTRY

ANCHOR = methodology.get_path("indicators", "d2", "yoy_anchor")
SPAN = methodology.get_path("indicators", "d2", "yoy_span")
NO_ROLLOVER = methodology.get_path("indicators", "d2", "no_rollover_mult")
D2 = REGISTRY["d2"]


def test_what_says_the_reading_falls_when_growth_slows():
    # Production's first and newest reading, neither with a rollover.
    assert d2_margin.sub_score(53.7, False) == pytest.approx(0.492, abs=1e-3)
    assert d2_margin.sub_score(31.39, False) == pytest.approx(0.109, abs=1e-3)
    assert "falls when growth slows" in D2.what
    # The number is highest in the boom and fell with no deleveraging confirmed.
    assert "deleveraging-confirmation signal" not in D2.what


def test_why_names_the_rollover_the_code_tests():
    # Growth turning down is not the rollover: the balances rise every month
    # here while their year-over-year growth falls from 60 % to 34 %.
    slowing = [100.0, 110.0, 121.0, 125.0, 130.0, 135.0, 140.0, 145.0, 150.0, 153.0,
               156.0, 158.0, 160.0, 161.0, 162.0]
    growth = [d2_margin.yoy_pct(slowing[:n]) for n in (13, 14, 15)]
    assert growth[0] > growth[1] > growth[2]
    assert d2_margin.rollover_confirmed(slowing) is False
    # The balances falling two months running is.
    assert d2_margin.rollover_confirmed([*slowing[:13], 159.0, 158.0]) is True
    assert "YoY turning down" not in D2.why
    assert "falling two months running" in D2.why


def test_the_caveat_states_where_the_number_is_zero_and_where_a_rollover_adds():
    boom_maximum = d2_margin.sub_score(ANCHOR + SPAN, False)
    break_even = ANCHOR + NO_ROLLOVER * SPAN
    assert boom_maximum == pytest.approx(NO_ROLLOVER)
    assert d2_margin.sub_score(1000.0, False) == pytest.approx(NO_ROLLOVER)
    assert d2_margin.sub_score(ANCHOR, True) == 0.0
    assert d2_margin.sub_score(-10.0, True) == 0.0          # margin debt down on the year
    assert d2_margin.sub_score(break_even, True) == pytest.approx(boom_maximum)
    assert d2_margin.sub_score(break_even + 1, True) > boom_maximum
    # With no rollover confirmed the reading moves with growth alone; a
    # confirmation that lapses lowers it at unchanged growth.
    unconfirmed = [d2_margin.sub_score(float(g), False) for g in range(-20, 91)]
    assert unconfirmed == sorted(unconfirmed)
    assert d2_margin.sub_score(50.0, False) < d2_margin.sub_score(50.0, True)

    assert len(D2.caveats) == 2
    caveat = D2.caveats[1]
    assert f"at or below {ANCHOR:g}% reads 0" in caveat
    assert f"maximum of {NO_ROLLOVER:g}" in caveat
    assert f"above {break_even:g}%" in caveat
    assert "While no rollover is confirmed, a falling reading means" in caveat


def test_the_prompt_reads_the_corrected_sentences_whole_and_the_first_caveat():
    # The digest's prompt takes `what`, `why` and caveats[0], cut to whole
    # sentences at context.MAX_TEXT. The first caveat stays the first; the
    # second is not prompt material (GET /api/v1/indicators/d2 and
    # /api/v1/meta/methodology serve every caveat).
    (ref,) = context.references_for("MARGIN_ROLLOVER")
    assert ref.what == " ".join(D2.what.split())
    assert ref.why == " ".join(D2.why.split())
    assert D2.caveats[0].startswith("CXO Advisory")
    assert ref.caveat == " ".join(D2.caveats[0].split())
