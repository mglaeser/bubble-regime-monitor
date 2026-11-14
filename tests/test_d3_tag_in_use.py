"""D3 reads each issuer under the tag it files today, and capex and OCF for one period.

The spec (app/references.py REGISTRY["d3"].how): per CIK, OCF and capex "from
EDGAR companyfacts (handle filer tag variants), compute TTM capex/OCF per firm".
An issuer that changes its tag keeps the old one in companyfacts for ever. The
adapter took the first tag present, so such an issuer was read at the capex of
the year it left the tag, over this year's OCF, under this year's date.

The production evidence for one such issuer is at the tag lists in
app/sources/edgar.py.

What the rule moves in the published score, on production's 615 snapshots up
to 2026-11-10. The five payloads were not read for this change (no network
where it was written), so which of the two cases applies is the owner's check
before the merge, and the merge is the owner's decision on the move. Read at
its filed ratio r, the issuer that was read near 0.05 takes D3's value, the
cohort mean, from 0.80 to 0.80 + (r - 0.05) / 5: 0.98 at r = 0.9, 1.06 at
r = 1.3.

- The gate stays off: the sub-score is min(base, 0.30) and the cap binds from a
  mean of 0.65, so it stays 0.30 and the headline does not move.
- The gate fires (any issuer, read under today's tags, with total revenue
  growth below 15 % and TTM OCF - capex below 0): the sub-score goes from 0.30
  to 1.00 at a mean of 1.0 or more and the headline rises by 8.2 to 10.6
  points, 9.6 on average (the 471 snapshots since 2026-08-23 replayed through
  monte_carlo; 322 of the 437 medians in trim then lie in de-risk). The last
  one, 2026-11-10 22:00 UTC: 48.8 -> 57.3, still trim (de-risk starts at 60).

An issuer whose capex and OCF end on different dates leaves the cohort (D3's
quality n/5). With one issuer out, none of the 563 snapshots whose Block D is
not degraded falls under the coverage line: the lowest, 0.8658, becomes 0.8018
against 0.6667.

The golden fixtures do not move: they hand HyperscalerReading objects to the
pipeline (tests/conftest.py) and never call the adapter.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

from app.indicators import d3_hyperscaler_fcf
from app.sources import SourceError, edgar


def _usd(rows: list[tuple[str, str, float]]) -> dict:
    return {"units": {"USD": [{"start": s, "end": e, "val": v} for s, e, v in rows]}}


def _filed_through_2026q3(fy2024: float, fy2025: float, ytd2025: float, ytd2026: float) -> dict:
    """The FY2024 and FY2025 annuals and the nine-month figures of 2025 and 2026:
    TTM to 2026-09-30 = fy2025 + ytd2026 - ytd2025."""
    return _usd([("2024-01-01", "2024-12-31", fy2024), ("2025-01-01", "2025-12-31", fy2025),
                 ("2025-01-01", "2025-09-30", ytd2025), ("2026-01-01", "2026-09-30", ytd2026)])


OCF = _filed_through_2026q3(120e9, 140e9, 105e9, 115e9)        # TTM 150e9
CAPEX_TODAY = _filed_through_2026q3(83e9, 130e9, 90e9, 140e9)  # TTM 180e9
CAPEX_LEFT_IN_2016 = _usd([("2015-01-01", "2015-12-31", 4.6e9), ("2016-01-01", "2016-12-31", 6.7e9)])
REVENUE_TODAY = _usd([("2024-01-01", "2024-12-31", 600e9), ("2025-01-01", "2025-12-31", 660e9)])        # +10 %
REVENUE_LEFT_IN_2019 = _usd([("2018-01-01", "2018-12-31", 200e9), ("2019-01-01", "2019-12-31", 250e9)])  # +25 %

# The issuer left the FIRST capex tag after FY2016 and the FIRST revenue tag after FY2019.
SWITCHED = {"facts": {"us-gaap": {
    edgar.OCF_TAGS[0]: OCF,
    edgar.CAPEX_TAGS[0]: CAPEX_LEFT_IN_2016, edgar.CAPEX_TAGS[1]: CAPEX_TODAY,
    edgar.REVENUE_TAGS[0]: REVENUE_LEFT_IN_2019, edgar.REVENUE_TAGS[1]: REVENUE_TODAY,
}}}
# The issuer files capex under a tag the adapter does not know: its newest known capex is FY2016.
CAPEX_UNKNOWN_TODAY = {"facts": {"us-gaap": {
    edgar.OCF_TAGS[0]: OCF, edgar.CAPEX_TAGS[0]: CAPEX_LEFT_IN_2016, edgar.REVENUE_TAGS[1]: REVENUE_TODAY,
}}}
# The capex series has no nine-month figure of 2025, so its TTM cannot be
# differenced and ttm() answers with the FY2025 annual; the OCF's TTM ends 2026-09-30.
CAPEX_ANNUAL_ONLY = {"facts": {"us-gaap": {
    edgar.OCF_TAGS[0]: OCF,
    edgar.CAPEX_TAGS[0]: _usd([("2024-01-01", "2024-12-31", 83e9), ("2025-01-01", "2025-12-31", 130e9),
                               ("2026-01-01", "2026-09-30", 140e9)]),
    edgar.REVENUE_TAGS[0]: REVENUE_TODAY,
}}}
STEADY = {"facts": {"us-gaap": {
    edgar.OCF_TAGS[0]: OCF, edgar.CAPEX_TAGS[0]: _filed_through_2026q3(60e9, 70e9, 50e9, 55e9),  # TTM 75e9
    edgar.REVENUE_TAGS[0]: REVENUE_TODAY,
}}}


def _readings(monkeypatch: pytest.MonkeyPatch, by_ticker: dict[str, dict]):
    """hyperscaler_readings() over these issuers' companyfacts; no request is made."""
    monkeypatch.setattr(edgar, "CIKS", {t: t for t in by_ticker})
    monkeypatch.setattr(edgar, "_companyfacts", lambda cik: by_ticker[cik])
    return edgar.hyperscaler_readings()


