"""Content API contract: slugs complete, constraints regexable, envelope honest."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import content_registry
from app.references import DISCLAIMER, EPISTEMIC_CAVEATS


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


class TestDashboardContent:
    def test_envelope_and_caveats(self, client):
        r = client.get("/api/v1/content/dashboard")
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"data", "meta"}
        assert body["meta"]["epistemic_caveats"] == list(EPISTEMIC_CAVEATS)
        assert body["meta"]["computed_at"] is None
        assert "max-age=300" in r.headers.get("cache-control", "")

    def test_every_registry_slug_served_and_nonempty(self, client):
        blocks = client.get("/api/v1/content/dashboard").json()["data"]["blocks"]
        assert set(blocks.keys()) == set(content_registry.static_blocks().keys())
        for slug, block in blocks.items():
            kind = block["kind"]
            if kind in ("text", "template"):
                # template = text carrying {placeholder} slots the client fills
                # (Q9 interim design); both must be non-empty strings.
                assert isinstance(block["text"], str) and block["text"].strip(), slug
            elif kind in ("links", "endpoint_catalog", "list", "table"):
                assert isinstance(block["items"], list) and block["items"], slug
            elif kind == "map":
                assert isinstance(block["entries"], dict) and block["entries"], slug
            else:  # a new kind must be added to this contract deliberately
                pytest.fail(f"unknown block kind {kind!r} for {slug}")

    def test_disclaimer_is_the_canonical_text(self, client):
        blocks = client.get("/api/v1/content/dashboard").json()["data"]["blocks"]
        served = blocks["disclaimer_full"]["text"]
        assert served == DISCLAIMER.replace("**", "")
        assert "not investment advice" in served

    def test_no_unresolved_interpolation(self, client):
        # {identifier}-shaped placeholders only; literal braces in prose (the
        # disclaimer's reference class {1929, ...}) are legitimate.
        blocks = client.get("/api/v1/content/dashboard").json()["data"]["blocks"]
        for slug, block in blocks.items():
            if block["kind"] == "text":
                assert not re.search(r"\{[a-z_]+\}", block["text"]), (
                    f"unresolved placeholder in {slug}"
                )

    def test_empty_snapshot_carries_the_derived_schedule(self, client):
        from app.engine.recompute_slots import schedule_display

        blocks = client.get("/api/v1/content/dashboard").json()["data"]["blocks"]
        assert schedule_display() in blocks["empty.snapshot"]["text"]

    def test_endpoint_catalogue_paths_exist_in_openapi(self, client):
        # Compare with path parameters normalized ({id} vs {indicator_id}).
        def norm(p: str) -> str:
            return re.sub(r"\{[^}]+\}", "{}", p)

        paths = {norm(p) for p in client.get("/openapi.json").json()["paths"]}
        documented_outside_schema = {"/readyz"}  # served, deliberately schema-hidden
        for item in client.get("/api/v1/content/dashboard").json()["data"]["blocks"][
            "api.endpoints"
        ]["items"]:
            path = item["path"]
            assert (
                norm(path) in paths or path in documented_outside_schema
            ), f"catalogued endpoint {path} not in the OpenAPI schema"

    def test_doc_links_are_same_origin(self, client):
        for link in client.get("/api/v1/content/dashboard").json()["data"]["blocks"][
            "api.doc_links"
        ]["items"]:
            assert link["href"].startswith("/"), link


class TestDynamicContent:
    def test_slots_have_valid_contracts_and_conforming_placeholders(self, client):
        r = client.get("/api/v1/content/dynamic")
        assert r.status_code == 200
        slots = r.json()["data"]["slots"]
        assert set(slots.keys()) == {s.slug for s in content_registry.DYNAMIC_SLOTS}
        for slug, slot in slots.items():
            constraints = slot["constraints"]
            assert constraints["max_len"] > 0
            compiled = re.compile(constraints["regex"])  # must compile
            assert slot["source"] == "placeholder"
            assert slot["updated_at"] is None
            text = slot["text"]
            assert len(text) <= constraints["max_len"], slug
            assert compiled.fullmatch(text), f"placeholder violates its own contract: {slug}"

    def test_cache_header(self, client):
        r = client.get("/api/v1/content/dynamic")
        assert "max-age=60" in r.headers.get("cache-control", "")


class TestCacheScope:
    def test_public_while_read_surface_is_public(self, client):
        # TESTING default: READ_ENDPOINTS_PUBLIC=true -> shared caching is fine.
        r = client.get("/api/v1/content/dashboard")
        assert r.headers["cache-control"].startswith("public, ")

    def test_private_when_reads_are_keyed(self, monkeypatch):
        # A keyed reply must never be shared-cacheable (panel finding, PR #95).
        from app.routers import content as content_router

        class _Keyed:
            read_endpoints_public = False

        monkeypatch.setattr(content_router, "get_settings", lambda: _Keyed())
        assert content_router._cache_control(300) == "private, max-age=300"


class TestBlockArtifact:
    """The versioned content-block artifact (config/content_blocks.v1.json)."""

    def _with_artifact(self, monkeypatch, artifact):
        import app.content_registry as reg

        reg._file_artifact.cache_clear()
        monkeypatch.setattr(reg, "_file_artifact", lambda: artifact)
        return reg

    def test_file_blocks_merge_without_overriding_builtins(self, monkeypatch):
        reg = self._with_artifact(monkeypatch, {"content_version": 3, "blocks": {
            "atlas.matrix.legend": {"kind": "text", "text": "example"},
            "disclaimer_full": {"kind": "text", "text": "MUST NOT WIN"},
        }})
        blocks = reg.static_blocks()
        assert blocks["atlas.matrix.legend"]["text"] == "example"
        assert "MUST NOT WIN" not in blocks["disclaimer_full"]["text"]
        assert reg.content_version() == 3

    def test_gauge_display_prefix_extraction(self, monkeypatch):
        reg = self._with_artifact(monkeypatch, {"content_version": 1, "blocks": {
            "gauge.band.oneliner": {"kind": "text", "text": "example copy"},
            "site.intro": {"kind": "text", "text": "not gauge"},
        }})
        display = reg.gauge_display()
        assert display == {"band.oneliner": {"kind": "text", "text": "example copy"}}

    def test_shipped_artifact_actually_feeds_the_display_deck(self):
        # Panel finding (PR-1a round 1): the deck must be verified against the
        # REAL shipped artifact, never a fabricated slug — Q8 may not be dead
        # on arrival while the artifact ships in the same PR.
        import app.content_registry as reg

        reg._file_artifact.cache_clear()
        display = reg.gauge_display()
        assert display, "score data.display is empty with the shipped artifact"
        assert "band.oneliner" in display
        reg._file_artifact.cache_clear()

    def test_missing_file_serves_builtins_with_version_zero(self, monkeypatch):
        import app.content_registry as reg

        reg._file_artifact.cache_clear()
        monkeypatch.setattr(reg, "_BLOCKS_FILE", reg._BLOCKS_FILE.with_name("nope.json"))
        reg._file_artifact.cache_clear()
        assert reg.content_version() == 0
        assert "disclaimer_full" in reg.static_blocks()
        reg._file_artifact.cache_clear()
