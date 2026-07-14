"""v3.3.0: rescale-then-aggregate + governance-hygiene regression tests."""

from __future__ import annotations

import pytest


class TestRescaleAggregation:
    def test_rescale_maps_to_floor_range(self):
        from app.engine.aggregate import RESCALE_FLOOR, rescale

        assert rescale(0.0) == pytest.approx(RESCALE_FLOOR)
        assert rescale(1.0) == pytest.approx(1.0)

    def test_zero_never_annihilates_block(self):
        # Defect-2 acceptance: exactly one input at 0 must never collapse the
        # block to 0 — it yields L^{w} of the all-else-equal value.
        from app.engine.aggregate import RESCALE_FLOOR, geometric_block

        w = {"a": 0.5, "b": 0.5}
        one_zero = geometric_block({"a": 0.0, "b": 1.0}, w)
        assert one_zero == pytest.approx(RESCALE_FLOOR**0.5, abs=1e-9)  # ~0.316, not 0
        all_zero = geometric_block({"a": 0.0, "b": 0.0}, w)
        assert all_zero == pytest.approx(RESCALE_FLOOR, abs=1e-9)       # block floor, not 0


class TestJudgmentCompletionGuard:
    def test_fragment_gets_terminal_punctuation(self):
        from app.engine.judgment import _clean_completion

        out = _clean_completion("Valuations are stretched but breadth is holding, leaving the band at")
        assert out[-1] in ".!?"

    def test_clean_sentence_passes_through(self):
        from app.engine.judgment import _clean_completion

        s = "Shares look pricey; broad participation is the main calming factor."
        assert _clean_completion(s) == s

    def test_multi_sentence_trims_to_boundary(self):
        from app.engine.judgment import _clean_completion

        long = ("A" * 250) + ". " + ("B" * 100) + " tail without a period"
        out = _clean_completion(long, 300)
        assert out[-1] in ".!?"
        assert len(out) <= 300


class TestVrpUnits:
    def test_fast_alarm_exposes_units_and_sanity(self):
        from app.engine.legs import fast_alarm

        # ~0.8%/day alternating moves -> realistic ~13% annualized realized vol,
        # so VRP lands in the sane band (a 0-vol series would legitimately flag).
        closes = [100.0]
        for i in range(30):
            closes.append(closes[-1] * (1.008 if i % 2 == 0 else 0.992))
        fa = fast_alarm("contango", 14.0, closes, 128.0).as_dict()
        assert fa["vrp_units"] == "annualized_variance_pts_pct2"
        assert fa["vrp_sane"] is True
