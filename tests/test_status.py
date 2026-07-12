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
        for k in ("service", "snapshot", "recompute", "sources", "providers", "indicators",
                  "science_audit", "falsification_criteria", "changelog", "epistemic_caveats",
                  "disclaimer"):
            assert k in body, f"missing {k}"
        assert body["snapshot"] is None  # no recompute yet
        assert len(body["indicators"]) == 10
        assert len(body["sources"]) == 10
        # every source is "unknown" before the first pull
        assert all(s["status"] == "unknown" for s in body["sources"])
        assert body["service"]["docs"]["swagger"] == "/docs"

    def test_known_issues_always_flagged(self, client):
        audit = client.get("/api/v1/status").json()["science_audit"]
        cats = {f["category"] for f in audit["flags"]}
        # the scientific-correctness catalogue is always present
        assert "citation-unverified" in cats
        assert "contested-method" in cats
        assert "documented-deviation" in cats  # the alpha-range deviation
        assert "data-substitution" in cats     # ETF index proxies
        # unverified citations surface as individual flags
        assert sum(1 for f in audit["flags"] if f["category"] == "citation-unverified") == 3
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
        assert ex["data"]["headline_median"] == 40
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
