"""The display deck that rides GET /api/v1/score says what the service does.

Each case is written from a statement the service makes elsewhere, not from
the artifact: guardrail 1 (app/references.py: the headline "is uncalibrated"),
the score route (a snapshot whose band is withheld still publishes
headline_median), the recompute schedule (app/engine/recompute_slots.py, six
slots a day) and the S5 method (REGISTRY["s5"].how: the excess bond premium
of two years ago).

Executed on a copy of the production database, 2026-11-11: 53 of 615
snapshots carry the band "suppressed (block degraded)", and each of the 53
has a median (47.5 to 60.0). S5 went from 0.52 to 0.85 on 2026-11-05, when
the premium it reads (24 monthly prints back) moved from -0.06 to -0.33 pp;
the high-yield spread stood at 332 to 348 bps that week, 73 to 89 bps above
its low (259 bps, January 2025) in the history the service keeps since July
2023. 601 of the 615 S5 readings come from the excess bond premium.

The changelog case and the override case are about the deck's version table
and the red-flag header. The table's tip says it is shown "so you never
mistake a methodology change for a market event"; the table ended at 3.1.0,
before the three releases the served changelog (app/references.py, CHANGELOG)
marks as moving the score. What each of the three rows states, in the same 615
snapshots:

  - 3.3.0: id 10 (2026-07-14 06:10 UTC, service 3.2.0) median 32.83, band
    hold; id 11 (15:12 UTC, 3.3.0) median 45.46, band trim. The raw readings
    of breadth, LPPLS and the semis run-up are identical in the two runs. The
    aggregation of 3.3.0 on id 10's own sub-scores gives a point score of
    44.69 against the stored 32.70: 11.99 of the step's 12.67 points.
  - 3.3.2: id 14 (2026-07-15 18:11 UTC, 3.3.0) median 48.84; id 15 (22:25
    UTC, 3.3.2) median 51.85. No snapshot carries 3.3.1: its move of S5 to the
    excess bond premium went live in the same deploy. Of the point score's
    +3.01 (48.88 to 51.89), D4 alone (0.20 to 0.27) is +1.05, S5 alone (0.21
    on the high-yield spread tier to 0.45 on the excess bond premium) +1.77
    and S3 +0.17, so the row names D4 and S5.
  - 3.9.0: id 144 (2026-08-23 18:01 UTC, 3.8.0) median 51.41; id 145 (22:02
    UTC, 3.9.0) median 51.51, S4 at 0.25 in both. On the 471 snapshots of
    3.9.0, replayed from their stored sub-scores (the recomputed point score
    equals the stored one on all 471), S4 at 0.05 instead of 0.25 lowers the
    point score by 1.32 to 1.98 points, mean 1.54.

The header said "If at least 3 of 4 fire" and no more. The GSADF flag was
BLOCKED (not fireable) on all 490 snapshots that carry the typed flag
contract, each with override_required_count 3 and
override_fireable_universe_count 3. The override fired on none of the 615,
and at most one flag was active at a time (breadth, on 176).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import content_registry
from app.engine.snapshot_contract import STATE_SUPPRESSED


@pytest.fixture()
def client(isolated_db, monkeypatch):
    import app.scheduler as scheduler
    import app.services.backfill as backfill

    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown", lambda: None)
    monkeypatch.setattr(backfill, "seed_hy_oas_history", lambda: 0)
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _strings(node):
    """Every text a block serves, whatever its kind (not its source pointer)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if key != "source":
                yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _deck() -> dict:
    content_registry._clear_artifact_cache()
    return content_registry.gauge_display()


def test_no_deck_text_calls_the_heuristic_calibrated():
    # Guardrail 1: "it is uncalibrated". The chip beside the headline said
    # "calibrated on n≈4". \b keeps "uncalibrated" legal.
    assert [t for t in _strings(_deck()) if re.search(r"\bcalibrated\b", t, re.I)] == []


def test_a_withheld_band_is_not_called_a_missing_score(client):
    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    raw.hyperscalers = None      # drop D3 (0.32)
    raw.lppls_confidence = None  # drop D4 (0.20): more than 1/3 of Block D
    persist_snapshot(compute_snapshot(raw, mc_samples=2_000, mc_seed=1), raw)

    data = client.get("/api/v1/score").json()["data"]
    band = data["action_band"]
    assert band == "suppressed (block degraded)"
    assert isinstance(data["headline_median"], int)      # the score IS published

    # Every deck line this state selects: under the published band word and
    # under the typed state's bare name, which the deck keys as well.
    deck = data["display"]
    said = []
    for key in (band, STATE_SUPPRESSED):
        said += [deck[name]["entries"][key] for name in ("band.oneliner", "splash.band_blurb")]
        said += [row["template"] for name in ("verdict.lead", "verdict.detail")
                 for row in deck[name]["items"] if row["band"] == key]
    assert len(said) == 8
    assert not [t for t in said if re.search(r"not scored", t, re.I)], said
    # The maps' "fallback" entry is the same sentence (what a client shows for
    # a band word it does not know), so it follows.
    for name in ("band.oneliner", "splash.band_blurb"):
        assert deck[name]["entries"]["fallback"] == deck[name]["entries"][band]
    # "unknown" keeps "Not scored": that is the client's own state for a
    # reading it could not classify, not a band this route publishes
    # (tests/test_content_api.py::test_verdict_tables_never_map_unknown_to_hold).


