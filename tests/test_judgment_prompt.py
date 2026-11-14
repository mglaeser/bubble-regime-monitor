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


def test_the_judgment_is_handed_the_month_end_state(isolated_db, monkeypatch):
    """Leg 2 is Faber's rule, read once a month: its state is the last
    completed month's (legs.month_end_faber), and `faber_10mo` is the running
    month's preview. The pipeline handed the model the preview under
    "Long-term trend (still in the market?)": "SPY IN, QQQ OUT" in every one
    of production's 48 prompts from 2026-11-03 02:00 to 11-10 22:00 UTC, while
    the rule's state for QQQ was IN (October's close) and no trend alert
    fired. Here: the trend states production stored in snapshot 568."""
    from app.services.compute import compute_snapshot, persist_snapshot, write_judgment
    from tests.conftest import make_golden_raw_inputs

    seen: list[str] = []
    monkeypatch.setattr(judgment, "run_completion", lambda prompt: seen.append(prompt) or "Calm.")
    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
    data.trend_states = {
        "SPY": {"faber_10mo": "IN", "faber_live_preview": "IN", "faber_distance_pct": 2.7289757844301397,
                "faber_month_end_state": "IN", "faber_month_end_period": "2026-10"},
        "QQQ": {"faber_10mo": "OUT", "faber_live_preview": "OUT", "faber_distance_pct": -0.3471388086092497,
                "faber_month_end_state": "IN", "faber_month_end_period": "2026-10"}}
    write_judgment(persist_snapshot(data, raw), data)
    (line,) = [line for line in seen[0].splitlines() if line.startswith("Long-term trend")]
    assert line.endswith(": SPY IN, QQQ IN.") and "OUT" not in line.split(":")[-1]
    # and the line says what the two words are: the rule's monthly reading
    assert "once a month" in line and "finished month" in line and "still in the market" not in line
