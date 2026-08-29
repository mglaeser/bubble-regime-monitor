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

        reg._clear_artifact_cache()
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

        reg._clear_artifact_cache()
        display = reg.gauge_display()
        assert display, "score data.display is empty with the shipped artifact"
        assert "band.oneliner" in display
        reg._clear_artifact_cache()

    def test_missing_file_serves_builtins_with_version_zero(self, monkeypatch):
        import app.content_registry as reg

        reg._clear_artifact_cache()
        monkeypatch.setattr(reg, "_BLOCKS_FILE", reg._BLOCKS_FILE.with_name("nope.json"))
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        assert "disclaimer_full" in reg.static_blocks()
        reg._clear_artifact_cache()

    def test_malformed_artifact_never_couples_to_requests(self, monkeypatch, tmp_path):
        # Panel finding (PR #97): a corrupt artifact file must degrade to
        # built-ins at version 0, never raise into a request handler.
        import app.content_registry as reg

        bad = tmp_path / "content_blocks.v1.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", bad)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        assert "disclaimer_full" in reg.static_blocks()
        reg._clear_artifact_cache()

    def test_every_band_shaped_map_keys_real_band_values(self):
        # Panel findings (PR #97 rounds 2 + 7): EVERY band-keyed map in the
        # artifact must key the REAL bubblegauge band strings — a 'derisk'
        # key makes the highest-severity band miss the lookup (and ||hold
        # client patterns would show HOLD). Round 7 caught the sibling map
        # this test previously masked by pinning only band.oneliner.
        import app.content_registry as reg

        reg._clear_artifact_cache()
        blocks = reg.static_blocks()
        # Round 10: detection-by-key self-excludes a map MISSING those keys —
        # the known band maps are pinned by SLUG and must be band-shaped.
        KNOWN_BAND_MAPS = ("gauge.band.oneliner", "gauge.splash.band_blurb")
        for slug in KNOWN_BAND_MAPS:
            assert blocks.get(slug, {}).get("kind") == "map", f"{slug} missing or not a map"
        band_maps = [
            (slug, block["entries"])
            for slug, block in blocks.items()
            if block.get("kind") == "map"
            and (slug in KNOWN_BAND_MAPS
                 or ("hold" in block.get("entries", {}) and "trim" in block["entries"]))
        ]
        assert band_maps, "no band-shaped maps found - artifact regression?"
        for slug, entries in band_maps:
            for band in ("hold", "trim", "de-risk", "suppressed (block degraded)", "fallback"):
                assert band in entries, f"{slug} missing band key {band!r}"
            assert "derisk" not in entries, f"{slug} carries the broken 'derisk' key"
        reg._clear_artifact_cache()

    def test_structurally_invalid_blocks_degrades_wholly_to_v0(self, monkeypatch, tmp_path):
        # Round 3: blocks-as-list must not serve built-ins while STILL
        # advertising the artifact's version — the whole artifact degrades.
        import app.content_registry as reg

        bad = tmp_path / "content_blocks.v1.json"
        bad.write_text('{"content_version": 1, "blocks": []}', encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", bad)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_hostile_deep_nesting_never_escapes(self, monkeypatch, tmp_path):
        # Round 3: a deeply nested JSON file raises RecursionError inside
        # json.load — it must degrade to v0, never crash a request handler.
        import app.content_registry as reg

        bad = tmp_path / "content_blocks.v1.json"
        bad.write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", bad)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        assert "disclaimer_full" in reg.static_blocks()
        reg._clear_artifact_cache()

    def test_site_disclaimer_alias_for_companion_contract(self):
        # The companion dashboard's frozen contract gates on this exact slug
        # and phrase; its deploy-time fallback generator exits 1 without it.
        import app.content_registry as reg

        reg._clear_artifact_cache()
        block = reg.static_blocks()["site.disclaimer"]
        assert "not investment advice" in block["text"]
        reg._clear_artifact_cache()

    def test_member_corruption_degrades_whole_artifact(self, monkeypatch, tmp_path):
        # Round 4: partial content must never be served under the artifact's
        # version — one corrupt member degrades the whole artifact to v0.
        import app.content_registry as reg

        bad = tmp_path / "content_blocks.v1.json"
        bad.write_text('{"content_version": 1, "blocks": {'
                       '"good.block": {"kind": "text", "text": "fine"},'
                       '"bad.block": {"kind": "text", "text": ""}}}',
                       encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", bad)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        assert "good.block" not in reg.static_blocks()
        reg._clear_artifact_cache()

    def test_lone_surrogate_rejected_at_load_not_at_response(self, monkeypatch, tmp_path):
        # Round 4: a lone UTF-16 surrogate passes json.load but raises
        # UnicodeEncodeError in the RESPONSE encoder — reject at load.
        import app.content_registry as reg

        bad = tmp_path / "content_blocks.v1.json"
        bad.write_text('{"content_version": 1, "blocks": {'
                       '"x.y": {"kind": "text", "text": "bad \\ud800 char"}}}',
                       encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", bad)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_boolean_version_rejected(self, monkeypatch, tmp_path):
        # Round 4: bool is an int subclass — `true` must not pass as version.
        import app.content_registry as reg

        bad = tmp_path / "content_blocks.v1.json"
        bad.write_text('{"content_version": true, "blocks": {'
                       '"x.y": {"kind": "text", "text": "fine"}}}', encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", bad)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_non_finite_numbers_rejected_at_load(self, monkeypatch, tmp_path):
        # Round 5: python json accepts Infinity/NaN; the response encoder
        # (allow_nan=False) then 500s — reject at load.
        import app.content_registry as reg

        bad = tmp_path / "content_blocks.v1.json"
        bad.write_text('{"content_version": 1, "blocks": {'
                       '"x.y": {"kind": "table", "items": [{"v": Infinity}]}}}',
                       encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", bad)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_nested_item_corruption_degrades_whole_artifact(self, monkeypatch, tmp_path):
        # Round 5: a table holding a string item is corrupt — the all-or-
        # nothing invariant applies to ITEMS, not just containers.
        import app.content_registry as reg

        bad = tmp_path / "content_blocks.v1.json"
        bad.write_text('{"content_version": 1, "blocks": {'
                       '"x.y": {"kind": "table", "items": [{"a": 1}, "corrupt"]}}}',
                       encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", bad)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_exponent_overflow_infinity_rejected(self, monkeypatch, tmp_path):
        # Round 6: 1e400 parses to float('inf') via parse_float (bypassing
        # parse_constant); the strict round-trip must reject it at load.
        import app.content_registry as reg

        bad = tmp_path / "content_blocks.v1.json"
        bad.write_text('{"content_version": 1, "blocks": {'
                       '"x.y": {"kind": "table", "items": [{"v": 1e400}]}}}',
                       encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", bad)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_depth_bound_covers_wrapped_response_margin(self, monkeypatch, tmp_path):
        # Round 8: preflight dumped the RAW artifact while responses serialize
        # it WRAPPED — depth is now bounded explicitly (32), iteratively,
        # independent of interpreter recursion limits.
        import json as _json

        import app.content_registry as reg

        deep: dict = {"kind": "map", "entries": {"k": "v"}}
        nested: object = "leaf"
        for _ in range(40):
            nested = [nested]
        deep_block = {"kind": "table", "items": [{"v": nested}]}
        bad = tmp_path / "content_blocks.v1.json"
        bad.write_text(_json.dumps({"content_version": 1, "blocks": {
            "ok.map": deep, "deep.block": deep_block}}), encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", bad)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_verdict_tables_never_map_unknown_to_hold(self):
        # Round 9: the source generator conflated 'hold (and any unknown
        # band)' — an unrecognized band rendered HOLD copy with action
        # framing. Every verdict table must carry an explicit 'unknown' row
        # with non-action copy, and no row may bundle unknown with a band.
        import app.content_registry as reg

        reg._clear_artifact_cache()
        blocks = reg.static_blocks()
        for table in ("gauge.verdict.lead", "gauge.verdict.detail"):
            bands = [row.get("band") for row in blocks[table]["items"]]
            assert "unknown" in bands, f"{table} lacks an explicit unknown row"
            for band in bands:
                assert band is None or "unknown" not in band or band == "unknown", (
                    f"{table} bundles unknown into row {band!r}"
                )
            unknown_rows = [r for r in blocks[table]["items"] if r.get("band") == "unknown"]
            for row in unknown_rows:
                text = row.get("template", "").lower()
                for verb in ("de-risk now", "trim now", "hold —", "hold -"):
                    assert verb not in text, f"{table} unknown row carries action copy"
        reg._clear_artifact_cache()

    def test_truncated_artifact_fails_completeness_attestation(self, monkeypatch, tmp_path):
        # Round 10: a valid-member subset must not serve under the artifact's
        # version — block_count self-attestation catches truncation.
        import app.content_registry as reg

        bad = tmp_path / "content_blocks.v1.json"
        bad.write_text('{"content_version": 1, "block_count": 5, "blocks": {'
                       '"x.y": {"kind": "text", "text": "only one"}}}', encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", bad)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_shipped_artifact_attests_its_own_completeness(self):
        import json as _json

        import app.content_registry as reg

        raw = _json.loads(reg._BLOCKS_FILE.read_text(encoding="utf-8"))
        assert raw["block_count"] == len(raw["blocks"]) >= 211

    def test_runtime_artifact_change_is_reexamined(self, monkeypatch, tmp_path):
        # Round 11 (SOTA-C): lru_cache froze artifact health at first load —
        # the mtime/size-keyed cache must re-examine a changed/vanished file.
        import os

        import app.content_registry as reg

        f = tmp_path / "content_blocks.v1.json"
        good = ('{"content_version": 2, "block_count": 5, "blocks": {'
                '"gauge.band.oneliner": {"kind": "map", "entries": {"hold": "h"}},'
                '"gauge.splash.band_blurb": {"kind": "map", "entries": {"hold": "h"}},'
                '"gauge.verdict.lead": {"kind": "table", "items": [{"a": "b"}]},'
                '"gauge.verdict.detail": {"kind": "table", "items": [{"a": "b"}]},'
                '"gauge.verdict.distance": {"kind": "table", "items": [{"a": "b"}]}}}')
        f.write_text(good, encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", f)
        reg._clear_artifact_cache()
        assert reg.content_version() == 2
        f.write_text("{corrupt", encoding="utf-8")
        os.utime(f, (1, 1))  # force a different mtime signature
        assert reg.content_version() == 0, "runtime corruption must be re-examined"
        reg._clear_artifact_cache()

    def test_junk_artifact_with_matching_count_fails_required_slugs(self, monkeypatch, tmp_path):
        # Round 11 (SOTA-A): count-consistent junk must fail the code-anchored
        # required-slug manifest.
        import app.content_registry as reg

        f = tmp_path / "content_blocks.v1.json"
        f.write_text('{"content_version": 1, "block_count": 1, "blocks": {'
                     '"junk.slug": {"kind": "text", "text": "junk"}}}', encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", f)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_shipped_artifact_passes_the_full_loader(self):
        # Round 12: the CI pin must exercise the REAL loader end-to-end on the
        # shipped file — raw-JSON reads can short-circuit loader regressions.
        import app.content_registry as reg

        reg._clear_artifact_cache()
        assert reg.content_version() == 1
        payload = reg.dashboard_payload()
        assert payload["content_version"] == 1
        assert len(payload["blocks"]) >= 211
        reg._clear_artifact_cache()

    def test_dashboard_payload_is_one_snapshot(self, monkeypatch):
        # Round 12: blocks and version must come from a SINGLE artifact read.
        import app.content_registry as reg

        calls = {"n": 0}
        real = reg.artifact_view

        def counting():
            calls["n"] += 1
            return real()

        monkeypatch.setattr(reg, "artifact_view", counting)
        reg._clear_artifact_cache()
        reg.dashboard_payload()
        assert calls["n"] == 1
        reg._clear_artifact_cache()

    def test_required_slug_with_wrong_kind_degrades(self, monkeypatch, tmp_path):
        # Round 12: presence alone is porous — a required slug of the wrong
        # KIND must degrade the artifact.
        import app.content_registry as reg

        f = tmp_path / "content_blocks.v1.json"
        f.write_text('{"content_version": 1, "block_count": 5, "blocks": {'
                     '"gauge.band.oneliner": {"kind": "text", "text": "wrong kind"},'
                     '"gauge.splash.band_blurb": {"kind": "map", "entries": {"hold": "h"}},'
                     '"gauge.verdict.lead": {"kind": "table", "items": [{"a": "b"}]},'
                     '"gauge.verdict.detail": {"kind": "table", "items": [{"a": "b"}]},'
                     '"gauge.verdict.distance": {"kind": "table", "items": [{"a": "b"}]}}}',
                     encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", f)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_malformed_slug_degrades_whole_artifact(self, monkeypatch, tmp_path):
        # Round 13: slug shape is schema — a slug with spaces/notes must
        # degrade the artifact (this check caught a real defect in the
        # shipped artifact on first contact).
        import app.content_registry as reg

        f = tmp_path / "content_blocks.v1.json"
        f.write_text('{"content_version": 1, "block_count": 6, "blocks": {'
                     '"bad slug (with note)": {"kind": "text", "text": "x"},'
                     '"gauge.band.oneliner": {"kind": "map", "entries": {"hold": "h"}},'
                     '"gauge.splash.band_blurb": {"kind": "map", "entries": {"hold": "h"}},'
                     '"gauge.verdict.lead": {"kind": "table", "items": [{"a": "b"}]},'
                     '"gauge.verdict.detail": {"kind": "table", "items": [{"a": "b"}]},'
                     '"gauge.verdict.distance": {"kind": "table", "items": [{"a": "b"}]}}}',
                     encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", f)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_band_vocabulary_closed_across_structures(self):
        # Round 14: every band key the band maps declare must resolve to a
        # row in the band-keyed verdict tables — vocabulary closure, so no
        # state silently falls to generic copy. And no extractor scaffolding
        # (_placeholder) may ship in any served table.
        import app.content_registry as reg

        reg._clear_artifact_cache()
        blocks = reg.static_blocks()
        declared = set(blocks["gauge.band.oneliner"]["entries"].keys())
        for table in ("gauge.verdict.lead", "gauge.verdict.detail"):
            rows = {r.get("band") for r in blocks[table]["items"]}
            missing = declared - rows
            assert not missing, f"{table} lacks rows for declared bands: {missing}"
        for slug, block in blocks.items():
            for item in block.get("items", []) if isinstance(block.get("items"), list) else []:
                assert "_placeholder" not in str(item), (
                    f"extractor scaffolding shipped in {slug}"
                )
        reg._clear_artifact_cache()
