"""The served changelog says where each number it quotes was measured.

The v3.9.0 entry (GET /api/v1/meta/methodology, the status page, the README)
quotes "53.30 -> 51.82". That is the pipeline's point score on the test suite's
golden raw inputs with the endpoint statistic measured for the release put in
(1.1315 against cv90 1.1393), contested rule on against off; the second test
below recomputes it. It was not the production headline at release (51.41
before, 51.51 after), so the entry may not call it live.

Production, the stored snapshots (615, 2026-07-11 .. 2026-11-10):

  - id 144, 2026-08-23 18:01 UTC, service 3.8.0: median 51.41, s4 0.25;
  - id 145, 2026-08-23 22:02 UTC, the first of 3.9.0: median 51.51, s4 0.25.
    Its endpoint statistic was 1.2681 against cv90 1.1425, a rejection, and a
    contested rejection stays capped at 0.25 under the new rule. The release
    did not move s4; +0.10 on the median is the other inputs.
  - the 38 snapshots of 2026-08-19 .. 2026-08-26: medians 49.65 .. 53.12.
  - s4 under 3.9.0: 0.25 on 428 snapshots, 0.05 on 43. The first 0.05 is id
    567, 2026-11-02 22:01 UTC; the README's changelog entry on completed months is about those.
  - replay of the 471 snapshots of 3.9.0 from their stored sub-scores (the
    recomputed point score equals the stored one on all 471): s4 at 0.05
    against 0.25 changes the point score by -1.32 .. -1.98, mean -1.54.

No database exists in CI, so the production numbers stand in the entry and
here. What the tests hold to the engine is the fixture measurement and that the
quoted span is the size of what the rule moves.
"""

from __future__ import annotations

from pathlib import Path

from app.engine.aggregate import RedFlags, deterministic_score
from app.engine.montecarlo import BASE_WEIGHTS_D, BASE_WEIGHTS_S
from app.indicators import s4_gsadf
from app.references import CHANGELOG
from app.services import compute
from tests.conftest import GOLDEN_SUB_D, GOLDEN_SUB_S, make_golden_raw_inputs

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

# The endpoint BSADF and its critical values measured for the release, and the
# GSADF sup the service reported then (the triples of tests/test_s4_real_index.py).
RELEASE_BSADF, RELEASE_BSADF_CV90, RELEASE_BSADF_CV95 = 1.1315, 1.1393, 1.378
RELEASE_SUP, RELEASE_SUP_CV90, RELEASE_SUP_CV95 = 1.579, 1.9359, 2.2215

# What s4 at 0.05 instead of 0.25 takes off the point score on the 471 stored
# snapshots of v3.9.0 (the replay in the module docstring).
SPAN_LOW, SPAN_HIGH = 1.32, 1.98


def _entry() -> dict[str, str]:
    return next(e for e in CHANGELOG if e["version"] == "v3.9.0")


def _readme_bullet() -> str:
    return next(line for line in README.splitlines() if line.startswith("- **v3.9.0"))


def _raw_with_the_release_statistic() -> compute.RawInputs:
    raw = make_golden_raw_inputs()
    raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = (
        RELEASE_SUP, RELEASE_SUP_CV90, RELEASE_SUP_CV95)
    raw.bsadf_stat, raw.bsadf_cv90, raw.bsadf_cv95 = (
        RELEASE_BSADF, RELEASE_BSADF_CV90, RELEASE_BSADF_CV95)
    return raw


def test_v39_entry_does_not_call_the_fixture_measurement_live():
    score, notes, bullet = _entry()["score"], _entry()["notes"], _readme_bullet()
    assert "live headline" not in score
    assert "golden raw inputs" in score
    assert "no step at release" in score
    assert "the live endpoint" not in notes
    assert "51.82 live" not in bullet
    assert "live margin" not in bullet
    assert "golden raw inputs" in bullet
    assert "no step in production at release" in bullet


def test_the_delta_the_entry_quotes_is_the_fixture_measurement(monkeypatch):
    on = compute.compute_snapshot(_raw_with_the_release_statistic())
    monkeypatch.setattr(s4_gsadf, "CONTESTED_RULE", "symmetric")
    monkeypatch.setattr(s4_gsadf, "ASYMMETRIC_CONTESTED", False)
    off = compute.compute_snapshot(_raw_with_the_release_statistic())

    assert (off.indicators["s4"].sub_score, on.indicators["s4"].sub_score) == (0.25, 0.05)
    assert off.action_band == on.action_band == "trim"
    score = _entry()["score"]
    assert f"{off.point_score:.2f} -> {on.point_score:.2f}" in score      # 53.30 -> 51.82
    assert f"delta {on.point_score - off.point_score:.2f}" in score        # delta -1.48
    assert f"{off.point_score:.2f} → {on.point_score:.2f}" in _readme_bullet()


def test_the_span_the_entry_quotes_is_what_the_rule_can_move():
    def point(s4: float) -> float:
        return deterministic_score({**GOLDEN_SUB_S, "s4": s4}, dict(GOLDEN_SUB_D), 1.0, RedFlags(),
                                   BASE_WEIGHTS_S, BASE_WEIGHTS_D).score

    # The golden sub-scores with the other eight held: 52.43 -> 50.97, -1.46.
    drop = point(s4_gsadf.SUB_CONTESTED_OR_STALE) - point(s4_gsadf.SUB_NULL)
    assert round(drop, 2) == 1.46
    assert SPAN_LOW <= drop <= SPAN_HIGH
    assert f"{SPAN_LOW} to {SPAN_HIGH} points" in _entry()["score"]
    assert f"{SPAN_LOW:.1f} to {SPAN_HIGH:.1f} points" in _readme_bullet()
