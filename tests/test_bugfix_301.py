"""Regression tests for the v3.0.1 first-live-run bugfixes."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import make_golden_raw_inputs


def _make_finra_xlsx(newest_first: bool = True) -> bytes:
    """Fixture XLSX shaped like FINRA's: title rows, then Month + Debit columns."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["FINRA Margin Statistics"])
    ws.append([])
    ws.append(["Month/Year", "Debit Balances in Customers' Securities Margin Accounts"])
    # 14 months, 2025-04 .. 2026-05; May 2026 = 1,415,557 and May 2025 set
    # twelve rows earlier so the YoY works out to +53.7%.
    rows = []
    for i in range(14):
        month_total = 4 + i  # April 2025 start
        y, m = 2025 + (month_total - 1) // 12, (month_total - 1) % 12 + 1
        rows.append([f"{y}-{m:02d}-01", 900_000.0 + i * 1000])
    rows[-1][1] = 1_415_557.0          # 2026-05
    rows[1][1] = 1_415_557 / 1.537     # 2025-05 (12 months before the latest)
    if newest_first:
        rows = list(reversed(rows))
    for ymd, val in rows:
        ws.append([ymd, val])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestFinraParser:
    def test_latest_row_selected_despite_newest_first_order(self):
        from app.sources.finra import parse_debit_balances

        values, months, as_of = parse_debit_balances(_make_finra_xlsx(newest_first=True))
        assert as_of == "2026-05-31"  # v3.7.4/C-06: reference month-END
        assert months[-1] == "2026-05" and len(months) == len(values)  # v3.7.5/C-07
        assert values[-1] == pytest.approx(1_415_557.0)
        # YoY across the properly sorted window ~ +53.7%
        from app.indicators.d2_margin import sub_score, yoy_pct

        yoy = yoy_pct(values)
        assert yoy == pytest.approx(53.7, abs=0.1)
        assert sub_score(yoy, rollover=False) == pytest.approx(0.49, abs=0.01)

    def test_chronological_input_also_works(self):
        from app.sources.finra import parse_debit_balances

        values, months, as_of = parse_debit_balances(_make_finra_xlsx(newest_first=False))
        assert as_of == "2026-05-31"  # v3.7.4/C-06: reference month-END
        assert values[-1] == pytest.approx(1_415_557.0)


class TestD2Guards:
    def test_stale_over_90d_uses_cached_reading(self, isolated_db):
        from app.services.compute import compute_snapshot

        raw = make_golden_raw_inputs()
        raw.margin_as_of = (datetime.now(UTC) - timedelta(days=200)).date().isoformat()
        raw.margin_cached = {"value": 53.7, "sub_score": 0.49,
                             "timestamp": "2026-06-01T06:00:00+00:00"}
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        d2 = data.indicators["d2"]
        assert not d2.dropped
        assert d2.sub_score == pytest.approx(0.49)
        assert "cached reading" in (d2.note or "")
        assert "rollover: unknown" in (d2.note or "")

    def test_stale_over_90d_without_cache_drops(self, isolated_db):
        from app.services.compute import compute_snapshot

        raw = make_golden_raw_inputs()
        raw.margin_as_of = (datetime.now(UTC) - timedelta(days=200)).date().isoformat()
        raw.margin_cached = None
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        assert data.indicators["d2"].dropped

    def test_rollover_asserted_within_75d_sla(self, isolated_db):
        from app.services.compute import compute_snapshot

        raw = make_golden_raw_inputs()
        # descending recent months => rollover pattern present in the data...
        raw.margin_balances = [900_000 + i * 40_000 for i in range(11)] + [1_400_000, 1_390_000, 1_380_000]
        # ...and a ~71-day age is the FRESHEST possible FINRA reading (monthly,
        # ~3-week lag), so within the 75d SLA it must NOT be flagged stale.
        raw.margin_as_of = (datetime.now(UTC) - timedelta(days=71)).date().isoformat()
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        assert not data.indicators["d2"].dropped
        assert "rollover: unknown" not in (data.indicators["d2"].note or "")

    def test_fresh_data_may_assert_rollover(self, isolated_db):
        from app.services.compute import compute_snapshot

        raw = make_golden_raw_inputs()
        raw.margin_balances = [900_000 + i * 40_000 for i in range(11)] + [1_400_000, 1_390_000, 1_380_000]
        raw.margin_as_of = datetime.now(UTC).date().isoformat()
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        assert "rollover confirmed" in (data.indicators["d2"].note or "")


class TestRenormalizationRegression:
    def test_dropped_indicators_null_and_renormalized(self, isolated_db):
        """Spec check: with D1 and D4 dropped, weights renormalize to
        d2 = 0.13/0.45 = 0.289, d3 = 0.32/0.45 = 0.711."""
        from app.engine.aggregate import deterministic_score
        from app.engine.montecarlo import BASE_WEIGHTS_D, BASE_WEIGHTS_S

        sub_s = {"s1": 0.92, "s2": 0.80, "s3": 0.525, "s4": 0.25, "s5": 0.80}
        sub_d = {"d2": 0.49, "d3": 0.30}  # d1/d4 dropped
        from app.engine.aggregate import RedFlags

        res = deterministic_score(sub_s, sub_d, 1.0, RedFlags(),
                                  BASE_WEIGHTS_S, BASE_WEIGHTS_D)
        assert res.weights_d["d2"] == pytest.approx(0.13 / 0.45)
        assert res.weights_d["d3"] == pytest.approx(0.32 / 0.45)
        # v3.3.0 rescale-then-aggregate: prod(rescale(s)^w), rescale(s)=0.10+0.90*s
        from app.engine.aggregate import rescale
        expected_d = rescale(0.49) ** (0.13 / 0.45) * rescale(0.30) ** (0.32 / 0.45)
        assert res.d_block == pytest.approx(expected_d, abs=1e-9)

    def test_dropped_payload_has_null_subscore(self, isolated_db):
        from app.services.compute import compute_snapshot

        raw = make_golden_raw_inputs()
        raw.breadth_pct = None
        raw.lppls_confidence = None
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        for ind_id in ("d1", "d4"):
            p = data.indicators[ind_id].payload()
            assert p["dropped"] is True
            assert p["sub_score"] is None


class TestJudgmentDegradation:
    def test_no_prior_failure_is_machine_detectable(self, isolated_db):
        """anthropic is not installed in this venv, so generate() exercises
        the real failure path: text must be None (not a placeholder) with an
        error_class set."""
        from app.engine.judgment import generate

        call = generate(40.0, (34.0, 47.0), "hold", {}, {}, 1.0, {}, False,
                        "IN", "IN", {}, last_successful=None)
        assert call.text is None
        assert call.stale is True
        assert call.error_class

    def test_prior_text_served_stale(self, isolated_db):
        from app.engine.judgment import generate

        call = generate(40.0, (34.0, 47.0), "hold", {}, {}, 1.0, {}, False,
                        "IN", "IN", {}, last_successful="previous judgment")
        assert call.text == "previous judgment"
        assert call.stale is True


class TestStooqUnavailableTyped:
    def test_html_captcha_detected(self):
        from app.sources.stooq import SourceUnavailable, _parse_csv

        with pytest.raises(SourceUnavailable):
            _parse_csv("<html><body>captcha</body></html>", "spy.us")
