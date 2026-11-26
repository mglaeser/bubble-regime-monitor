"""REGISTRY d4 and the SKEW caveat say what the code computes and serves.

D4. GET /api/v1/indicators/d4 serves the d4 record, every snapshot stores its
`what` as the reading's explanation, and the daily digest's model reads `what`,
`why` and the first caveat (app/message_engine/context.py). `how` said the
confidence "== n_qualifying/n_evaluated". The pinned library divides by the
windows whose fit is a positive bubble (b < 0), not by every fitted window. On
the 601 production readings that carry the counts (2026-07-15 to 2026-11-10)
the value equals qualifying / positive on all 601 and qualifying / fitted on
none; 83 to 130 of the 144 fitted windows were positive; the mean value is
0.299 against 0.208 over all fitted windows.

SKEW. LEG_CAVEATS["skew"] is served as `skew_caveat` beside the level by
GET /api/v1/legs/fast-alarm and as the fast-alarm leg's caveat on the status
page, and it is a line of the framework block in the digest's prompt
(context.framework_for). It called a reading above ~131 the index's top 5%.
That is the sample of the analysis it cites: of the service's 615 stored
readings (2026-07-11 to 2026-11-10) 609 are above 131, minimum 126.4, median
148.5, maximum 168.6.
"""

from __future__ import annotations

import pytest

from app.message_engine import context
from app.references import CHANGELOG, LEG_CAVEATS, LEGS_SCIENCE, REGISTRY


def _flat(text: str) -> str:
    return " ".join(text.split())


def _fit(b: float, *, qualifies: bool) -> dict:
    # A window of 100 days ending at t2 = 1000. tc inside (t2 - 50, t2 + 50),
    # m in (0, 1), w in [4, 25], O above 2.5 and D above 0.5 qualify; m = 1.5
    # does not.
    return {"t1": 900.0, "t2": 1000.0, "tc": 1010.0, "m": 0.5 if qualifies else 1.5,
            "w": 8.0, "b": b, "c": 0.1, "c1": 0.1, "c2": 0.1, "O": 5.0, "D": 1.0}


def test_the_library_divides_by_the_positive_bubble_windows():
    """The fact the registry must state. Four fitted windows: two positive
    bubbles (b < 0), one of them inside every filter, and two negative ones
    (b > 0). Red if the library ever changes its denominator."""
    lppls = pytest.importorskip("lppls.lppls")
    import numpy as np

    from app.indicators.d4_lppls import FILTER_CONDITIONS, _positive_qualified

    fits = [_fit(-1.0, qualifies=True), _fit(-1.0, qualifies=False),
            _fit(+1.0, qualifies=False), _fit(+1.0, qualifies=False)]
    model = lppls.LPPLS(observations=np.array([[1.0, 2.0, 3.0], [1.0, 1.1, 1.2]]))
    row = model.compute_indicators([{"t2": 1000.0, "p2": 1.0, "res": fits}],
                                   filter_conditions_config=FILTER_CONDITIONS).iloc[-1]
    assert row["pos_conf"] == pytest.approx(1 / 2)      # of the 2 positive windows
    assert row["pos_conf"] != pytest.approx(1 / 4)      # not of the 4 fitted
    # the counts the reading's note reports are on the same denominator
    assert _positive_qualified(list(row["_fits"])) == (1, 2)


def test_d4_says_which_windows_the_fraction_is_over():
    record = REGISTRY["d4"]
    what, how = _flat(record.what), _flat(record.how)
    assert "n_qualifying/n_evaluated" not in how
    assert "positive-bubble" in what and "not a share of all fitted windows" in what
    assert "positive-bubble" in how and "b < 0" in how and "NOT by all fitted windows" in how


def test_the_digest_material_carries_the_whole_d4_sentence():
    """The digest's model reads `what` cut to whole sentences within
    context.MAX_TEXT: the sentence that names the denominator is inside."""
    assert len(_flat(REGISTRY["d4"].what)) <= context.MAX_TEXT
    material = context.render(context.references_for("daily_digest"))
    assert f"(D4, literature-grounded): {_flat(REGISTRY['d4'].what)}" in material
    assert "It is not a share of all fitted windows." in material


def test_the_changelog_no_longer_states_the_equality():
    entry = next(e for e in CHANGELOG if e["version"] == "v3.3.2")
    notes = _flat(entry["notes"])
    assert "this equals n_qualifying/n_evaluated" not in notes
    assert "positive-bubble windows" in notes


def test_the_skew_caveat_dates_its_level():
    caveat = _flat(LEG_CAVEATS["skew"])
    assert "COINCIDENT CONTEXT ONLY" in caveat
    assert "top 5% (>~131) has had" not in caveat       # 131 as today's top 5%
    assert "in his sample" in caveat and "609 of this service's 615 stored readings" in caveat


def test_the_digest_framework_carries_the_dated_skew_caveat():
    """The fast-alarm leg's caveat is a line of the framework block in the
    digest's prompt: the model reads the dated sentence, not 131 as an
    extreme."""
    (leg,) = [leg for leg in LEGS_SCIENCE if leg["id"] == "fast_alarm"]
    assert leg["caveat"] == LEG_CAVEATS["skew"]
    block = context.framework_for("daily_digest")
    assert f"- {leg['name']}: {LEG_CAVEATS['skew']}" in block
    assert "top 5% (>~131)" not in block
