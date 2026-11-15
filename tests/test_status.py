"""Status + spec UI: JSON aggregation, science audit, source-pull reflection, XSS safety."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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


class TestStatusJson:
    def test_shape_on_empty_db(self, client):
        body = client.get("/api/v1/status").json()
        for k in ("service", "snapshot", "recompute", "sources", "feed_sources", "providers",
                  "indicators", "science_audit", "falsification_criteria", "changelog",
                  "epistemic_caveats"):
            assert k in body, f"missing {k}"
        # v3.6.0: no advice tag in machine payloads (status HTML carries it statically)
        assert "disclaimer" not in body
        assert body["snapshot"] is None  # no recompute yet
        assert body["feed_sources"] is None  # no feed payload persisted yet
        assert len(body["indicators"]) == 10
        # v3.7.2: 11 rows — fed_ebp (S5 primary) added; S5 fallbacks carry both keys
        assert len(body["sources"]) == 11
        keys = {s["key"] for s in body["sources"]}
        assert "fed_ebp" in keys
        # every source is "unknown" before the first pull
        assert all(s["status"] == "unknown" for s in body["sources"])
        assert body["service"]["docs"]["swagger"] == "/docs"

    def test_feed_sources_reflect_latest_payload(self, client):
        """v3.7.2: the non-scoring feed pulls (incl. CNN F&G) appear on the
        status page, reflected from the latest persisted feed payload."""
        import json as _json
        from datetime import UTC, datetime

        from app.db import session_scope
        from app.models import DashboardFeed

        payload = {
            "series": {"fear_greed": {"source": "cnn:fear_greed", "available": False,
                                      "stale": None, "as_of": None,
                                      "note": "source failed: CNN F&G: non-JSON response"}},
            "metrics": {"gold_spot": {"source": "twelvedata:XAU/USD", "available": True,
                                      "stale": False, "as_of": "2026-07-16"}},
        }
        with session_scope() as session:
            session.add(DashboardFeed(computed_at=datetime(2026, 7, 16, 22, 0, tzinfo=UTC),
                                      payload=_json.dumps(payload)))
        fs = client.get("/api/v1/status").json()["feed_sources"]
        assert fs is not None and fs["unavailable"] == ["fear_greed"]
        by_key = {i["key"]: i for i in fs["items"]}
        assert by_key["fear_greed"]["source"] == "cnn:fear_greed"
        assert by_key["fear_greed"]["available"] is False
        assert by_key["gold_spot"]["available"] is True

    def test_every_tracked_pull_has_a_registry_row(self):
        """v3.7.2 guard: every _track() label in the gather must be wired to a
        SOURCE_REGISTRY health_keys entry — the gap that hid fed_ebp/BAA for
        four releases can never silently reopen."""
        import re
        from pathlib import Path

        from app.references import SOURCE_REGISTRY

        compute_src = (Path(__file__).resolve().parents[1] / "app/services/compute.py").read_text()
        tracked = set(re.findall(r'_track\(raw, "([^"]+)"', compute_src))
        assert tracked, "no _track labels found — gather refactored?"
        wired = {k for spec in SOURCE_REGISTRY for k in spec.health_keys}
        missing = sorted(tracked - wired)
        assert not missing, f"tracked pulls invisible on the status page: {missing}"

    def test_known_issues_always_flagged(self, client):
        audit = client.get("/api/v1/status").json()["science_audit"]
        cats = {f["category"] for f in audit["flags"]}
        # the scientific-correctness catalogue is always present
        # citations were verified during the 2026-07 audit → now surfaced as
        # "citation-verified" info flags (previously "citation-unverified" warns).
        assert "citation-verified" in cats
        assert "citation-unverified" not in cats
        assert "contested-method" in cats
        assert "documented-deviation" in cats  # the d1 breadth-anchor deviation
        assert "data-substitution" in cats     # ETF index proxies
        # v3.7.1: the stale alpha-range entry (claimed U(0.25,0.75); real range
        # is the spec's U(0.40,0.60) since v3.3.0) must never come back
        ids = {f["id"] for f in audit["flags"]}
        assert "alpha-range-deviation" not in ids
        assert "d1-anchor-deviation" in ids
        # all three framework citations resolve to real sources
        assert sum(1 for f in audit["flags"] if f["category"] == "citation-verified") == 3
        # flags are severity-ordered (error, then warn, then info)
        ranks = {"error": 0, "warn": 1, "info": 2}
        seq = [ranks[f["severity"]] for f in audit["flags"]]
        assert seq == sorted(seq)


class TestSourcePullReflection:
    def _write_health(self, ok: bool, note: str = "", source: str = "cape"):
        from app.db import session_scope
        from app.models import SourceHealth

        with session_scope() as session:
            session.add(SourceHealth(source=source, ok=ok, latency_ms=123.4,
                                     http_status=200 if ok else None,
                                     checked_at=datetime.now(UTC), note=note or None))

    def test_failed_pull_surfaces_status_and_flag(self, client):
        self._write_health(ok=False, note="403 Forbidden", source="cape")
        body = client.get("/api/v1/status").json()
        cape = next(s for s in body["sources"] if s["key"] == "cape")
        assert cape["status"] == "failed"
        assert any(p["note"] == "403 Forbidden" and not p["ok"] for p in cape["pulls"])
        # and it produces a source-pull-failed flag in the audit
        assert any(f["category"] == "source-pull-failed" and "cape" in f["id"]
                   for f in body["science_audit"]["flags"])

    def test_successful_pull_marks_ok(self, client):
        self._write_health(ok=True, source="finra_xlsx")
        body = client.get("/api/v1/status").json()
        finra = next(s for s in body["sources"] if s["key"] == "finra")
        assert finra["status"] == "ok"
        assert not any(f["id"] == "source-finra" for f in body["science_audit"]["flags"])

    def test_partial_group_is_partial(self, client):
        # prices group has 5 pulls; one fails -> partial
        self._write_health(ok=True, source="price_SPY")
        self._write_health(ok=False, note="tiingo cap", source="price_NDX")
        body = client.get("/api/v1/status").json()
        prices = next(s for s in body["sources"] if s["key"] == "prices")
        assert prices["status"] == "partial"


class TestLiveSnapshotFlags:
    def test_snapshot_and_coverage_degraded_flag(self, client, isolated_db):
        from app.services.compute import compute_snapshot, persist_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        raw.hyperscalers = None      # drop D3 (0.32)
        raw.lppls_confidence = None  # drop D4 (0.20) -> >1/3 of Block D
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        persist_snapshot(data, raw)
        body = client.get("/api/v1/status").json()
        assert body["snapshot"] is not None
        cats = [f["category"] for f in body["science_audit"]["flags"]]
        assert "block-degraded" in cats
        assert "indicator-dropped" in cats

    @pytest.mark.parametrize("override, sentence", [
        (False, "the action band is suppressed."),
        (True, "the fired override keeps the action band at de-risk.")])
    def test_the_coverage_flag_says_what_the_band_is(self, client, isolated_db, override, sentence):
        """README, v3.7.3: a fired override wins the action band on degraded
        data. The flag said "the action band is suppressed" then too."""
        from app.services.compute import compute_snapshot, persist_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        raw.hyperscalers = None      # drop D3 (0.32)
        raw.lppls_confidence = None  # drop D4 (0.20) -> >1/3 of Block D
        if override:                 # flags 2, 3 and 4
            raw.smh_2yr_return_pct, raw.spy_2yr_return_pct = 200.0, 30.0
            raw.hy_oas_bps, raw.hy_oas_history_bps = 500.0, [200.0] * 800
            raw.breadth_pct, raw.index_within_2pct_of_ath = 40.0, True
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        assert data.override_fired is override and data.action_band.startswith(
            "de-risk" if override else "suppressed")
        persist_snapshot(data, raw)
        flags = {f["id"]: f for f in client.get("/api/v1/status").json()["science_audit"]["flags"]}
        assert flags["coverage-D"]["detail"].endswith(sentence)


class TestHtmlPage:
    def test_root_and_status_serve_html(self, client):
        for path in ("/", "/status"):
            r = client.get(path)
            assert r.status_code == 200
            assert r.text.lstrip().startswith("<!DOCTYPE html")
            assert "bubblegauge" in r.text

    def test_page_is_self_contained_no_external_assets(self, client):
        html = client.get("/").text
        # no external scripts/styles/images -> CSP-friendly, no data exfil surface
        assert "src=\"http" not in html and "href=\"http" not in html.split("<body")[0]

    def test_page_has_no_unsafe_innerhtml_interpolation(self):
        # XSS guard: external notes/errors must never be injected as markup.
        # The page renders exclusively via textContent/createElement.
        html = (Path(__file__).resolve().parents[1] / "app" / "routers" / "status.html").read_text()
        assert ".innerHTML" not in html
        assert "textContent" in html


class TestOpenApiExample:
    def test_score_has_example_and_status_path(self, client):
        oa = client.get("/openapi.json").json()
        assert "/api/v1/status" in oa["paths"]
        ex = oa["paths"]["/api/v1/score"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
        assert ex["data"]["headline_median"] == 53
        # root HTML page is hidden from the machine schema
        assert "/" not in oa["paths"]


class TestCompletenessFixes:
    def test_leg_science_surfaced(self, client):
        body = client.get("/api/v1/status").json()
        legs = {leg["id"]: leg for leg in body["legs_science"]}
        assert set(legs) == {"trend", "fast_alarm", "action_bands"}
        # Faber (trend), Bollerslev (fast alarm), Alessi-Detken (action bands)
        assert any("Faber" in r for r in legs["trend"]["references"])
        assert any("Bollerslev" in r for r in legs["fast_alarm"]["references"])
        assert any("Alessi" in r for r in legs["action_bands"]["references"])
        assert legs["trend"]["caveat"]

    def test_example_shared_and_consistent(self, client):
        # Single source of truth: score.py and status.py both import the same
        # references.SCORE_EXAMPLE, so page and OpenAPI examples cannot drift.
        import app.routers.score as score_router
        from app.references import SCORE_EXAMPLE

        assert score_router._SCORE_EXAMPLE is SCORE_EXAMPLE
        body = client.get("/api/v1/status").json()
        assert body["example"] == SCORE_EXAMPLE  # status doc serves the canonical example
        oa = client.get("/openapi.json").json()
        oa_ex = oa["paths"]["/api/v1/score"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
        assert oa_ex["data"]["headline_median"] == SCORE_EXAMPLE["data"]["headline_median"]

    def test_stale_judgment_is_flagged(self, client, isolated_db):
        from app.services.compute import compute_snapshot, persist_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        persist_snapshot(data, raw)
        # mark the persisted snapshot's judgment stale (non-erroring)
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import Snapshot

        with session_scope() as session:
            snap = session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1)).scalars().first()
            snap.judgment_stale = True
            snap.judgment_error = None
        body = client.get("/api/v1/status").json()
        assert any(f["id"] == "judgment-stale" for f in body["science_audit"]["flags"])


class TestSnapshotAge:
    """The status document says when the number it shows is old (module
    docstring of app/services/status.py: the audit lists everything currently
    incomplete; README: "Live service + science-audit status"). "Old" is the
    outage alert's own condition, app/alerts/watchdog.py: two scheduled
    recomputes missed and 90 minutes more.

    Production served one snapshot for 60 h (from 2026-07-27), 24 h (08-01),
    344 h (08-06 to 08-20) and 16 h (08-28) while the audit listed no flag of
    category service-state and none that named the age."""

    @staticmethod
    def _snapshot_computed(hours_ago: float) -> None:
        from datetime import timedelta

        from sqlalchemy import update

        from app.db import session_scope
        from app.models import Snapshot
        from app.services.compute import compute_snapshot, persist_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        persist_snapshot(compute_snapshot(raw, mc_samples=2_000, mc_seed=1), raw)
        with session_scope() as session:
            session.execute(update(Snapshot).values(
                computed_at=datetime.now(UTC) - timedelta(hours=hours_ago)))

    @staticmethod
    def _flags(client) -> dict[str, dict]:
        return {f["id"]: f for f in client.get("/api/v1/status").json()["science_audit"]["flags"]}

    def test_a_snapshot_sixty_hours_old_is_flagged_as_an_error(self, client):
        self._snapshot_computed(60)
        body = client.get("/api/v1/status").json()
        flag = {f["id"]: f for f in body["science_audit"]["flags"]}["snapshot-old"]
        assert flag["severity"] == "error" and flag["category"] == "service-state"
        # it names the snapshot's own time and how many recomputes are missing
        assert body["snapshot"]["computed_at"] in flag["detail"]
        # 60 h hold 15 four-hourly slots (16 if one falls between this test's clock and the service's)
        assert any(f"{n} scheduled recomputes" in flag["detail"] for n in (15, 16))
        # errors lead the list the page renders
        assert body["science_audit"]["flags"][0]["severity"] == "error"

    @pytest.mark.parametrize("hours_ago", [0, 5])
    def test_a_snapshot_inside_the_schedule_is_not_flagged(self, client, hours_ago):
        # Five hours hold at most two slots, and the second is then at most an
        # hour old: inside the alert's 90-minute grace at any time of day.
        self._snapshot_computed(hours_ago)
        assert "snapshot-old" not in self._flags(client)

    def test_a_failed_last_recompute_is_flagged_without_its_error_text(self, client, monkeypatch):
        """The run's error text is an upstream's words; it stays in
        recompute.last and no word of it enters the flag."""
        from app.routers import admin

        self._snapshot_computed(0)
        monkeypatch.setitem(admin._last, "finished_at", "2026-11-11T02:01:00+00:00")
        monkeypatch.setitem(admin._last, "error", "provider said: NOT-FOR-A-FLAG")
        body = client.get("/api/v1/status").json()
        flag = {f["id"]: f for f in body["science_audit"]["flags"]}["recompute-failed"]
        assert flag["severity"] == "error" and flag["category"] == "service-state"
        assert "2026-11-11T02:01:00+00:00" in flag["detail"]
        assert "NOT-FOR-A-FLAG" not in flag["title"] + flag["detail"]
        assert body["recompute"]["last"]["error"] == "provider said: NOT-FOR-A-FLAG"

    def test_a_last_recompute_that_worked_is_not_flagged(self, client, monkeypatch):
        from app.routers import admin

        self._snapshot_computed(0)
        monkeypatch.setitem(admin._last, "error", None)
        assert "recompute-failed" not in self._flags(client)

    def test_the_no_snapshot_flag_reads_the_schedule_from_the_slot_table(self, client):
        """app/engine/recompute_slots.py: the schedule "must never be restated
        as a literal"; the flag spelt it "4-hourly (02/06/10/14/18/22 UTC)"."""
        from app.engine.recompute_slots import schedule_display

        assert schedule_display() in self._flags(client)["no-snapshot"]["detail"]


