"""Which dynamic slots a model may write — enforced, not just documented.

The program's founding invariant is that the model never writes a number
(app/alerts/llm_selector.py contains it for the alert path by having the model
select CODES). Phase D generates PROSE for the dashboard, and the slot registry
mixes prose with computed statistics: signed coefficients, bounded
percentages, weights, date stamps. Nothing stopped a generator from filling
those, so the split is made explicit here and checked.
"""
from __future__ import annotations

import re

from app.content_registry import (
    DYNAMIC_SLOTS,
    MODEL_WRITABLE_SLOTS,
    dynamic_slots_payload,
    model_may_write,
)

#: A regex that admits ONLY digits and punctuation describes a computed value.
_NUMERIC_CONTRACT = re.compile(r"\\d\{|\[0-9\]|\\\.\\d")


def _is_numeric_contract(rx: str) -> bool:
    return bool(_NUMERIC_CONTRACT.search(rx)) and "x20-" not in rx


def test_no_numeric_slot_is_model_writable():
    """The load-bearing one: a statistic must never be model-written."""
    offenders = [
        s.slug for s in DYNAMIC_SLOTS
        if _is_numeric_contract(s.regex or "") and model_may_write(s.slug)
    ]
    assert offenders == [], (
        f"these slots have a numeric contract and would accept generated text: "
        f"{offenders} — the model never writes a number")


def test_slots_too_short_to_hold_a_sentence_are_not_model_writable():
    """A 24-character stamp is a computed value however it is spelled."""
    offenders = [s.slug for s in DYNAMIC_SLOTS
                 if s.max_len <= 48 and model_may_write(s.slug)]
    assert offenders == [], (
        f"{offenders} are too short to be prose; a computation already knows "
        "them exactly and a model restating them can only introduce error")


def test_the_allowlist_names_only_real_slots():
    """A stale slug in the allowlist would silently permit nothing, or worse,
    permit a slot that was renamed underneath it."""
    known = {s.slug for s in DYNAMIC_SLOTS}
    assert MODEL_WRITABLE_SLOTS <= known, MODEL_WRITABLE_SLOTS - known


def test_an_unknown_slug_is_not_writable():
    """Fail-closed: the question is asked with strings, so it must be safe to
    ask about one that does not exist."""
    assert not model_may_write("")
    assert not model_may_write("analytics.tail.gold.bf.evil")
    assert not model_may_write("../../etc/passwd")


def test_every_slot_is_classified_and_the_split_is_not_degenerate():
    """A denylist that ended up empty, or an allowlist that swallowed
    everything, would pass every other assertion here."""
    writable = [s for s in DYNAMIC_SLOTS if model_may_write(s.slug)]
    computed = [s for s in DYNAMIC_SLOTS if not model_may_write(s.slug)]
    assert len(writable) + len(computed) == len(DYNAMIC_SLOTS)
    assert writable and computed, "the classification collapsed to one side"


def test_the_served_payload_reports_the_writer():
    """A consumer must be able to tell a generated line from a computed one
    without knowing the registry by heart."""
    payload = dynamic_slots_payload()
    assert set(payload) == {s.slug for s in DYNAMIC_SLOTS}
    for slug, entry in payload.items():
        assert entry["writer"] in ("model", "data")
        assert entry["writer"] == ("model" if model_may_write(slug) else "data")


def test_placeholders_still_satisfy_their_own_contract():
    """Whatever the writer, what is SERVED today must already be legal — the
    placeholder is what a reader sees until generation lands."""
    for s in DYNAMIC_SLOTS:
        assert len(s.placeholder) <= s.max_len, s.slug
        assert re.match(s.regex, s.placeholder), (s.slug, s.placeholder)
