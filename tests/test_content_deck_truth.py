"""Four corrected statements of the display deck that GET /api/v1/score serves as data.display."""

from __future__ import annotations

import re

from app import content_registry


def _deck() -> dict:
    content_registry._clear_artifact_cache()
    return content_registry.gauge_display()


def test_the_chip_does_not_call_the_heuristic_calibrated():
    # Guardrail 1: "it is uncalibrated". \b keeps "uncalibrated" legal.
    assert not re.search(r"\bcalibrated\b", _deck()["epistemic.chip_long"]["text"], re.I)


def test_a_withheld_band_is_not_called_a_missing_score():
    deck = _deck()
    withheld = ("suppressed (block degraded)", "suppressed")
    said = [deck[name]["entries"][key] for name in ("band.oneliner", "splash.band_blurb")
            for key in (*withheld, "fallback")]
    said += [row["template"] for row in deck["verdict.lead"]["items"] if row["band"] in withheld]
    assert len(said) == 8
    assert not [t for t in said if re.search(r"not scored", t, re.I)], said


def test_the_editorial_line_does_not_say_twice_daily():
    assert "twice daily" not in _deck()["live_backfill.editorial_line"]["text"]


def test_the_s5_gloss_describes_the_reading_of_two_years_ago():
    s5 = _deck()["reg.s5"]
    assert "two years ago" in s5["entries"]["plain"]
    said = " ".join(s5["entries"].values())
    assert not re.search(r"25-year|as of \w+ 20\d\d|spreads (are|were) ", said, re.I), said
    assert "as_of" not in s5
