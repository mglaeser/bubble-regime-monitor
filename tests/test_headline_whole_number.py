"""The whole-number headline never names a band line the score has not reached.

The band is decided on the unrounded median, at or above the artifact's lines
(aggregate.action_band). The nearest whole number read 60 beside "trim" for a
median of 59.698; the whole points reached read 59.
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


@pytest.mark.parametrize("median", [0.0, 44.4, 44.5, 44.99, 45.0, 45.5, 59.49, 59.5, 59.698,
                                    59.996, 60.0, 60.14, 60.5, 69.99, 70.0, 99.99, 100.0])
def test_the_headline_is_at_or_above_a_line_exactly_when_the_median_is(median):
    shown = headline(median)
    assert type(shown) is int
    for line in LINES:
        assert (shown >= line) == (median >= line), (median, line)
    assert action_band(shown) == action_band(median)
    assert 0 <= median - shown < 1


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


@pytest.mark.parametrize("earlier, shown, change", [(60.144, 60, -1), (58.6, 58, 1)])
def test_an_earlier_headline_is_its_whole_points_and_the_change_their_difference(earlier, shown, change):
    """A day and a week earlier alike, against today's 59.996 (shown as 59):
    the earlier headline is its own whole points, the change their difference."""
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


def test_a_median_inside_its_range_is_shown_inside_it(monkeypatch):
    """Median 48.9 in the range 48.6 to 52.3: with the ends rounded to the
    nearest the digest read "48/100 trim. range 49-52."."""
    snap = _snapshot(48.9)
    snap.iqr_lo, snap.iqr_hi, snap.band5, snap.band95 = 48.6, 52.3, 48.55, 55.0
    assert "48/100 trim. range 48-52." in deterministic_report(snap, 320)
    facts = digest.digest_facts(snap)
    assert facts["iqr_lo"] <= facts["median"] <= facts["iqr_hi"]
    assert facts["range_lo"] <= facts["median"] <= facts["range_hi"]
    prompts: list[str] = []
    monkeypatch.setattr(judgment, "run_completion", lambda prompt: prompts.append(prompt) or "A note.")
    judgment.generate(48.9, (48.6, 52.3), "trim", {}, {}, 1.0, {}, False, "IN", "IN", {})
    assert "Headline: 48 out of 100 (rough range 48-52)" in prompts[0]
