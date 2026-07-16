"""CNN Fear & Greed adapter (v3.7.0): strict validation per the adopted
OpenAPI spec, and feed integration (metric + series, per-item degradation)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.sources import SourceError
from app.sources import fear_greed as fg

# The example payload from the adopted OpenAPI spec (2026-01-08 capture),
# plus a component block that clients must tolerate (additionalProperties).
SPEC_EXAMPLE = {
    "fear_and_greed": {
        "score": 46,
        "rating": "neutral",
        "timestamp": "2026-01-08T23:19:21+00:00",
        "previous_close": 46,
        "previous_1_week": 44,
        "previous_1_month": 31,
        "previous_1_year": 31,
    },
    "fear_and_greed_historical": {
        "timestamp": 1767914361000,
        "score": 46,
        "rating": "neutral",
        "data": [
            {"x": 1767914361000, "y": 46, "rating": "neutral"},
        ],
    },
    "market_momentum_sp500": {"timestamp": 1767914361000, "score": 40.0,
                              "rating": "fear", "data": []},
}


class FakeResponse:
    def __init__(self, payload, text=None):
        self._payload = payload
        self._text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _patch_fetch(monkeypatch, payload, text=None):
    monkeypatch.setattr(fg, "fetch", lambda source, url, headers=None, params=None:
                        FakeResponse(payload, text))


# ---- happy path -----------------------------------------------------------

def test_spec_example_parses(monkeypatch):
    _patch_fetch(monkeypatch, json.loads(json.dumps(SPEC_EXAMPLE)))
    r = fg.fetch_fear_greed()
    assert r.score == 46.0
    assert r.rating == "neutral"
    assert r.as_of == "2026-01-08"
    assert r.previous_close == 46.0
    assert r.previous_1_year == 31.0
    # 1767914361000 ms = 2026-01-08T23:19:21Z
    assert r.history == [("2026-01-08", 46.0)]


def test_rating_case_insensitive_and_extremes(monkeypatch):
    p = json.loads(json.dumps(SPEC_EXAMPLE))
    p["fear_and_greed"]["rating"] = "Extreme Greed"
    p["fear_and_greed"]["score"] = 93.4
    _patch_fetch(monkeypatch, p)
    r = fg.fetch_fear_greed()
    assert r.rating == "extreme greed" and r.score == 93.4


def test_history_sorted_and_malformed_points_skipped(monkeypatch):
    p = json.loads(json.dumps(SPEC_EXAMPLE))
    p["fear_and_greed_historical"]["data"] = [
        {"x": 1767914361000, "y": 46},          # 2026-01-08
        {"x": 1765200000000, "y": 31},          # 2025-12-08 (earlier -> sorts first)
        {"x": "not-a-number", "y": 50},         # skipped
        {"y": 50},                              # skipped (no x)
        {"x": 1765300000000, "y": 200},         # skipped (out of range)
    ]
    _patch_fetch(monkeypatch, p)
    r = fg.fetch_fear_greed()
    assert r.history == [("2025-12-08", 31.0), ("2026-01-08", 46.0)]


def test_missing_historical_block_is_tolerated(monkeypatch):
    p = {"fear_and_greed": dict(SPEC_EXAMPLE["fear_and_greed"])}
    _patch_fetch(monkeypatch, p)
    assert fg.fetch_fear_greed().history == []


def test_invalid_comparison_values_become_none_not_errors(monkeypatch):
    p = json.loads(json.dumps(SPEC_EXAMPLE))
    p["fear_and_greed"]["previous_close"] = "n/a"
    p["fear_and_greed"]["previous_1_month"] = 400  # out of range
    _patch_fetch(monkeypatch, p)
    r = fg.fetch_fear_greed()
    assert r.previous_close is None and r.previous_1_month is None
    assert r.previous_1_week == 44.0  # valid neighbours survive


# ---- strict validation (spec: "clients must validate") ---------------------

def test_html_block_page_rejected(monkeypatch):
    _patch_fetch(monkeypatch, None, text="<html>Access Denied</html>")
    with pytest.raises(SourceError, match="non-JSON"):
        fg.fetch_fear_greed()


def test_missing_root_block_rejected(monkeypatch):
    _patch_fetch(monkeypatch, {"something_else": 1})
    with pytest.raises(SourceError, match="fear_and_greed"):
        fg.fetch_fear_greed()


def test_score_out_of_range_rejected(monkeypatch):
    p = json.loads(json.dumps(SPEC_EXAMPLE))
    p["fear_and_greed"]["score"] = 146
    _patch_fetch(monkeypatch, p)
    with pytest.raises(SourceError, match="outside"):
        fg.fetch_fear_greed()


def test_score_missing_rejected(monkeypatch):
    p = json.loads(json.dumps(SPEC_EXAMPLE))
    del p["fear_and_greed"]["score"]
    _patch_fetch(monkeypatch, p)
    with pytest.raises(SourceError, match="score"):
        fg.fetch_fear_greed()


def test_unknown_rating_rejected(monkeypatch):
    p = json.loads(json.dumps(SPEC_EXAMPLE))
    p["fear_and_greed"]["rating"] = "panic"
    _patch_fetch(monkeypatch, p)
    with pytest.raises(SourceError, match="rating"):
        fg.fetch_fear_greed()


def test_bad_timestamp_rejected(monkeypatch):
    p = json.loads(json.dumps(SPEC_EXAMPLE))
    p["fear_and_greed"]["timestamp"] = "yesterday"
    _patch_fetch(monkeypatch, p)
    with pytest.raises(SourceError, match="timestamp"):
        fg.fetch_fear_greed()


# ---- feed integration ------------------------------------------------------

def _reading_now(months):
    """A FearGreedReading whose history spans the last ~13 grid months."""
    hist = [(f"{m}-15", 30.0 + i) for i, m in enumerate(months[-13:])]
    return fg.FearGreedReading(
        score=46.0, rating="neutral",
        as_of=datetime.now(UTC).date().isoformat(),
        timestamp=datetime.now(UTC).isoformat(),
        previous_close=46.0, previous_1_week=44.0,
        previous_1_month=31.0, previous_1_year=31.0,
        history=hist)


def test_feed_metric_and_series(monkeypatch, isolated_db):
    from app.services import dashboard_feed as df
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs
    from tests.test_dashboard_feed import fake_fred, fake_td, fake_tiingo

    now = datetime.now(UTC)
    months = df.month_grid(f"{now.year:04d}-{now.month:02d}")
    monkeypatch.setattr(df, "_tiingo_monthly", fake_tiingo)
    monkeypatch.setattr(df, "_fred_series", fake_fred)
    monkeypatch.setattr(df, "_td_series", fake_td)
    monkeypatch.setattr(df, "_fear_greed", lambda: _reading_now(months))

    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
    feed = df.build_feed(raw, data)

    metric = feed["metrics"]["fear_greed"]
    assert metric["available"] is True and metric["value"] == 46.0
    assert metric["unit"] == "index_0_100" and metric["source"] == "cnn:fear_greed"
    assert metric["detail"]["rating"] == "neutral"
    assert metric["detail"]["previous_1_year"] == 31.0

    s = feed["series"]["fear_greed"]
    assert len(s["points"]) == 61
    non_null = [p for p in s["points"] if p["value"] is not None]
    assert len(non_null) == 13          # only ~13 months of CNN history
    assert s["points"][0]["value"] is None   # left-padded nulls
    assert s["points"][-1]["value"] == 42.0  # 30 + 12
    assert s["kind"] == "sentiment_index" and s["available"] is True


def test_feed_degrades_when_cnn_fails(monkeypatch, isolated_db):
    from app.services import dashboard_feed as df
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs
    from tests.test_dashboard_feed import fake_fred, fake_td, fake_tiingo

    monkeypatch.setattr(df, "_tiingo_monthly", fake_tiingo)
    monkeypatch.setattr(df, "_fred_series", fake_fred)
    monkeypatch.setattr(df, "_td_series", fake_td)

    def _boom():
        raise SourceError("CNN F&G: non-JSON response (HTML block page or changed endpoint)")

    monkeypatch.setattr(df, "_fear_greed", _boom)
    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
    feed = df.build_feed(raw, data)

    metric = feed["metrics"]["fear_greed"]
    assert metric["available"] is False and metric["value"] is None
    assert "source failed" in metric["note"]
    s = feed["series"]["fear_greed"]
    assert s["available"] is False
    assert all(p["value"] is None for p in s["points"])
    # the failure degraded ONLY this pair - a neighbouring metric still works
    assert feed["metrics"]["cape"]["available"] is True