class TestFeedBehind:
    """DASHBOARD_FEED_SPEC.md section 4: the feed is built inside every
    recompute, strictly after the score persists, and when its build fails
    "the endpoint then serves the previous payload". The failed build is one
    log line (dashboard_feed_failed, app/services/compute.py) and the run
    counts as a success, so the status document showed an old "Feed built"
    time beside a fresh snapshot and listed no flag for it.

    Not seen in production: the 222 recomputes from 2026-10-05 to 11-10 logged
    222 dashboard_feed_persisted and 0 dashboard_feed_failed."""

    @staticmethod
    def _snapshot() -> None:
        from app.services.compute import compute_snapshot, persist_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        persist_snapshot(compute_snapshot(raw, mc_samples=2_000, mc_seed=1), raw)

    @staticmethod
    def _feed(at: datetime) -> None:
        import json

        from app.db import session_scope
        from app.models import DashboardFeed

        with session_scope() as session:
            session.add(DashboardFeed(computed_at=at,
                                      payload=json.dumps({"series": {}, "metrics": {}})))

    @staticmethod
    def _flags(client) -> dict[str, dict]:
        return {f["id"]: f for f in client.get("/api/v1/status").json()["science_audit"]["flags"]}

    def test_a_feed_older_than_the_snapshot_is_flagged(self, client):
        from datetime import timedelta

        self._feed(datetime.now(UTC) - timedelta(days=3))   # the last build that worked
        self._snapshot()                                    # recomputes have run since
        body = client.get("/api/v1/status").json()
        flag = {f["id"]: f for f in body["science_audit"]["flags"]}["feed-behind"]
        assert flag["severity"] == "warn" and flag["category"] == "service-state"
        # it names both times: the payload being served and the snapshot it missed
        assert body["feed_sources"]["computed_at"] in flag["detail"]
        assert body["snapshot"]["computed_at"] in flag["detail"]

    def test_a_snapshot_with_no_feed_at_all_is_flagged(self, client):
        self._snapshot()
        assert client.get("/api/v1/dashboard/feed").status_code == 503
        assert "never been built" in self._flags(client)["feed-behind"]["detail"]

    def test_a_feed_built_after_its_snapshot_raises_nothing(self, client):
        self._snapshot()
        self._feed(datetime.now(UTC))
        assert "feed-behind" not in self._flags(client)

    def test_a_run_in_flight_raises_nothing(self, client):
        """Between the snapshot's commit and the feed's the newest feed is the
        older of the two in every run, six times a day (the 14 payloads
        retained on 2026-11-10 are each 87 to 236 seconds younger than their
        snapshot: the snapshots of 14:00:50, 18:00:46 and 22:00:49 have feeds
        of 14:04:46, 18:02:23 and 22:02:32). The recompute lock covers the
        whole run, so the flag waits for its release."""
        from datetime import timedelta

        from app.routers.admin import recompute_lock

        self._feed(datetime.now(UTC) - timedelta(hours=4))
        self._snapshot()
        assert recompute_lock.acquire(blocking=False)
        try:
            assert "feed-behind" not in self._flags(client)
        finally:
            recompute_lock.release()
        assert "feed-behind" in self._flags(client)

    def test_a_run_that_ends_while_the_document_is_read_raises_nothing(self, client, monkeypatch):
        """The document reads the snapshot, then the lock, then the feed. A
        run that commits its feed and releases the lock in the instant before
        the lock is asked is seen whole, because the feed is read after the
        lock's state: read before it, the old feed would stand beside a
        released lock and the flag would show for one request."""
        from datetime import timedelta

        from app.routers import admin

        self._feed(datetime.now(UTC) - timedelta(hours=4))
        self._snapshot()                        # the run in flight has committed its snapshot
        write_feed = self._feed

        class TheRunEndsNow:
            @staticmethod
            def locked() -> bool:
                write_feed(datetime.now(UTC))   # its feed is committed
                return False                    # and its lock released

        monkeypatch.setattr(admin, "recompute_lock", TheRunEndsNow())
        body = client.get("/api/v1/status").json()
        assert body["recompute"]["running"] is False
        assert "feed-behind" not in {f["id"] for f in body["science_audit"]["flags"]}

    def test_no_snapshot_no_flag(self, client):
        from datetime import timedelta

        assert "feed-behind" not in self._flags(client)
        self._feed(datetime.now(UTC) - timedelta(days=3))
        assert "feed-behind" not in self._flags(client)
