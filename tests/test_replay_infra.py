"""Historical Replay Infrastructure (RM-1..RM-5, v3.8.0).

Covers: the per-snapshot methodology stamp; DB-level append-only enforcement
on falsification outcomes; the S5 sufficiency tracker; the B0-B5 policy
replay; the S5 dual-report backfill incl. the hypothetical headline delta;
the RM-5 assembler; and the read-only API surfaces. Everything is read-only
toward scoring: the golden fixture tests elsewhere prove 52.43 is untouched.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import methodology as M


def _dated_monthly(values, end_year=2026, end_month=6):
    """Month-end-dated copies of a monthly float series (newest last)."""
    import calendar as _cal

    out = []
    y, m = end_year, end_month
    for v in reversed(values):
        out.append((f"{y:04d}-{m:02d}-{_cal.monthrange(y, m)[1]:02d}", v))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _persist_golden(monkeypatch=None):
    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    # the golden fixture builds RawInputs directly (no gather step), so attach
    # the DATED series the PIN-H shadow consumes, mirroring live gathering.
    # The golden S5 rides the daily HY-OAS tier -> daily dates, newest last.
    if raw.hy_oas_history_bps:
        from datetime import date, timedelta

        end = date(2026, 6, 30)
        n = len(raw.hy_oas_history_bps)
        raw.hy_oas_history_dated = [
            ((end - timedelta(days=n - 1 - i)).isoformat(), v)
            for i, v in enumerate(raw.hy_oas_history_bps)]
    data = compute_snapshot(raw, mc_samples=5_000, mc_seed=20260711)
    persist_snapshot(data, raw)
    return data


class TestRM1Evidence:
    def test_snapshot_carries_methodology_stamp(self, isolated_db):
        from app.db import session_scope
        from app.models import Snapshot

        _persist_golden()
        with session_scope() as session:
            snap = session.execute(select(Snapshot)).scalars().first()
            assert snap.methodology_sha256 == M.frozen_sha256()
            assert snap.methodology_version == M.get_path("_meta", "methodology_version")

    def test_falsification_outcomes_append_only(self, isolated_db):
        import sqlalchemy.exc

        from app.db import session_scope
        from app.models import FalsificationOutcome
        from app.services.replay import record_outcome

        oid = record_outcome("test criterion", "detail text")
        assert oid >= 1
        with session_scope() as session:
            row = session.execute(select(FalsificationOutcome)).scalars().first()
            row.detail = "rewritten"
            with pytest.raises(sqlalchemy.exc.DatabaseError, match="append-only"):
                session.flush()
            session.rollback()
        with session_scope() as session:
            row = session.execute(select(FalsificationOutcome)).scalars().first()
            session.delete(row)
            with pytest.raises(sqlalchemy.exc.DatabaseError, match="append-only"):
                session.flush()
            session.rollback()

    def test_record_outcome_rejects_empty_criterion(self, isolated_db):
        from app.services.replay import record_outcome

        with pytest.raises(ValueError):
            record_outcome("   ")

    def test_evidence_summary(self, isolated_db):
        from app.services.replay import evidence_summary, record_outcome

        _persist_golden()
        record_outcome("criterion x")
        ev = evidence_summary()
        assert ev["snapshots_stamped"] == 1
        assert ev["falsification_outcomes"] == 1
        assert ev["latest_snapshot"]["methodology_sha256"] == M.frozen_sha256()
        assert ev["current_artifact_sha256"] == M.frozen_sha256()
        assert ev["outcomes_append_only"] is True


class TestRM2Sufficiency:
    def test_tracker_counts_trading_days_and_tiers(self, isolated_db):
        from app.services.replay import S5_GATE_TRADING_DAYS, s5_tier_sufficiency

        _persist_golden()
        out = s5_tier_sufficiency()
        assert out["gate_trading_days"] == S5_GATE_TRADING_DAYS == 60
        # the golden snapshot computes on a weekday EBP tier
        assert out["days_by_tier"]["fed_ebp"] in (0, 1)
        assert out["trading_days_observed"] in (0, 1)
        assert out["days_remaining_to_gate"] >= 59
        assert out["all_tiers_observed"] is False   # only EBP present -> honest False


class TestRM4Policies:
    def test_b_policy_report_full_coverage_snapshot(self, isolated_db):
        from app.services.replay import b_policy_report

        _persist_golden()
        rep = b_policy_report()
        assert rep["snapshots"] == 1
        # the golden snapshot is fully covered: every candidate policy keeps
        # the headline available and no masking fires
        for policy, s in rep["policies"].items():
            assert s["available"] == 1, policy
            assert s["longest_gap"] == 0
        assert rep["both_blocks_degraded"] == 0
        assert rep["b4_masking_events"] == {"B4@0.50": 0, "B4@2/3": 0}
        assert "no policy or constant is recommended" in rep["note"]

    def test_b_policy_report_degraded_snapshot(self, isolated_db):
        from app.services.compute import compute_snapshot, persist_snapshot
        from app.services.replay import b_policy_report
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        raw.breadth_pct = None              # d1 (0.35) drops -> block D degraded
        data = compute_snapshot(raw, mc_samples=5_000, mc_seed=20260711)
        persist_snapshot(data, raw)
        rep = b_policy_report()
        assert rep["one_block_degraded"] == 1
        assert rep["policies"]["B1"]["available"] == 0       # withheld under B1
        assert rep["policies"]["B0"]["available"] == 1       # current behavior keeps it
        assert "d1" in rep["worst_suppression_drivers"]

    def test_s5_dual_report_headline_delta(self, isolated_db):
        from app.services.replay import s5_dual_report

        _persist_golden()
        rep = s5_dual_report()
        assert rep["snapshots_with_dual_report"] == 1
        row = rep["series"][0]
        assert row["tier"] == "fred_BAMLH0A0HYM2"
        if row["sub_delta"] is not None:        # candidate resolved on this data
            assert rep["sub_score_delta"]["n"] == 1
            assert row["headline_delta"] is not None
        assert "included_in_score=false" in rep["note"]


class TestRM5Assembler:
    def test_packages_mark_host_studies_pending(self, isolated_db):
        from app.services.replay import assemble_decision_packages

        _persist_golden()
        pkg = assemble_decision_packages()
        for pin in ("DE_d3_ocf_quorum", "F_ath_basis", "G_lppls_execution", "C_ndx_identity"):
            assert pkg[pin]["status"] == "PENDING_HOST"
        assert pkg["H_s5_calendar"]["status"] == "EVIDENCE_ACCUMULATING"
        assert pkg["B_coverage_floor"]["report"]["snapshots"] == 1
        assert any("six S5 constants" in r for r in pkg["H_s5_calendar"]["activation_requires"])


class TestApiSurfaces:
    def test_replay_endpoints_and_admin_recording(self, isolated_db):
        from fastapi.testclient import TestClient

        from app.main import create_app
        from tests.conftest import TEST_ADMIN_KEY

        _persist_golden()
        with TestClient(create_app()) as client:
            ev = client.get("/api/v1/replay/evidence")
            assert ev.status_code == 200
            assert ev.json()["data"]["snapshots_stamped"] == 1
            suf = client.get("/api/v1/replay/sufficiency")
            assert suf.status_code == 200
            assert suf.json()["data"]["gate_trading_days"] == 60
            # unauthenticated recording is rejected; keyed recording appends
            assert client.post("/api/v1/admin/falsification",
                               json={"criterion": "c"}).status_code in (401, 403)
            ok = client.post("/api/v1/admin/falsification",
                             json={"criterion": "score<30 through >30% drawdown",
                                   "detail": "manual test"},
                             headers={"X-API-Key": TEST_ADMIN_KEY})
            assert ok.status_code == 201
            assert ok.json()["data"]["recorded"] is True
            ev2 = client.get("/api/v1/replay/evidence")
            assert ev2.json()["data"]["falsification_outcomes"] == 1


class TestCliAndHarness:
    def test_cli_studies_run(self, isolated_db, capsys, monkeypatch):
        import importlib.util
        import json as _json
        import sys as _sys
        from pathlib import Path

        _persist_golden()
        spec = importlib.util.spec_from_file_location(
            "replay_report", Path(__file__).resolve().parents[1] / "scripts" / "replay_report.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        capsys.readouterr()          # flush persist-time log lines from the buffer
        for study in ("sufficiency", "b", "s5", "evidence", "assemble"):
            monkeypatch.setattr(_sys, "argv", ["replay_report.py", study])
            assert mod.main() == 0
            _json.loads(capsys.readouterr().out)      # valid JSON every time
        monkeypatch.setattr(_sys, "argv", ["replay_report.py", "nope"])
        assert mod.main() == 2

    def test_alfred_harness_imports_and_gates_on_key(self):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "alfred_h", Path(__file__).resolve().parents[1] / "docs" / "harnesses"
            / "alfred_vintage_harness.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)               # import-safe without a key
        assert mod.BASE.startswith("https://api.stlouisfed.org")