def test_the_tag_the_issuer_files_today_is_read_not_the_first_one_present():
    value, as_of = edgar.ttm(edgar.duration_facts(SWITCHED, edgar.CAPEX_TAGS))
    assert (value, as_of.isoformat()) == (pytest.approx(180e9), "2026-09-30")  # was (6.7e9, 2016-12-31)
    assert edgar.annual_yoy(edgar.duration_facts(SWITCHED, edgar.REVENUE_TAGS)) == pytest.approx(10.0)  # was 25.0


def test_an_issuer_that_changed_its_capex_tag_is_read_at_the_ratio_it_filed(monkeypatch):
    result = _readings(monkeypatch, {"SWITCHED": SWITCHED})
    (reading,) = result.value
    assert reading.capex_ocf_ttm == pytest.approx(180 / 150)   # was 6.7/150 = 0.045
    assert reading.ttm_fcf == pytest.approx(-30e9)             # was +143.3e9
    assert result.provenance.as_of == "2026-09-30"
    # Growth 10 % (was 25 %, FY2019 over FY2018) and FCF below 0 (was above): the gate reads both.
    assert d3_hyperscaler_fcf.gate_fired(result.value)


def test_two_tags_that_end_on_the_same_date_keep_the_list_order():
    """An issuer that reports both tags for the same periods is read as before."""
    both = {"facts": {"us-gaap": {
        edgar.OCF_TAGS[0]: OCF, edgar.OCF_TAGS[1]: _filed_through_2026q3(1.0, 2.0, 3.0, 4.0)}}}
    assert edgar.ttm(edgar.duration_facts(both, edgar.OCF_TAGS))[0] == pytest.approx(150e9)


@pytest.mark.parametrize(("other", "capex_was_read_as"), [
    (CAPEX_UNKNOWN_TODAY, "FY2016's 6.7e9 over the OCF to 2026-09-30: 0.045"),
    (CAPEX_ANNUAL_ONLY, "FY2025's 130e9 over the OCF to 2026-09-30: 0.867"),
])
def test_capex_that_ends_on_another_date_than_the_ocf_gives_the_issuer_no_reading(
        monkeypatch, other, capex_was_read_as):
    result = _readings(monkeypatch, {"STEADY": STEADY, "OTHER": other})
    assert [r.ticker for r in result.value] == ["STEADY"], capex_was_read_as
    assert result.value[0].capex_ocf_ttm == pytest.approx(75 / 150)


def test_with_no_issuer_left_the_source_fails_and_d3_is_dropped(monkeypatch):
    """No placeholder (AGENTS.md ground rule 4): the cohort's last issuer without
    a reading is the adapter's SourceError, which the pipeline turns into a
    dropped D3 and a renormalized Block D."""
    with pytest.raises(SourceError):
        _readings(monkeypatch, {"OTHER": CAPEX_UNKNOWN_TODAY})     # was one reading at 0.045


def test_the_point_in_time_harness_reads_the_tag_filed_as_of_its_date():
    """The same rule in the D/E replay harness (docs/harnesses): before the
    switch the old tag is the issuer's capex, after it the new one."""
    path = Path(__file__).resolve().parents[1] / "docs" / "harnesses" / "de_d3_pit_harness.py"
    spec = importlib.util.spec_from_file_location("de_d3_pit_harness", path)
    assert spec is not None and spec.loader is not None
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)

    def filed(rows: list[tuple[str, str, float, str]]) -> dict:
        return {"units": {"USD": [{"start": s, "end": e, "val": v, "filed": f} for s, e, v, f in rows]}}

    facts = {"facts": {"us-gaap": {
        edgar.CAPEX_TAGS[0]: filed([("2016-01-01", "2016-12-31", 6.7e9, "2017-02-10")]),
        edgar.CAPEX_TAGS[1]: filed([("2025-01-01", "2025-12-31", 130e9, "2026-02-06")]),
    }}}
    assert harness._pit_ttm(facts, edgar.CAPEX_TAGS, date(2017, 6, 30)) == pytest.approx(6.7e9)
    assert harness._pit_ttm(facts, edgar.CAPEX_TAGS, date(2026, 6, 30)) == pytest.approx(130e9)  # was 6.7e9
    # Both tags filed for the same period: the list order decides, as before.
    both = {"facts": {"us-gaap": {
        edgar.CAPEX_TAGS[0]: filed([("2025-01-01", "2025-12-31", 130e9, "2026-02-06")]),
        edgar.CAPEX_TAGS[1]: filed([("2025-01-01", "2025-12-31", 1.0, "2026-02-06")]),
    }}}
    assert harness._pit_ttm(both, edgar.CAPEX_TAGS, date(2026, 6, 30)) == pytest.approx(130e9)
