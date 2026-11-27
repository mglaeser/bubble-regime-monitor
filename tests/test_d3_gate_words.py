"""D3's gate is described by what it reads: the group's total revenue growth
over its last two fiscal years (app/sources/edgar.py), not a cloud segment's,
which EDGAR companyfacts does not carry.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

import app.content_registry as content_registry
from app.indicators.d3_hyperscaler_fcf import HyperscalerReading, gate_fired
from app.references import REGISTRY, SOURCE_REGISTRY
from app.sources import edgar

README = Path(__file__).resolve().parents[1] / "README.md"

#: The claims that go: a segment figure that is read, and a proxy for it.
SEGMENT_READING = re.compile(r"proxy|best-effort|segment tags|segment data|press figures", re.I)


def _usd(rows):
    return {"units": {"USD": [{"start": s, "end": e, "val": v} for s, e, v in rows]}}


def _two_years(fy2024: float, fy2025: float) -> dict:
    return _usd([("2024-01-01", "2024-12-31", fy2024), ("2025-01-01", "2025-12-31", fy2025)])


def _issuer(revenue: dict | None) -> dict:
    """Capex above operating cash flow (120bn over 100bn), so TTM FCF < 0."""
    facts = {edgar.OCF_TAGS[0]: _two_years(100e9, 100e9), edgar.CAPEX_TAGS[0]: _two_years(90e9, 120e9)}
    if revenue is not None:
        facts[edgar.REVENUE_TAGS[0]] = revenue
    return {"facts": {"us-gaap": facts}}


def _readings(monkeypatch, by_issuer: dict[str, dict]):
    monkeypatch.setattr(edgar, "CIKS", {ticker: ticker for ticker in by_issuer})
    monkeypatch.setattr(edgar, "_companyfacts", lambda cik: by_issuer[cik])
    return edgar.hyperscaler_readings()


def test_a_reading_holds_the_revenue_growth_and_no_flag_for_a_proxy():
    assert [f.name for f in dataclasses.fields(HyperscalerReading)] == [
        "ticker", "capex_ocf_ttm", "revenue_yoy_pct", "ttm_fcf"]


def test_the_gate_reads_the_groups_total_revenue_growth(monkeypatch):
    """Total revenue up 11 %, capex above OCF: the gate fires, whatever a
    cloud segment inside the group grew by. The payload has no place for it."""
    result = _readings(monkeypatch, {"X": _issuer(_two_years(600e9, 666e9))})
    (reading,) = result.value
    assert reading.revenue_yoy_pct == pytest.approx(11.0)
    assert reading.ttm_fcf < 0 and gate_fired(result.value)
    assert result.provenance.note == "gate growth: total revenue, last two fiscal years"


def test_a_group_growing_more_slowly_than_its_segment_trips_the_gate_sooner():
    """Capex above OCF in both cases. At 22 % growth (a cloud segment's, say)
    the gate stays off; at 11 % (its group's) it fires. Reading the group is
    the easier bar, and the caveat says which way that leans."""
    def burning(growth: float) -> list[HyperscalerReading]:
        return [HyperscalerReading("X", 1.2, growth, -30e9)]

    assert not gate_fired(burning(22.0)) and gate_fired(burning(11.0))
    caveat = REGISTRY["d3"].caveats[0]
    assert "trips more easily" in caveat and "over-alarming" in caveat
    assert "harder to trip" not in caveat and "under-alarming" not in caveat


def test_the_registry_says_total_revenue_is_read_and_no_segment_figure():
    """The methodology record and the source record, both served by the API."""
    (source,) = [spec for spec in SOURCE_REGISTRY if spec.key == "edgar"]
    record = REGISTRY["d3"]
    for text in (record.how, record.caveats[0], source.caveats):
        assert "total revenue" in text.lower(), text
        assert not SEGMENT_READING.search(text), text
    assert "total revenue grew less than 15%" in record.how


def test_the_display_deck_names_total_revenue_as_what_fires_the_gate():
    """gauge.reg.d3 is served beside the score (data.display)."""
    content_registry._clear_artifact_cache()
    fire = content_registry.dashboard_payload()["blocks"]["gauge.reg.d3"]["entries"]["fire"]
    content_registry._clear_artifact_cache()
    assert "total revenue" in fire and "cloud revenue" not in fire.lower(), fire


def test_the_readme_names_no_stand_in_for_a_d3_reading():
    """The sources table's fallback cell for D3 says "none" and names no segment
    reading or proxy."""
    (row,) = [line for line in README.read_text(encoding="utf-8").splitlines()
              if line.startswith("| Hyperscaler FCF (D3) |")]
    fallback = row.split("|")[3].strip()
    assert fallback.startswith("none"), fallback
    assert not SEGMENT_READING.search(row), row
