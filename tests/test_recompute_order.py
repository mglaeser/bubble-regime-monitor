"""What a recompute has on disk at each of its steps, and in which order.

The specification these are written from:
  - app/services/alert_integration.py: "a lost sidecar is a hole in the replay
    record that can never be filled";
  - app/engine/judgment.py: "the recompute never waits for" the note, and on
    an error or a timeout "the snapshot keeps the last successful text with
    stale:true";
  - GET /readyz (app/routers/health.py): "Per-source health matrix from the
    latest gather".

Production, the 222 recomputes from 2026-10-05 to 2026-11-10 (journal and
database): the alert capture was committed 4.5 to 40.2 s after the snapshot
(median 6.8 s, p90 21.3 s), behind the feed's provider fetches. On 2026-11-01
the health check killed the container 3.7 s after the capture
(alert_capture_committed 02:02:48.04, container kill 02:02:51.73). Of 490
snapshots with a typed contract none lacks its sidecar.

In the same 222 runs the snapshot's commit waited for the model's note for
22.1 to 289.2 s (median 97.0 s, p90 155.9 s) after a gather and computation
of 24.6 to 104.6 s (median 48.8 s): two thirds of the time in which a restart
discarded a finished computation (148 s per run at the median, 49 s without
the wait). On 2026-11-01 that kill came 26 s after the commit.

The production database to 2026-11-10 holds 11,048 source_health rows at 615
distinct times, each the computed_at of one of the 615 snapshots: the matrix
was the latest snapshot's, and a gather whose run could not score left no row.
The journal since 2026-10-05 has no such run (0 recompute_impossible beside
222 recompute_done); it is the run after which an operator opens the page.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import func, select

import app.services.dashboard_feed as dashboard_feed
from app.alerts.models import AlertInputSnapshot
from app.config import get_settings
from app.db import session_scope
from app.engine import judgment
from app.models import Snapshot
from app.routers.health import readyz
from app.services import compute
from tests.conftest import make_golden_raw_inputs


class _ProcessDies(BaseException):
    """Stands in for the container's SIGKILL: no `except Exception` catches
    it, so nothing after the step that raises it runs."""


class _Probe:
    """One `run_recompute` after another on the golden inputs. `completion`
    and `feed` are what the two steps that wait on a network answer; a test
    replaces one to fail or to die there."""

    def __init__(self) -> None:
        #: The network steps in the order the run reached them.
        self.order: list[str] = []
        #: What was on disk when each was reached.
        self.seen: dict[str, dict[str, int]] = {}
        self.completion = lambda prompt: "A plain note."
        self.feed = lambda raw, data: 0

    @staticmethod
    def on_disk() -> dict[str, int]:
        with session_scope() as session:
            return {
                "snapshots": session.scalar(select(func.count()).select_from(Snapshot)),
                "sidecars": session.scalar(select(func.count()).select_from(AlertInputSnapshot)),
            }

    @staticmethod
    def note(snapshot_id: int) -> tuple[str | None, bool, str | None]:
        """The judgment note the row holds: its text, stale, the error class."""
        with session_scope() as session:
            row = session.get_one(Snapshot, snapshot_id)
            return row.judgment_call, row.judgment_stale, row.judgment_error

    @staticmethod
    def run() -> int | None:
        return compute.run_recompute()


@pytest.fixture()
def recompute(isolated_db, monkeypatch):
    """`run_recompute` without a network: the gather returns the golden
    inputs, and the judgment's gateway call and the feed's build are probes
    that record what is on disk when the run reaches them."""
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    get_settings.cache_clear()
    probe = _Probe()

    def _completion(prompt: str) -> str:
        probe.order.append("judgment")
        probe.seen["judgment"] = probe.on_disk()
        return probe.completion(prompt)

    def _feed(raw, data) -> int:
        probe.order.append("feed")
        probe.seen["feed"] = probe.on_disk()
        return probe.feed(raw, data)

    monkeypatch.setattr(compute, "gather_inputs", make_golden_raw_inputs)
    monkeypatch.setattr(judgment, "run_completion", _completion)
    monkeypatch.setattr(dashboard_feed, "build_and_persist_feed", _feed)
    yield probe
    get_settings.cache_clear()


def test_the_alert_capture_does_not_wait_for_the_feed(recompute):
    def _dies(raw, data) -> int:
        raise _ProcessDies

    recompute.run()
    assert recompute.seen["feed"] == {"snapshots": 1, "sidecars": 1}
    recompute.feed = _dies
    with pytest.raises(_ProcessDies):
        recompute.run()
    assert recompute.on_disk() == {"snapshots": 2, "sidecars": 2}


def test_the_snapshot_is_committed_before_the_model_is_asked(recompute):
    snapshot_id = recompute.run()
    assert recompute.seen["judgment"] == {"snapshots": 1, "sidecars": 1}
    assert recompute.note(snapshot_id) == ("A plain note.", False, None)


def test_the_model_is_asked_last(recompute):
    recompute.run()
    assert recompute.order == ["feed", "judgment"]


def test_a_restart_during_the_model_call_loses_nothing(recompute):
    def _dies(prompt: str) -> str:
        raise _ProcessDies

    first = recompute.run()
    recompute.completion = _dies
    with pytest.raises(_ProcessDies):
        recompute.run()
    assert recompute.on_disk() == {"snapshots": 2, "sidecars": 2}
    assert recompute.order == ["feed", "judgment", "feed", "judgment"]
    # The row says what it holds: the previous run's note, marked stale.
    assert recompute.note(first + 1) == ("A plain note.", True, None)


def test_a_failed_model_call_keeps_the_previous_note_and_names_the_error(recompute):
    def _fails(prompt: str) -> str:
        raise TimeoutError("the gateway did not answer")

    first = recompute.run()
    recompute.completion = _fails
    second = recompute.run()
    assert second == first + 1
    assert recompute.note(second) == ("A plain note.", True, "TimeoutError")


def test_a_first_run_whose_model_call_fails_has_no_note_and_names_the_error(recompute):
    def _fails(prompt: str) -> str:
        raise TimeoutError("the gateway did not answer")

    recompute.completion = _fails
    assert recompute.note(recompute.run()) == (None, True, "TimeoutError")


def _pull(source: str, *, ok: bool, note: str | None = None) -> dict[str, object]:
    """One source's health as the gather records it (compute._track)."""
    return {"source": source, "ok": ok, "latency_ms": 12.0,
            "http_status": 200 if ok else None, "note": note}


