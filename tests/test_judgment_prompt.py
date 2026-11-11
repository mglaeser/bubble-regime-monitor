"""The judgment's prompt: what the model is handed (app/engine/judgment.py).

Written from the methodology registry (app/references.py, the de-facto spec)
and AGENTS.md ground rule 1: every indicator of the registry's two blocks is a
gauge the prompt hands over by its id, and a model asked for "the ONE biggest
thing" in plain words can name it only when the prompt says what the id
measures.
"""

from __future__ import annotations

import re
import string

from app.engine import judgment
from app.references import REGISTRY

#: The gauges: the registry's indicators of the S and the D block.
GAUGES = sorted(key for key, record in REGISTRY.items() if record.block in ("S", "D"))

#: A snapshot's values as compute.persist_snapshot hands them over.
GOOD = dict(median=52.0, iqr=(50.0, 54.0), band="trim",
            s_scores={"s1": 0.97, "s2": 0.85, "s3": None, "s4": 0.05, "s5": 0.85},
            d_scores={"d1": 0.63, "d2": 0.11, "d3": 0.3, "d4": 0.23}, v=1.05,
            red_flag_detail={"gsadf_explosive_noncontested": False, "semi_runup_ge_150pp": False,
                             "hy_oas_widen_gt_100bps": False, "breadth_lt_50_near_ath": True},
            override=False, spy_state="IN", qqq_state="OUT",
            fast_alarm={"term_structure": "backwardation", "vrp": -3.2, "vrp_flag": True, "skew": 150.34})


def _prompt(monkeypatch, **overrides):
    """The prompt `generate` sends, captured where it leaves for the gateway."""
    seen: list[str] = []
    monkeypatch.setattr(judgment, "run_completion", lambda prompt: seen.append(prompt) or "Calm.")
    judgment.generate(**{**GOOD, **overrides})
    return seen[0]


def test_the_prompt_says_what_every_gauge_it_is_handed_measures(monkeypatch):
    """One line per gauge, `<id> = <plain words>`. Without it production's model
    answered "What do the unnamed measures track?" on 50 of the 145 flag-free
    judgments between 2026-10-16 and 2026-11-10."""
    prompt = _prompt(monkeypatch)
    assert GAUGES == ["d1", "d2", "d3", "d4", "s1", "s2", "s3", "s4", "s5"]
    for gauge in GAUGES:
        lines = re.findall(rf"(?m)^\s*{gauge} = (.+)$", prompt)
        assert len(lines) == 1 and len(lines[0].split()) >= 5, (gauge, lines)


def test_the_gauge_names_are_the_templates_own_words_not_a_field():
    """AGENTS.md ground rule 1: the legend is template text. The interpolated
    fields are exactly the twelve numbers and enums, no more."""
    fields = {name for _, name, _, _ in string.Formatter().parse(judgment.PROMPT_TEMPLATE) if name}
    assert fields == {"median", "iqr_lo", "iqr_hi", "band", "s_scores", "d_scores", "v",
                      "red_flag_detail", "override", "spy_state", "qqq_state", "fast_alarm"}