def test_the_deck_states_no_refresh_cadence_of_its_own():
    # The deck said "Refreshes twice daily."; the recompute has six slots a
    # day (recompute_slots.RECOMPUTE_SLOT_HOURS). The schedule has one source,
    # recompute_slots.schedule_display(), which /api/v1/status and the
    # empty-state text derive from; a cadence typed into the deck goes stale
    # with the next schedule change, so the deck states none.
    cadence = re.compile(
        r"\b(refresh\w*|recomput\w*|updat\w*)\b[^.]*\b(daily|a day|hourly|every \d)", re.I)
    assert [t for t in _strings(_deck()) if cadence.search(t)] == []


def test_the_s5_gloss_describes_the_reading_of_two_years_ago():
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    # The code fact (REGISTRY["s5"].how): S5 ranks the excess bond premium of
    # 24 monthly prints back. The high-yield spread of the day is no input to
    # it: at 260 bps or at 600 bps the reading is the same.
    def reading(spread_today: float) -> tuple:
        raw = make_golden_raw_inputs()
        raw.ebp_history = [((i * 37) % 100) / 100 - 0.5 for i in range(120)]
        raw.hy_oas_bps = spread_today
        s5 = compute_snapshot(raw, mc_samples=2_000, mc_seed=1).indicators["s5"]
        return s5.data_source, s5.value, s5.sub_score

    assert reading(260.0) == reading(600.0)
    assert reading(260.0)[0] == "fed_ebp"

    s5 = _deck()["reg.s5"]
    assert "two years ago" in s5["entries"]["plain"]
    # No statement about the spreads of the day, so nothing in it is dated.
    said = " ".join(s5["entries"].values())
    assert not re.search(r"25-year|as of \w+ 20\d\d|spreads (are|were) ", said, re.I), said
    assert "as_of" not in s5


def test_the_changelog_reaches_every_score_shifting_release():
    from app.references import CHANGELOG

    # The served changelog (GET /api/v1/meta/methodology) opens the `score`
    # field of a release that moved the score with one of these marks. The
    # deck's table says why it is shown (changelog.tip), so it has a row for
    # each of them; a release marked this way later needs its row too.
    marks = ("METHODOLOGY CHANGE", "D4 METHOD CHANGE", "SCORE-SHIFTING")
    shifting = [entry["version"] for entry in CHANGELOG if entry["score"].startswith(marks)]
    assert {"v3.3.0", "v3.3.2", "v3.9.0"} <= set(shifting)   # the marks select them

    deck = _deck()
    assert "never mistake a methodology change for a market event" in deck["changelog.tip"]["text"]
    shown = {row["v"].removeprefix("v") for row in deck["changelog"]["items"]}
    assert [version for version in shifting if version.removeprefix("v") not in shown] == []


def test_the_override_line_says_what_fires_while_s4_is_contested():
    from app.config import Settings
    from app.engine.aggregate import evaluate_red_flags

    # What the service does: GSADF_CONTESTED is true unless the operator sets
    # it, a contested GSADF flag does not fire whatever the statistic reads,
    # and the override needs three flags. So it fires only when the three
    # other flags fire together; one of them calm and it does not.
    assert Settings.model_fields["gsadf_contested"].default is True
    firing = {"gsadf_explosive_p05": True, "gsadf_contested": True, "semi_runup_pp": 200.0,
              "hy_oas_bps": 500.0, "hy_oas_3yr_tight_bps": 300.0, "breadth_pct": 40.0,
              "index_within_2pct_of_ath": True}
    flags = evaluate_red_flags(**firing)
    assert not flags.gsadf_explosive_noncontested
    assert flags.override_fired
    for calm in ({"semi_runup_pp": 100.0}, {"hy_oas_bps": 350.0}, {"breadth_pct": 60.0}):
        assert not evaluate_red_flags(**{**firing, **calm}).override_fired

    # The header states the rule (3 of 4); it must also say what that needs
    # under the standing setting.
    assert "all three others" in _deck()["redflag.header"]["text"]
