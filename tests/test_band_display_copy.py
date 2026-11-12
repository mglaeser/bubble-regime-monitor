"""Every band the score can publish is a word its readers have copy for: the
display deck that rides the same /score reply (Q8: the copy rides the data it
explains; content_registry: "an unrecognized band must never fall through to
HOLD action copy") and the daily digest's word list (composer.WORDS).

Written from the README's bands ("<45 hold, 45-60 trim, >=60/override
de-risk"), the coverage rule (frozen_methodology.json `coverage`: a block that
kept less than two thirds of its weight is degraded, and its band is
suppressed) and the v3.7.3 rule A-03 (a fired override wins the band even on
degraded data).

A-03's decision is unchanged; this pins the word it is published as. Until
2026-11 that word was "de-risk (data degraded)": no entry in the deck's
band one-liner, splash blurb or either verdict table, and a band the
companion dashboard does not know is drawn as HOLD. The band reads "de-risk",
and the degradation is served beside it (meta.coverage, asserted below).
Production never stored the old word: 0 of 615 snapshots (2026-07-11 to
2026-11-10), on none of which the override fired. The wording is the owner's
call, taken by merging the change. The old word stays where it is history:
the v3.7.3 changelog entries (README, app/references.py) and migration 0007,
which maps the rows written before it (tests/test_alert_snapshot_contract.py)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# (override fired, a block degraded) -> the published band; the golden inputs
# score 52, a trim.
STATES = [
    (False, False, "trim"),
    (False, True, "suppressed (block degraded)"),
    (True, False, "de-risk"),
    (True, True, "de-risk"),
]


def _published_score(*, override: bool, degraded: bool) -> dict:
    """Compute and store a snapshot in that state; return what /score serves."""
    from app.main import create_app
    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    if override:  # flags 2, 3 and 4 (flag 1 is blocked while GSADF is contested)
        raw.smh_2yr_return_pct, raw.spy_2yr_return_pct = 200.0, 30.0
        raw.hy_oas_bps, raw.hy_oas_history_bps = 500.0, [200.0] * 800
        raw.breadth_pct, raw.index_within_2pct_of_ath = 40.0, True
    if degraded:  # D3 (0.32) and D4 (0.20): more than a third of block D
        raw.hyperscalers = None
        raw.lppls_confidence = None
    persist_snapshot(compute_snapshot(raw, mc_samples=2_000, mc_seed=1), raw)
    body: dict = TestClient(create_app()).get("/api/v1/score").json()
    return body


@pytest.mark.parametrize("override, degraded, expected", STATES)
def test_the_published_band_has_copy_in_every_band_keyed_block(isolated_db, override, degraded, expected):
    body = _published_score(override=override, degraded=degraded)
    band, deck = body["data"]["action_band"], body["data"]["display"]
    for slug in ("band.oneliner", "splash.band_blurb"):
        assert deck[slug]["entries"].get(band), f"{slug} has no copy for {band!r}"
    for slug in ("verdict.lead", "verdict.detail"):
        assert any(row["band"] == band for row in deck[slug]["items"]), f"{slug} has no row for {band!r}"
    assert band == expected
    assert body["data"]["override_fired"] is override
    assert bool(body["meta"]["coverage"]["degraded"]) is degraded  # the degradation is served beside the band


@pytest.mark.parametrize("override, degraded, expected", STATES)
def test_the_published_band_is_a_word_of_the_digest(isolated_db, override, degraded, expected):
    from app.message_engine import composer

    band = _published_score(override=override, degraded=degraded)["data"]["action_band"]
    assert composer.typed("action_band", band) == band == expected


@pytest.mark.parametrize("fact", ["band_1d_ago", "band_7d_ago"])
def test_a_stored_row_with_the_retired_word_reads_as_a_dash(fact):
    """The digest compares today's band with stored ones. Its word list is what
    the score publishes, so the retired word is no band a day or a week
    earlier either: the slot shows a dash, nothing fails. Production holds no
    such row (0 of 615)."""
    from app.message_engine import composer

    retired = "de-risk (data degraded)"
    assert composer.typed(fact, retired) is None
    assert composer.render_fallback(f"band {{{fact}}}.", {fact: retired}) == "band -."
