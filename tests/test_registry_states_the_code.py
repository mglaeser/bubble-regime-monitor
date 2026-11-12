"""The methodology registry says what the code does (app/references.py is the
de-facto spec, AGENTS.md). GET /api/v1/indicators/{id} serves these sentences,
each stored reading carries `what` as its explanation, and the daily digest's
model reads `what`, `why` and the first caveat (app/message_engine/context.py).

Each case names the code fact the sentence has to agree with. Executed on a
copy of the production database, 2026-11-11: S5 rose from 0.52 to 0.85 on
2026-11-05 (the excess bond premium of two years earlier went from -0.06 to
-0.33 pp) while HY OAS stood at 334 bps, 75 bps above its three-year low of
259. A sentence that explains S5 by the spreads of the day explains that move
backwards.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import fields

import pytest

from app.message_engine import context
from app.references import REGISTRY, UNVERIFIED_CITATIONS


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_s5_what_names_the_reading_the_score_takes():
    # REGISTRY["s5"].how: the excess bond premium "read at t-2 years".
    what = _flat(REGISTRY["s5"].what).lower()
    assert "two years ago" in what and "excess bond premium" in what
    assert "today predicts" not in what


def test_s2_claims_no_part_in_a_red_flag():
    # Red flag 4 is breadth below 50 % while the index is near its high: the
    # function that evaluates the four flags takes no concentration input.
    from app.engine.aggregate import evaluate_red_flags

    inputs = set(inspect.signature(evaluate_red_flags).parameters)
    assert not {p for p in inputs if "top10" in p or "concentration" in p}
    assert "red-flag" not in _flat(" ".join(REGISTRY["s2"].caveats)).lower()


def test_s4_names_the_one_series_it_tests_and_no_p_value():
    # compute.py runs the test on the Nasdaq-100 proxy only; the runner's
    # result carries statistics and critical values, no p-value.
    from app.engine.gsadf_runner import GsadfOutput

    assert not [f.name for f in fields(GsadfOutput) if "p_val" in f.name or "pval" in f.name]
    record = REGISTRY["s4"]
    assert "SMH" not in record.what and "QQQ" in record.what
    assert "plus the p-value" not in _flat(record.caveats[0])
    assert "critical values" in _flat(record.caveats[0])


def test_d1_promises_no_confidence_interval():
    # sources/breadth.py: worst-case identification bounds, display only.
    how = _flat(REGISTRY["d1"].how)
    assert "binomial" not in how and "identification bounds" in how


def test_s3_dates_its_one_market_statement():
    # No code computes a Magnificent-7 run-up: the figure is the one the
    # indicator was scoped with, so the sentence carries its date.
    caveat = _flat(REGISTRY["s3"].caveats[0])
    assert "does not currently" not in caveat
    assert "July 2026" in caveat


def test_no_reference_is_flagged_unverified_while_none_is():
    assert UNVERIFIED_CITATIONS == []
    flagged = [ref for record in REGISTRY.values() for ref in record.references
               if "verify at build time" in ref]
    assert flagged == []


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


@pytest.mark.parametrize("module, indicator", [
    ("app.indicators.s3_semis_gsy", "s3"), ("app.indicators.s4_gsadf", "s4"),
])
def test_a_module_that_quotes_its_caveat_quotes_the_registry(module, indicator):
    # Both docstrings say "CAVEAT (verbatim)": a caveat edited in the registry
    # alone fails here.
    doc = _flat(importlib.import_module(module).__doc__)
    assert "CAVEAT (verbatim): " + _flat(REGISTRY[indicator].caveats[0]) in doc
