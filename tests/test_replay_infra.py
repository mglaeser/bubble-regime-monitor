"""Historical Replay Infrastructure (RM-1..RM-5, v3.8.0).

Covers: the per-snapshot methodology stamp; DB-level append-only enforcement
on falsification outcomes; the B0-B5 policy replay; the RM-5 assembler; and
the read-only API surfaces. (The S5 calendar shadow study went with the
shadows, owner decision D4, 2026-09-28.) Everything is read-only
toward scoring: the golden fixture tests elsewhere prove 52.43 is untouched.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import methodology as M


def _persist_golden(monkeypatch=None):
    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
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

    def test_insert_or_replace_cannot_rewrite_outcomes(self, isolated_db):
        # Panel round-8 finding: INSERT OR REPLACE resolves a PK conflict by
        # DELETING the existing row, and with recursive_triggers OFF (SQLite
        # default) that implicit delete fires NO delete trigger — so the
        # append-only pair alone could be bypassed. The BEFORE INSERT guard
        # rejects id-reuse before conflict resolution (and the engine also
        # sets PRAGMA recursive_triggers=ON as depth).
        import sqlalchemy.exc
        from sqlalchemy import text

        from app.db import session_scope
        from app.models import FalsificationOutcome
        from app.services.replay import record_outcome

        oid = record_outcome("original criterion", "original detail")
        with session_scope() as session:
            with pytest.raises(sqlalchemy.exc.DatabaseError, match="append-only"):
                session.execute(text(
                    "INSERT OR REPLACE INTO falsification_outcomes "
                    "(id, criterion, tripped_at, detail) "
                    "VALUES (:i, 'tampered', '2026-07-25T00:00:00+00:00', 'x')"),
                    {"i": oid})
            session.rollback()
        with session_scope() as session:
            row = session.get(FalsificationOutcome, oid)
            assert row.criterion == "original criterion"    # untouched
            assert row.detail == "original detail"
        assert record_outcome("second criterion") == oid + 1  # appends still work

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


class TestNyseCalendar:
    def test_nyse_holiday_calendar(self):
        from datetime import date

        from app.alerts.calendars import is_trading_day, us_market_holidays

        h = us_market_holidays(2026)
        assert len(h) == 10
        assert date(2026, 4, 3) in h                # Good Friday via computus
        assert date(2026, 7, 3) in h                # July 4 2026 is a Saturday -> observed Friday
        assert not is_trading_day(date(2026, 11, 26))   # Thanksgiving
        assert not is_trading_day(date(2026, 7, 4))     # Saturday anyway
        assert is_trading_day(date(2026, 7, 6))         # ordinary Monday
        assert date(2027, 7, 5) in us_market_holidays(2027)   # Sun -> observed Monday
        # NYSE adopted Juneteenth in 2022: not a holiday before, observed after
        assert len(us_market_holidays(2021)) == 9
        assert is_trading_day(date(2021, 6, 18))               # pre-adoption Friday
        assert date(2022, 6, 20) in us_market_holidays(2022)   # 2022: Sun -> Monday




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
            assert s["longest_gap_days"] == 0
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

    def test_longest_gap_counts_distinct_days_not_rows(self, isolated_db):
        # Panel round-6 finding: the 4-hourly recompute persists several rows
        # per day, so counting consecutive ROWS inflated the "longest
        # continuous unavailable period" (a one-day outage read as ~6). Two
        # degraded snapshots on the SAME day are ONE gap day.
        from app.services.compute import compute_snapshot, persist_snapshot
        from app.services.replay import b_policy_report
        from tests.conftest import make_golden_raw_inputs

        for _ in range(2):                    # both persisted "today"
            raw = make_golden_raw_inputs()
            raw.breadth_pct = None            # d1 drops -> B1 unavailable
            data = compute_snapshot(raw, mc_samples=5_000, mc_seed=20260711)
            persist_snapshot(data, raw)
        rep = b_policy_report()
        assert rep["policies"]["B1"]["unavailable"] == 2      # rows counted
        assert rep["policies"]["B1"]["longest_gap_days"] == 1  # days counted

    def test_block_coverage_fraction_is_exact_at_policy_boundaries(self):
        # Panel round-4 finding: fractions were rounded to 4 dp BEFORE the
        # B2/B3/B4 threshold comparisons — round(0.66666, 4) = 0.6667 read a
        # below-2/3 snapshot as available. Coverage must stay exact.
        from app.services.replay import _block_coverage

        payload = {"x": {"dropped": False, "stale": False,
                         "quality": 0.66666, "sub_score": 0.5}}
        cov = _block_coverage(payload, {"x": 1.0})
        assert cov["fraction"] == 0.66666          # not 0.6667
        assert cov["fraction"] < 2.0 / 3.0         # boundary verdict preserved
        assert cov["lost_weight"]["x"] == 1.0 - 0.66666

class TestRM5Assembler:
    def test_packages_mark_host_studies_pending(self, isolated_db):
        from app.services.replay import assemble_decision_packages

        _persist_golden()
        pkg = assemble_decision_packages()
        for pin in ("DE_d3_ocf_quorum", "F_ath_basis", "G_lppls_execution", "C_ndx_identity"):
            assert pkg[pin]["status"] == "PENDING_HOST"
        assert "H_s5_calendar" not in pkg
        assert pkg["B_coverage_floor"]["report"]["snapshots"] == 1


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
            assert client.get("/api/v1/replay/sufficiency").status_code == 404
            # panel finding: empty criterion is a 422 client error, never a 500
            bad = client.post("/api/v1/admin/falsification", json={},
                              headers={"X-API-Key": TEST_ADMIN_KEY})
            assert bad.status_code == 422
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
        for study in ("b", "evidence", "assemble"):
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