def test_a_run_that_cannot_score_still_records_its_gather(recompute, monkeypatch):
    raw = make_golden_raw_inputs()
    # Block D without a usable source: the run cannot score.
    raw.breadth_pct = raw.margin_balances = raw.hyperscalers = raw.lppls_confidence = None
    raw.source_health = [_pull("cape", ok=True), _pull("finra_xlsx", ok=False, note="upstream 500")]
    monkeypatch.setattr(compute, "gather_inputs", lambda: raw)

    assert recompute.run() is None
    assert recompute.on_disk() == {"snapshots": 0, "sidecars": 0}
    sources = readyz()["sources"]
    assert sources["cape"]["ok"] is True
    assert (sources["finra_xlsx"]["ok"], sources["finra_xlsx"]["note"]) == (False, "upstream 500")


def test_a_failed_health_write_does_not_cost_the_score(recompute, monkeypatch):
    def _locked(**columns):
        raise sqlite3.OperationalError("database is locked")

    raw = make_golden_raw_inputs()
    raw.source_health = [_pull("cape", ok=True)]
    monkeypatch.setattr(compute, "gather_inputs", lambda: raw)
    monkeypatch.setattr(compute, "SourceHealth", _locked)

    assert recompute.run() is not None
    assert recompute.on_disk() == {"snapshots": 1, "sidecars": 1}
