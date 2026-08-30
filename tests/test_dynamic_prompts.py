"""config/dynamic_prompts.v1.json must agree with the slot registry.

A prompt library that drifts from the registry is worse than none: it looks
authored while pointing at slots that no longer exist, or leaves a live slot
with nothing to generate from. Both directions are asserted, and so is the
invariant that decides which slots belong here at all.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.content_registry import DYNAMIC_SLOTS, model_may_write

LIBRARY = Path(__file__).resolve().parents[1] / "config" / "dynamic_prompts.v1.json"
DOC = json.loads(LIBRARY.read_text(encoding="utf-8"))
PROMPTS: dict = DOC["prompts"]
BY_SLUG = {s.slug: s for s in DYNAMIC_SLOTS}


def test_exactly_the_model_writable_slots_have_prompts():
    """Both directions. A missing prompt leaves a live slot ungeneratable; an
    extra one points the generator at a computed value."""
    writable = {s.slug for s in DYNAMIC_SLOTS if model_may_write(s.slug)}
    assert set(PROMPTS) == writable, {
        "missing": sorted(writable - set(PROMPTS)),
        "extra": sorted(set(PROMPTS) - writable),
    }


def test_no_prompt_exists_for_a_computed_slot():
    """Stated separately from the equality above, because this is the one that
    matters: a prompt for a numeric slot would invite the model to write a
    statistic."""
    for slug in PROMPTS:
        assert model_may_write(slug), f"{slug} is data-filled and must have no prompt"


@pytest.mark.parametrize("slug", sorted(PROMPTS))
def test_each_prompt_states_the_slot_s_real_length_bound(slug):
    """A prompt advertising the wrong cap trains output the validator rejects."""
    entry = PROMPTS[slug]
    assert entry["max_len"] == BY_SLUG[slug].max_len
    assert str(BY_SLUG[slug].max_len) in entry["prompt"]


@pytest.mark.parametrize("slug", sorted(PROMPTS))
def test_each_prompt_names_its_slot_and_grounding(slug):
    entry = PROMPTS[slug]
    assert slug in entry["prompt"]
    assert entry["grounding_fields"], f"{slug} grounds on nothing"
    for field in entry["grounding_fields"]:
        assert re.fullmatch(r"F_[A-Z0-9_]+", field), (slug, field)


@pytest.mark.parametrize("slug", sorted(PROMPTS))
def test_every_prompt_carries_the_hard_rules(slug):
    """The preamble is what keeps a slot prompt from quietly becoming a licence
    to write numbers or advice."""
    text = PROMPTS[slug]["prompt"]
    for phrase in ("VERBATIM", "No advice", "No forecasting", "printable ASCII"):
        assert phrase in text, f"{slug} lost the {phrase!r} rule"


@pytest.mark.parametrize("slug", sorted(PROMPTS))
def test_the_placeholder_matches_the_registry_and_its_own_contract(slug):
    """What is served today must already be legal, whatever the model does later."""
    slot = BY_SLUG[slug]
    assert PROMPTS[slug]["placeholder"] == slot.placeholder
    assert len(slot.placeholder) <= slot.max_len
    assert re.match(slot.regex, slot.placeholder)


def test_the_library_is_ascii_and_declares_its_rules():
    raw = LIBRARY.read_text(encoding="utf-8")
    assert raw.isascii(), "non-ASCII in a library for printable-ASCII slots"
    assert DOC["rules"]["verbatim_grounded_numerals"] is True
    # The stative directives round 34 found missing from the validator are
    # named here too, so the prompt discourages what the validator refuses.
    assert "stay in" in DOC["rules"]["banned_directives"]


def test_no_prompt_smuggles_an_example_number():
    """A worked example in a prompt is a number the model has seen and may
    reuse — the failure the grounding rule exists to prevent."""
    for slug, entry in PROMPTS.items():
        body = entry["prompt"]
        # The slot's OWN name legitimately carries digits (ai2026, ust10y),
        # and so does its stated length bound. Everything else is a number the
        # model has been shown and may reuse.
        body = body.replace(slug, " ").replace(str(entry["max_len"]), " ")
        body = body.replace(entry["purpose"], " ")
        stray = re.findall(r"(?<![A-Za-z_])\d+(?![A-Za-z_])", body)
        assert not stray, f"{slug} contains literal numeral(s) {stray}"


def test_no_prompt_asks_the_model_to_DERIVE_a_number():
    """A direction that asks for a difference asks for arithmetic.

    The engine admits numerals that appear VERBATIM in the grounded facts, so a
    slot asking "how far X is from Y" while grounding only X and Y leaves the
    generator two choices: omit the figure, or compute one the validator will
    refuse. Round 36 (SOTA-A) found exactly that in gauge.verdict.distance.
    The fix is to supply the derived value as its own fact, not to relax the
    rule.
    """
    # INSTRUCTIONS to derive, not the vocabulary of derivation. "when the
    # figures were last computed" describes provenance and is fine; "compute
    # the distance" is not. The first version matched the bare word and flagged
    # gauge.badge.live_tip for saying "last computed".
    derive = re.compile(
        r"how far|how much (?:more|less|higher|lower)|the difference between|"
        r"\bsubtract\b|\b(?:compute|calculate|derive|work out)\s+(?:the|a|its)\b|"
        r"\bpercent(?:age)? change\b",
        re.IGNORECASE)
    # A PROHIBITION against deriving is not an instruction to derive. Without
    # this, "do not subtract one supplied number from another" — the very
    # sentence that fixes the defect — trips the control that checks for it.
    negated = re.compile(r"(?:do not|don't|never|must not|rather than)[^.;]*",
                         re.IGNORECASE)
    for slug, entry in PROMPTS.items():
        direction = entry["prompt"].split("DIRECTION:", 1)[-1]
        direction = negated.sub(" ", direction)
        hit = derive.search(direction)
        assert not hit, (
            f"{slug} asks the model to derive a value ({hit.group(0)!r}); "
            "supply it as a grounded fact instead")


def test_a_slot_naming_a_distance_grounds_the_distance_itself():
    entry = PROMPTS["gauge.verdict.distance"]
    assert "F_DISTANCE_POINTS" in entry["grounding_fields"]
    assert "do not compute it" in entry["prompt"]
