"""The reasons the composite gives for itself are what the engine does.

Four statements in the engine's docstrings, the science audit and the README
gave a reason or a consequence the code does not bear out: that the geometric
mean keeps a calm indicator from offsetting an alarming one, that the band
edges follow a fitted loss, that the override needs three of four flags
without saying the first cannot fire, and that a breadth reading from a
handful of names degrades Block D. Each case states the engine fact and holds
the sentence to it.

Production, the 615 snapshots stored from 2026-07-11 to 2026-11-10: the first
red flag was BLOCKED on all 490 snapshots that carry the typed flag contract
(from 2026-08-20), `override_required_count` and
`override_fireable_universe_count` read 3 and 3 on those 490, breadth was the
only flag ever active (176 snapshots) and the override fired on none of the
615. Under the quality-weighted gate breadth resolved 490 to 497 of 503 to 505
names on the 604 readings whose note carries both counts (quality 0.972 to
0.986); the one thin reading (33 names, 2026-07-12) is older than the gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import methodology
from app.engine import aggregate
from app.engine.montecarlo import BASE_WEIGHTS_D
from app.references import KNOWN_ISSUES, REGISTRY

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def test_a_calm_reading_pulls_the_block_below_the_weighted_mean():
    # A weighted geometric mean is never above the weighted arithmetic mean of
    # the same values. With high = alarming, a calm reading therefore weighs
    # more than under the linear form, not less.
    doc = _flat(aggregate.__doc__)
    expected = {(1.0, 0.0): (0.224, 0.415), (0.0, 1.0): (0.447, 0.685)}
    for (d1, others), (geometric, linear) in expected.items():
        subs = {key: d1 if key == "d1" else others for key in BASE_WEIGHTS_D}
        block = aggregate.geometric_block(subs, BASE_WEIGHTS_D)
        mean = sum(w * aggregate.rescale(subs[key]) for key, w in BASE_WEIGHTS_D.items())
        assert block == pytest.approx(geometric, abs=1e-3)
        assert mean == pytest.approx(linear, abs=1e-3)
        assert block < mean
        assert f"{block:.3f} against {mean:.3f}" in doc, (
            f"the module docstring does not quote {block:.3f} against {mean:.3f}")


def test_the_docstring_claims_no_protection_against_masking():
    # The paragraph on why the mean is geometric said linear aggregation lets
    # "a calm indicator ... fully offset an alarming one", as if the geometric
    # form prevented it. One alarming indicator among calm ones is held down
    # further by the geometric form (the case above).
    doc = _flat(aggregate.__doc__)
    assert "a calm indicator can fully offset an alarming one" not in doc
    assert "NOT protected from being offset by calm ones" in doc


def test_the_band_edges_are_called_judgmental():
    # The edges are two constants of the frozen artifact. No loss function, no
    # labelled episodes and no alternative edge exist in the repository.
    trim = methodology.get_path("action_bands", "trim_at_or_above")
    derisk = methodology.get_path("action_bands", "derisk_at_or_above")
    assert [aggregate.action_band(score) for score in (trim - 0.01, trim, derisk - 0.01, derisk)] \
        == ["hold", "trim", "trim", "de-risk"]

    doc = _flat(aggregate.action_band.__doc__)
    assert "theta" not in doc
    assert "judgmental" in doc

    how_to_read = README[README.index("## How to read the score"):README.index("## Golden fixture")]
    assert "loss with θ" not in how_to_read
    assert "judgmental cut-offs" in how_to_read

    anchors = next(issue for issue in KNOWN_ISSUES if issue["id"] == "judgmental-anchors")
    assert f"the action-band edges ({trim:g} and {derisk:g})" in anchors["detail"]


def test_the_override_needs_all_three_other_flags_while_s4_is_contested():
    from app.config import Settings
    from app.engine.snapshot_contract import FLAG_BLOCKED, build_red_flag_meta

    assert Settings.model_fields["gsadf_contested"].default is True

    # Every flag's condition met, the first one contested (the default).
    firing = {
        "gsadf_explosive_p05": True, "gsadf_contested": True, "semi_runup_pp": 200.0,
        "hy_oas_bps": 500.0, "hy_oas_3yr_tight_bps": 300.0, "breadth_pct": 40.0,
        "index_within_2pct_of_ath": True,
    }
    flags = aggregate.evaluate_red_flags(**firing)
    assert flags.gsadf_explosive_noncontested is False
    assert flags.count == 3 and flags.override_fired

    # One of the other three calm, and the override does not fire.
    for calm in ({"semi_runup_pp": 100.0}, {"hy_oas_bps": 350.0}, {"breadth_pct": 60.0}):
        assert not aggregate.evaluate_red_flags(**{**firing, **calm}).override_fired

    # The snapshot's own counts say the same: three required, three that can fire.
    meta = build_red_flag_meta(
        red_flags=flags, observed_at="2026-11-10T22:00:00+00:00",
        gsadf_stat=1.5, gsadf_cv95=1.4, gsadf_contested=True, gsadf_available=True,
        gsadf_as_of="2026-10-31", gsadf_stale=False,
        semi_runup_pp=200.0, semis_as_of="2026-11-10", semis_stale=False,
        hy_oas_bps=500.0, hy_oas_tight_bps=300.0, hy_oas_as_of="2026-11-10", hy_oas_stale=False,
        breadth_pct=40.0, breadth_as_of="2026-11-10", breadth_stale=False,
        near_ath_available=True, near_ath_state=True)
    assert meta["flags"]["rf1"]["state"] == FLAG_BLOCKED
    assert (meta["override_required_count"], meta["override_fireable_universe_count"]) == (3, 3)

    assert "needs the other three at once" in _flat(aggregate.__doc__)
    assert "needs the other three at once" in README
    # The README names the three by the frozen artifact's thresholds.
    semis = methodology.get_path("red_flags", "semi_runup_ge_pp")
    widening = methodology.get_path("red_flags", "hy_oas_widen_gt_bps")
    breadth = methodology.get_path("red_flags", "breadth_lt_pct")
    assert (f"the semiconductor run-up at {semis:g} pp or more, HY OAS more than {widening:g} "
            f"bps above its three-year low, and breadth below {breadth:g}%") in README


def test_a_thin_breadth_universe_alone_never_degrades_block_d():
    from app.services.compute import COVERAGE_DROP_THRESHOLD, IndicatorOutput, _coverage_gate
    from app.sources.breadth import MIN_RESOLVED

    today = datetime.now(UTC).date().isoformat()

    def block_d(d1_quality: float, lost: tuple[str, ...] = ()) -> dict:
        indicators = {
            key: IndicatorOutput(key, None, None, True, "x", False) if key in lost
            else IndicatorOutput(key, 1.0, 0.5, False, "x", False, as_of=today)
            for key in REGISTRY if key not in ("d1", "v")
        }
        indicators["d1"] = IndicatorOutput("d1", 56.0, 0.6, False, "x", False,
                                           quality=d1_quality, as_of=today)
        return _coverage_gate(indicators)["D"]

    # Breadth is published from MIN_RESOLVED names up. At that floor, with the
    # eight other indicators at full quality, Block D is not degraded.
    assert MIN_RESOLVED == 25
    assert block_d(MIN_RESOLVED / 504) == {"coverage": 0.6674, "degraded": False}

    # The quality the discount alone would need is below the publishing floor.
    weight = REGISTRY["d1"].weight
    needed = ((1.0 - COVERAGE_DROP_THRESHOLD) - (1.0 - weight)) / weight
    assert needed < MIN_RESOLVED / 504
    assert block_d((MIN_RESOLVED - 2) / 504)["degraded"] is True

    # What does degrade the block: a thin universe together with another lost
    # indicator, or d1 lost altogether.
    assert block_d(33 / 503, lost=("d4",)) == {"coverage": 0.473, "degraded": True}
    d1_lost = _coverage_gate({
        key: IndicatorOutput(key, None, None, True, "x", False, quality=0.0) if key == "d1"
        else IndicatorOutput(key, 1.0, 0.5, False, "x", False, as_of=today)
        for key in REGISTRY if key != "v"})["D"]
    assert d1_lost == {"coverage": 0.65, "degraded": True}

    doc = _flat(_coverage_gate.__doc__)
    assert "handful of names now correctly degrades" not in doc
    assert f"a quality below {needed:.1%}" in doc
    assert "coverage 0.6674, not degraded" in doc
