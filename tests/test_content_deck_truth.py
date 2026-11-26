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


# The wording of the five replaced lines. A pin on them, not a detector of
# advice in free text.
ACTION_COPY = ("de-risk now", "trim now", "is warranted", "consider ")


def test_no_verdict_or_band_line_tells_the_reader_to_act():
    deck = _deck()
    said = [row["template"] for name in ("verdict.lead", "verdict.detail")
            for row in deck[name]["items"]]
    said += [text for name in ("band.oneliner", "splash.band_blurb")
             for text in deck[name]["entries"].values()]
    assert [t for t in said if any(p in t.lower() for p in ACTION_COPY)] == []


def test_the_changelog_reaches_every_score_shifting_release():
    from app.references import CHANGELOG

    # A release whose served changelog entry opens its score field with one of
    # these marks has a row in the deck's table.
    marks = ("METHODOLOGY CHANGE", "D4 METHOD CHANGE", "SCORE-SHIFTING")
    shifting = [entry["version"] for entry in CHANGELOG if entry["score"].startswith(marks)]
    assert {"v3.3.0", "v3.3.2", "v3.9.0"} <= set(shifting)   # the marks select them

    deck = _deck()
    shown = {row["v"].removeprefix("v") for row in deck["changelog"]["items"]}
    assert [version for version in shifting if version.removeprefix("v") not in shown] == []


def test_the_override_line_says_what_fires_while_s4_is_contested():
    from app.config import Settings
    from app.engine.aggregate import evaluate_red_flags

    # What the service does: GSADF_CONTESTED is true by default, a contested
    # GSADF flag does not fire whatever the statistic reads, and the override
    # needs three flags. So it fires only when the three other flags fire
    # together; one of them calm and it does not.
    assert Settings.model_fields["gsadf_contested"].default is True
    firing = {"gsadf_explosive_p05": True, "gsadf_contested": True, "semi_runup_pp": 200.0,
              "hy_oas_bps": 500.0, "hy_oas_3yr_tight_bps": 300.0, "breadth_pct": 40.0,
              "index_within_2pct_of_ath": True}
    flags = evaluate_red_flags(**firing)
    assert not flags.gsadf_explosive_noncontested
    assert flags.override_fired
    for calm in ({"semi_runup_pp": 100.0}, {"hy_oas_bps": 350.0}, {"breadth_pct": 60.0}):
        assert not evaluate_red_flags(**{**firing, **calm}).override_fired

    assert "all three others" in _deck()["redflag.header"]["text"]
