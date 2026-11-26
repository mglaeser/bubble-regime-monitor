"""The methodology registry says what the code does (app/references.py is the
de-facto spec, AGENTS.md). GET /api/v1/indicators/{id} serves these sentences,
each stored reading carries `what` as its explanation, and the daily digest's
model reads `what`, `why` and the first caveat (app/message_engine/context.py).
"""

from __future__ import annotations

import pytest

from app.message_engine import context


@pytest.mark.parametrize("retired", [
    "and SMH monthly", "plus the p-value", "feeds red-flag #4",
    "today predicts", "does not currently meet",
])
def test_the_digest_material_carries_no_retired_claim(retired):
    material = context.render(context.references_for("daily_digest"))
    assert retired not in material


def test_the_digest_material_carries_the_whole_s5_reading():
    # context._bounded keeps whole sentences up to MAX_TEXT: the sentence that
    # says what the reading does not follow must not be the one that is cut.
    material = context.render(context.references_for("daily_digest"))
    assert "it does not follow the spreads of the day" in material
