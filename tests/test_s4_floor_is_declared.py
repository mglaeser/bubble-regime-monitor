"""A failed S4 keeps a declared value, and the two texts that state the rule say so.

AGENTS.md rule 4 reads "Never a neutral placeholder for a failed indicator.
Drop it and renormalize its block." Eight of the nine block indicators do
exactly that (the matrix in tests/test_degradation.py holds each of them to
the drop). S4 does not: an S4 whose GSADF run yields no usable statistic
(the statistic or a critical value missing, not finite, or the two critical
values out of order) keeps the methodology's 0.25 in Block S, as state FLOOR
with quality 0.0, labelled `imputation: missing_to_contested_floor`. The
value is declared, not improvised: `indicators.s4.sub_contested_or_stale` in
frozen_methodology.json, "data-missing -> 0.25" in the registry's S4 entry
(app/references.py), and the S4 row of the README's golden table ("no GSADF
input (data-missing)", 0.25). The rule is older than AGENTS.md: the
changelog's v3.0.1 entry records it ("GSADF data-missing now floors at the
contested/stale 0.25"), and the first stored snapshot carries it. The golden
fixture supplies no GSADF input, so its 52.43 is built on this value.

Two texts state the rule and are held to it here. AGENTS.md is what a
cold-start agent reads before anything else: an agent that followed the bare
rule to the letter would drop S4 and move the golden fixture, so rule 4
names the exception and what removing it costs. Section 11 of
docs/frozen_pin_manifest.md lists what a FLOOR does to the geometric mean:
a D4 FLOOR is excluded, an S4 FLOOR stays (section 10 of the same file gives
its sub-score as 0.25).

Two neighbours are not exceptions. V is not a block indicator: it multiplies
Block D, and without a ratio the multiplier is 1.0, which leaves Block D as
computed (tests/test_degradation.py::test_vix_unavailable_neutral_multiplier).
S1 without a CAPE history has not failed: CAPE and the real yield are both
read, the percentile half takes the step the artifact declares
(`indicators.s1.no_history_shim_*`: 0.99 above a CAPE of 35, else 0.5) and
the reading counts at quality 0.5. With CAPE or the real yield missing S1 is
dropped like the others.

The numbers:
- Golden sub-scores, S4 kept at 0.25: point score 52.4282. S4 dropped and
  Block S renormalized over the other four: 54.0910 (+1.6628). These are the
  two figures rule 4 quotes; the tests below compute them.
- Production (615 stored snapshots, 2026-07-11 to 2026-11-10): six had no
  statistic, all between 2026-07-11 and 2026-07-15. Each stored S4 at 0.25,
  not dropped, with its weight of 0.07. The other 609 carry a statistic. The
  four of the six computed under the present aggregation (2026-07-14 and
  2026-07-15) are reproduced exactly from their stored sub-scores; with S4
  dropped their point scores would have been 1.11 to 1.12 higher, the band
  `trim` either way. The S1 step above occurred on none of the 615.

The sentence in rule 4 records what the code, the artifact and the fixture
already do; it decides nothing. Dropping a failed S4 instead is a methodology
change (rule 6: the golden fixture moves by +1.66), not a correction of the
code to the rule.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MC_SEED = 20260711


def _rule_4() -> str:
    """Rule 4 of AGENTS.md: the one list item that starts with its title."""
    lines = (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
    found = [ln for ln in lines if ln.startswith("4. **Never a neutral placeholder")]
    assert len(found) == 1, "AGENTS.md rule 4 is not where this test looks for it"
    return found[0]


def _golden_point_scores() -> tuple[float, float]:
    """The golden point score with S4 at its fixture value, and with S4
    dropped and Block S renormalized over the other four."""
    from app.engine.aggregate import RedFlags, deterministic_score
    from app.engine.montecarlo import BASE_WEIGHTS_D, BASE_WEIGHTS_S
    from tests.conftest import GOLDEN_SUB_D, GOLDEN_SUB_S

    def point(sub_s: dict[str, float]) -> float:
        return deterministic_score(dict(sub_s), dict(GOLDEN_SUB_D), 1.0, RedFlags(),
                                   BASE_WEIGHTS_S, BASE_WEIGHTS_D).score

    without_s4 = {k: v for k, v in GOLDEN_SUB_S.items() if k != "s4"}
    return point(GOLDEN_SUB_S), point(without_s4)


def test_a_failed_s4_keeps_the_declared_floor_in_the_mean(isolated_db):
    """What the code does, in rule 4's words: the value stays in Block S, the
    row says nothing was measured, and the coverage gate counts S4's weight
    as lost."""
    from app import methodology
    from app.engine.aggregate import geometric_block, renormalize
    from app.engine.montecarlo import BASE_WEIGHTS_S
    from app.services.compute import _coverage_gate, compute_snapshot
    from tests.conftest import GOLDEN_SUB_S, make_golden_raw_inputs

    floor = methodology.get_path("indicators", "s4", "sub_contested_or_stale")
    assert floor == 0.25
    assert GOLDEN_SUB_S["s4"] == floor

    raw = make_golden_raw_inputs()
    assert raw.bsadf_stat is None and raw.gsadf_stat is None   # no GSADF input
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=MC_SEED)

    s4 = data.indicators["s4"]
    assert s4.state == "FLOOR"
    assert s4.quality == 0.0
    assert s4.dropped is False
    assert s4.sub_score == floor
    assert s4.payload()["imputation"] == "missing_to_contested_floor"

    # In the mean: Block S is the geometric mean over all five sub-scores at
    # their nominal weights, and lower than it would be without S4.
    sub_s = {k: ind.sub_score for k, ind in data.indicators.items() if k.startswith("s")}
    assert set(sub_s) == set(BASE_WEIGHTS_S)
    assert data.s_block == pytest.approx(geometric_block(sub_s, BASE_WEIGHTS_S))
    without_s4 = {k: v for k, v in sub_s.items() if k != "s4"}
    assert data.s_block < geometric_block(
        without_s4, renormalize(BASE_WEIGHTS_S, set(without_s4)))

    # Lost for the coverage gate: counted as a measured reading, S4 would add
    # its whole weight of 0.07 to what Block S obtains.
    assert BASE_WEIGHTS_S["s4"] == pytest.approx(0.07)
    counted = dict(data.indicators, s4=dataclasses.replace(s4, quality=1.0))
    gained = _coverage_gate(counted)["S"]["coverage"] - data.coverage["S"]["coverage"]
    assert gained == pytest.approx(BASE_WEIGHTS_S["s4"], abs=1e-4)

    kept, dropped = _golden_point_scores()
    assert kept == pytest.approx(52.43, abs=0.01)
    assert dropped == pytest.approx(54.09, abs=0.01)


def test_agents_rule_4_names_the_exception():
    """The rule still commands the drop, and names the one indicator that
    keeps a value, where the value comes from, how the row is marked, and what
    dropping it instead would do to the golden fixture (the engine's numbers,
    so a fixture that moves takes the sentence with it)."""
    rule = _rule_4()
    assert "Drop it and renormalize its block" in rule

    for needed in ("S4", "0.25", "`indicators.s4.sub_contested_or_stale`",
                   "`FLOOR`", "quality 0.0", "`imputation: missing_to_contested_floor`"):
        assert needed in rule, f"rule 4 does not name {needed}"

    kept, dropped = _golden_point_scores()
    assert f"{kept:.2f}" in rule, f"rule 4 does not quote the golden point score {kept:.2f}"
    assert f"{dropped:.2f}" in rule, (
        f"rule 4 does not quote the golden point score without S4, {dropped:.2f}")


def test_the_manifest_does_not_say_s4_floor_is_excluded():
    """Section 11 of the pin manifest: an S4 FLOOR stays in the geometric
    mean; only a D4 FLOOR is excluded."""
    lines = (ROOT / "docs" / "frozen_pin_manifest.md").read_text(encoding="utf-8").splitlines()

    s4_floor = [ln for ln in lines if "s4 FLOOR" in ln]
    assert s4_floor, "the manifest no longer has a line on the S4 FLOOR"
    assert not [ln for ln in s4_floor if "excluded" in ln.lower()]
    assert any("KEEPS 0.25" in ln and "missing_to_contested_floor" in ln for ln in s4_floor)

    d4_floor = [ln for ln in lines if ln.startswith("- d4 FLOOR")]
    assert len(d4_floor) == 1
    assert "EXCLUDED from the geometric mean" in d4_floor[0]
