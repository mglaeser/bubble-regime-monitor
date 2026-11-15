"""D3's gate is named and described by what it reads: the group's total revenue.

The gate releases D3's full sub-score when an issuer's revenue grew less than
15 % between its last two fiscal years while its TTM free cash flow is below 0
(app/references.py, REGISTRY["d3"].how). The revenue the adapter reads is the
group's total. EDGAR companyfacts carries the facts of the whole filer and no
segment figure, and app/sources/edgar.py never read one.

What was published said otherwise: "cloud-segment revenue", "best-effort from
XBRL segment data", total revenue as a "deliberately conservative" proxy that
is "harder to trip", and a flag for "a proxy was used" that nothing read. A
group usually grows more slowly than its cloud segment, so total revenue is
the easier bar, not the harder one.

Production, 2026-07-11 to 2026-11-10: 615 of 615 stored D3 notes ended
"total-revenue growth used as conservative gate proxy", and the "harder to
trip" sentence was D3's caveat line in every daily digest prompt. The gate
fired in 0 of the 615 and the sub-score was 0.30 in all of them, so no number
moves with these words. At the last stored ratio (0.8049) the gate decides
between a sub-score of 0.30 and one of 0.61.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

import app.content_registry as content_registry
from app.indicators.d3_hyperscaler_fcf import HyperscalerReading, gate_fired, sub_score
from app.message_engine import context
from app.references import REGISTRY, SOURCE_REGISTRY
from app.sources import SourceError, edgar

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


def test_without_two_fiscal_years_of_revenue_the_gate_cannot_fire_and_no_growth_is_noted(monkeypatch):
    result = _readings(monkeypatch, {"X": _issuer(None)})
    (reading,) = result.value
    assert reading.revenue_yoy_pct is None and reading.ttm_fcf < 0
    assert not gate_fired(result.value)
    assert result.provenance.note is None


def test_a_group_growing_more_slowly_than_its_segment_trips_the_gate_sooner():
    """Capex above OCF in both cases. At 22 % growth (a cloud segment's, say)
    the gate stays off; at 11 % (its group's) it fires. Reading the group is
    the easier bar, and the caveat says which way that leans."""
    def burning(growth: float) -> list[HyperscalerReading]:
        return [HyperscalerReading("X", 1.2, growth, -30e9)]

    assert not gate_fired(burning(22.0)) and gate_fired(burning(11.0))
    # What the gate decides at the last production ratio.
    assert sub_score(0.8049, False) == 0.30
    assert sub_score(0.8049, True) == pytest.approx(0.6098)
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


def test_the_digest_prompt_carries_d3s_caveat_whole():
    """The daily digest's model reads D3's first caveat (context.py). Both of
    its sentences fit the material's limit, so it reads what the gate reads
    and which way that leans."""
    references = context.references_for("daily_digest")
    (ref,) = [r for r in references if r.indicator == "d3"]
    assert ref.caveat == " ".join(REGISTRY["d3"].caveats[0].split())
    assert f"  Caveat: {ref.caveat}" in context.render(references).splitlines()
    assert "total revenue" in ref.caveat and "over-alarming" in ref.caveat


def test_the_display_deck_names_total_revenue_as_what_fires_the_gate():
    """gauge.reg.d3 is served beside the score (data.display)."""
    content_registry._clear_artifact_cache()
    fire = content_registry.dashboard_payload()["blocks"]["gauge.reg.d3"]["entries"]["fire"]
    content_registry._clear_artifact_cache()
    assert "total revenue" in fire and "cloud revenue" not in fire.lower(), fire


def test_d3_has_no_fallback_and_the_readme_names_none(monkeypatch):
    """An issuer without a reading is left out; with none left the source
    fails and the pipeline drops D3. Nothing stands in for a reading."""
    result = _readings(monkeypatch, {"X": _issuer(None), "Y": {"facts": {"us-gaap": {}}}})
    assert [r.ticker for r in result.value] == ["X"]
    with pytest.raises(SourceError):
        _readings(monkeypatch, {"Y": {"facts": {"us-gaap": {}}}})
    (row,) = [line for line in README.read_text(encoding="utf-8").splitlines()
              if line.startswith("| Hyperscaler FCF (D3) |")]
    fallback = row.split("|")[3].strip()
    assert fallback.startswith("none"), fallback
    assert not SEGMENT_READING.search(row), row
