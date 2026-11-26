"""The judgment's prompt: what the model is handed (app/engine/judgment.py).

Written from the methodology registry (app/references.py, the de-facto spec):
every indicator of the registry's two blocks is a gauge the prompt hands over
by its id, and a model asked for "the ONE biggest thing" in plain words can
name it only when the prompt says what the id measures.
"""

from __future__ import annotations

import re

from app.engine import judgment
from app.references import REGISTRY

#: The gauges: the registry's indicators of the S and the D block.
GAUGES = sorted(key for key, record in REGISTRY.items() if record.block in ("S", "D"))


def test_the_prompt_says_what_every_gauge_it_is_handed_measures():
    """One line per gauge, `<id> = <plain words>`. Without it production's model
    answered "What do the unnamed measures track?" on 50 of the 145 flag-free
    judgments between 2026-10-16 and 2026-11-10."""
    assert GAUGES == ["d1", "d2", "d3", "d4", "s1", "s2", "s3", "s4", "s5"]
    for gauge in GAUGES:
        lines = re.findall(rf"(?m)^\s*{gauge} = (.+)$", judgment.PROMPT_TEMPLATE)
        assert len(lines) == 1 and len(lines[0].split()) >= 5, (gauge, lines)
