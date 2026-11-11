"""The whole-number headline never names a band line the score has not reached.

The spec. README, "How to read the score": "The action bands (< 45 hold;
45-60 trim; >= 60 or override -> de-risk)". frozen_methodology.json:
action_bands.trim_at_or_above 45, derisk_at_or_above 60; override.target_score
70. The band is decided on the unrounded median (aggregate.action_band). So a
reader who is shown a whole number and knows the lines must arrive at the band
the monitor shows: the number is at or above a line exactly when the median is.

The nearest whole number does not do that. On the production database of
2026-11-10 (615 snapshots) 47 snapshots were published as "60" while the rule
put them under the 60 line, every one a median from 59.51 to under 60; seven
of the 54 daily digests sent were built on such a snapshot, and two of them
said the score sat "exactly on the de-risk line" (2026-10-06, median 59.996,
and 2026-10-12, median 59.698).

The whole points reached (the floor) do. The price, pinned here as well: the
number shown is one lower than the nearest whole number whenever the fraction
is a half or more (59.7 reads 59), which is 286 of those 615 snapshots.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import methodology as _M
from app.db import session_scope
from app.engine import judgment
from app.engine.aggregate import action_band, headline
from app.engine.sms_report import deterministic_report
from app.models import Snapshot
from app.services import digest

pytestmark = pytest.mark.usefixtures("isolated_db")

#: Every line the published method compares the score with.
LINES = (_M.get_path("action_bands", "trim_at_or_above"),
         _M.get_path("action_bands", "derisk_at_or_above"),
         _M.get_path("override", "target_score"))

T0 = datetime(2026, 10, 12, 6, 1, tzinfo=UTC)


def _snapshot(median: float, *, computed_at: datetime = T0) -> Snapshot:
    """A stored snapshot whose band is the rule's band for `median`."""
    return Snapshot(
        computed_at=computed_at, service_version="3.9.0", median=median, iqr_lo=57.6, iqr_hi=61.5,
        band5=54.9, band95=64.4, point_score=59.6, action_band=action_band(median), override_fired=False,
        red_flag_count=1, red_flag_detail={}, v_multiplier=1.0, v_state="contango",
        block_s={"value": 0.74, "indicators": {}}, block_d={"value": 0.48, "indicators": {}},
        trend_states={"SPY": {"faber_10mo": "IN"}, "QQQ": {"faber_10mo": "IN"}}, fast_alarm={},
        judgment_call=None, judgment_stale=False, data_freshness={}, data_degraded=False)


def test_the_lines_are_whole_numbers():
    """What lets one whole number agree with every line. A line moved to a
    fraction would need another headline rule, and this says so."""
    assert all(float(line).is_integer() for line in LINES)


@pytest.mark.parametrize("median", [0.0, 44.4, 44.5, 44.99, 45.0, 45.5, 59.49, 59.5, 59.698,
                                    59.996, 60.0, 60.14, 60.5, 69.99, 70.0, 99.99, 100.0])
def test_the_headline_is_at_or_above_a_line_exactly_when_the_median_is(median):
    shown = headline(median)
    assert type(shown) is int
    for line in LINES:
        assert (shown >= line) == (median >= line), (median, line)
    assert action_band(shown) == action_band(median)
    assert 0 <= median - shown < 1


def test_a_fraction_of_a_half_or_more_reads_one_lower_than_the_nearest_whole_number():
    """The accepted price of the rule: 59.7 reads 59, 48.861 (the production
    snapshot of 2026-11-10 22:00) reads 48, and a whole score reads itself."""
    assert [headline(m) for m in (59.7, 48.861, 49.378, 52.0)] == [59, 48, 49, 52]


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


def test_the_score_api_and_the_status_page_show_59_beside_trim(client):
    """The median of production snapshot 437 (2026-10-12 06:01), 59.698, band
    trim: both surfaces served 60 beside "trim"."""
    with session_scope() as s:
        s.add(_snapshot(59.698))
    score = client.get("/api/v1/score").json()["data"]
    assert (score["headline_median"], score["action_band"]) == (59, "trim")
    assert client.get("/api/v1/status").json()["snapshot"]["headline_median"] == 59


def test_the_digest_template_shows_59_beside_trim():
    assert deterministic_report(_snapshot(59.698), 160).startswith("bubblegauge 59/100 trim.")


def test_the_digest_facts_put_a_score_under_the_line_under_it():
    """The prompt's line reads "the headline minus each line": with the
    headline 59 the two gaps are 14 and -1, and the model is no longer told
    the score is on a line it is under. A day earlier the score was over the
    line and has fallen through it (the medians of production snapshots 395
    and 401, 60.144 and 59.996), where the facts said "60, change 0"."""
    day = _snapshot(60.144, computed_at=T0 - timedelta(hours=24))
    facts = digest.digest_facts(_snapshot(59.996), day=day)
    assert (facts["median"], facts["action_band"]) == (59, "trim")
    assert (facts["trim_line_gap"], facts["derisk_line_gap"]) == (14, -1)
    assert (facts["median_1d_ago"], facts["band_1d_ago"], facts["median_1d_change"]) == (60, "de-risk", -1)


@pytest.mark.parametrize("earlier, shown, change", [(60.144, 60, -1), (58.6, 58, 1)])
def test_an_earlier_headline_is_its_whole_points_and_the_change_their_difference(earlier, shown, change):
    """A day and a week earlier alike, against today's 59.996 (shown as 59):
    the earlier headline is its own whole points, and the change is the
    difference of the two headlines shown, so the three numbers agree."""
    then = _snapshot(earlier, computed_at=T0 - timedelta(hours=24))
    facts = digest.digest_facts(_snapshot(59.996), day=then, week=then)
    assert (facts["median_1d_ago"], facts["median_1d_change"]) == (shown, change)
    assert (facts["median_7d_ago"], facts["median_7d_change"]) == (shown, change)
    assert facts["median"] - shown == change


def test_the_judgment_is_told_the_same_headline(monkeypatch):
    prompts: list[str] = []
    monkeypatch.setattr(judgment, "run_completion", lambda prompt: prompts.append(prompt) or "A note.")
    judgment.generate(59.698, (57.6, 61.5), "trim", {}, {}, 1.0, {}, False, "IN", "IN", {})
    assert "Headline: 59 out of 100" in prompts[0]
