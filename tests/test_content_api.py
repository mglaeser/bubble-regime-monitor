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

    def test_fstat_failure_degrades_never_500s(self, monkeypatch):
        # Round 25 (SOTA-A): the try/except covered open() but NOT the
        # os.fstat on the descriptor — an EIO/revoked-fd stat escaped into
        # score/dashboard/dynamic as a 500. Every filesystem-level fault
        # must degrade to built-ins at v0.
        import os as _os

        import app.content_registry as reg

        def boom(_fd):
            raise OSError(5, "Input/output error")

        reg._clear_artifact_cache()
        monkeypatch.setattr(_os, "fstat", boom)
        assert reg.content_version() == 0
        assert "disclaimer_full" in reg.static_blocks()
        assert reg.dashboard_payload()["content_version"] == 0
        reg._clear_artifact_cache()

    def test_fstat_failure_keeps_routes_serving(self, client, monkeypatch):
        # The route-level half of the same guarantee.
        import os as _os

        import app.content_registry as reg

        def boom(_fd):
            raise OSError(5, "Input/output error")

        reg._clear_artifact_cache()
        monkeypatch.setattr(_os, "fstat", boom)
        for path in ("/api/v1/content/dashboard", "/api/v1/content/dynamic"):
            assert client.get(path).status_code == 200, path
        reg._clear_artifact_cache()

    def test_close_failure_degrades_never_500s(self, monkeypatch, tmp_path):
        # Sibling of the round-25 finding, found by sweeping the class: the
        # implicit close() at the end of `with fh:` can raise EIO too. The
        # guard spans the whole descriptor lifetime, so it degrades as well.
        import app.content_registry as reg

        real_open = type(reg._BLOCKS_FILE).open

        class BadClose:
            def __init__(self, fh):
                self._fh = fh

            def __getattr__(self, name):
                return getattr(self._fh, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                raise OSError(5, "Input/output error")

        def open_badclose(self, *a, **kw):
            return BadClose(real_open(self, *a, **kw))

        reg._clear_artifact_cache()
        monkeypatch.setattr(type(reg._BLOCKS_FILE), "open", open_badclose)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_transient_read_error_is_not_cached_under_a_valid_key(
            self, monkeypatch, tmp_path):
        # Round 26 (SOTA-A): a read EIO was caught INSIDE _load_artifact and
        # returned as a degraded artifact, which _file_artifact then cached
        # under the file's (mtime,size,inode) key. Recovery does not change
        # that key, so one transient fault pinned v0/empty gauge until the
        # artifact was rewritten or the process restarted.
        #
        # The fault is injected at the REAL read (fh.read, where json.load
        # pulls bytes) — NOT by monkeypatching _load_artifact, which would
        # bypass the inner except clause this test exists to pin. The
        # full-control audit caught exactly that vacuity in the first draft.
        import app.content_registry as reg

        art = self._baseline()
        reg_mod = self._install(monkeypatch, tmp_path, art)
        path_type = type(reg._BLOCKS_FILE)
        real_open = path_type.open
        state = {"fail": True}

        class FlakyRead:
            def __init__(self, fh):
                self._fh = fh

            def __getattr__(self, name):
                return getattr(self._fh, name)

            def __enter__(self):
                self._fh.__enter__()
                return self

            def __exit__(self, *exc):
                return self._fh.__exit__(*exc)

            def read(self, *a, **kw):
                if state["fail"]:
                    state["fail"] = False
                    raise OSError(5, "Input/output error")
                return self._fh.read(*a, **kw)

        def flaky_open(self, *a, **kw):
            return FlakyRead(real_open(self, *a, **kw))

        monkeypatch.setattr(path_type, "open", flaky_open)
        assert reg_mod.content_version() == 0, "transient fault must degrade"
        # The file has NOT changed — same mtime, size, inode. Recovery alone
        # must be enough; nothing else may be required of an operator.
        assert reg_mod.content_version() == 1, "recovery must not need a rewrite"
        reg._clear_artifact_cache()

    def test_content_fault_stays_cached_under_its_key(self, monkeypatch, tmp_path):
        # The other half of the same contract: a CONTENT fault is
        # deterministic, so it must keep caching under the real key — no
        # re-parsing corrupt bytes on every request.
        import app.content_registry as reg

        art = self._baseline()
        art["blocks"]["hero.intro"]["text"] = ""
        reg_mod = self._install(monkeypatch, tmp_path, art)
        real_load = reg._load_artifact
        calls = {"n": 0}

        def counting(fh):
            calls["n"] += 1
            return real_load(fh)

        monkeypatch.setattr(reg, "_load_artifact", counting)
        assert reg_mod.content_version() == 0
        assert reg_mod.content_version() == 0
        assert calls["n"] == 1, "a deterministic content fault must stay cached"
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
            for band in ("hold", "trim", "de-risk", "suppressed",
                         "suppressed (block degraded)", "fallback", "unknown"):
                assert band in entries, f"{slug} missing band key {band!r}"
            assert "derisk" not in entries, f"{slug} carries the broken 'derisk' key"
        reg._clear_artifact_cache()

    # ---- Round 17 (SOTA-A): every hostile test below builds on a baseline
    # that passes EVERY loader control, then applies exactly ONE violation.
    # The original tiny fixtures ({"blocks": {"x.y": ...}}) predated the
    # completeness controls and had become vacuous: they
    # degraded for several reasons at once, so deleting the specific control
    # a test claimed to pin kept CI green. The positive control
    # (test_baseline_fixture_passes_the_full_loader) keeps the battery honest.

    def _baseline(self, version: int = 1) -> dict:
        # Round 18: the baseline covers the FULL code-anchored manifest —
        # completeness now means every v1 slug with its declared kind, so the
        # builder derives from app/content_manifest.py, then overwrites the
        # five band-structured slugs with schema-complete band structures.
        import app.content_registry as reg

        shapes = {
            "text": {"kind": "text", "text": "x"},
            "template": {"kind": "template", "text": "x"},
            "map": {"kind": "map", "entries": {"k": "v"}},
            "list": {"kind": "list", "items": ["x"]},
            "links": {"kind": "links", "items": [{"a": "b"}]},
            "endpoint_catalog": {"kind": "endpoint_catalog", "items": [{"a": "b"}]},
            "table": {"kind": "table", "items": [{"a": "b"}]},
        }
        import copy

        blocks: dict = {slug: copy.deepcopy(shapes[kind])
                        for slug, kind in reg.REQUIRED_FILE_SLUG_KINDS.items()}
        bands = sorted(reg._CANONICAL_BANDS)
        blocks["gauge.band.oneliner"] = {
            "kind": "map", "entries": {b: f"one {b}" for b in bands}}
        blocks["gauge.splash.band_blurb"] = {
            "kind": "map", "entries": {b: f"blurb {b}" for b in bands}}
        for t in ("gauge.verdict.lead", "gauge.verdict.detail"):
            blocks[t] = {"kind": "table", "items": [
                {"band": b, "trend_broken": "any", "template": "t"} for b in bands]}
        blocks["gauge.verdict.distance"] = {
            "kind": "table", "items": [{"case": "c", "template": "t"}]}
        return {"content_version": version, "block_count": len(blocks),
                "blocks": blocks}

    def _install(self, monkeypatch, tmp_path, artifact=None, raw=None,
                 fix_count=True):
        import json as _json

        import app.content_registry as reg

        if raw is None:
            if fix_count:
                artifact["block_count"] = len(artifact["blocks"])
            # ensure_ascii=True keeps lone surrogates writable as escapes.
            raw = _json.dumps(artifact)
        f = tmp_path / "content_blocks.v1.json"
        f.write_text(raw, encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", f)
        reg._clear_artifact_cache()
        return reg

    def test_baseline_fixture_passes_the_full_loader(self, monkeypatch, tmp_path):
        # POSITIVE CONTROL: the baseline loads. Each hostile test is this
        # baseline plus one violation, so each fails iff its control is
        # removed from the loader — no fixture short-circuits (round 17).
        reg = self._install(monkeypatch, tmp_path, self._baseline())
        assert reg.content_version() == 1
        assert reg.static_blocks()["hero.intro"]["text"] == "x"
        reg._clear_artifact_cache()

    def test_structurally_invalid_blocks_degrades_wholly_to_v0(self, monkeypatch, tmp_path):
        # Round 3: blocks-as-list must not serve built-ins while STILL
        # advertising the artifact's version — the whole artifact degrades.
        art = self._baseline()
        art["blocks"] = list(art["blocks"].values())
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_hostile_deep_nesting_never_escapes(self, monkeypatch, tmp_path):
        # Round 3: a deeply nested JSON file raises RecursionError inside
        # json.load — it must degrade to v0, never crash a request handler.
        # (Tiny raw fixture is correct here: the crash happens AT PARSE,
        # before any validation control can run.)
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
        # version — ONE corrupt member (empty text) degrades the whole
        # artifact to v0.
        art = self._baseline()
        art["blocks"]["hero.intro"]["text"] = ""
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        assert "hero.howto.expl" not in reg.static_blocks()
        reg._clear_artifact_cache()

    def test_lone_surrogate_rejected_at_load_not_at_response(self, monkeypatch, tmp_path):
        # Round 4: a lone UTF-16 surrogate passes json.load but raises
        # UnicodeEncodeError in the RESPONSE encoder — reject at load.
        art = self._baseline()
        art["blocks"]["hero.intro"]["text"] = "bad \ud800 char"
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_boolean_version_rejected(self, monkeypatch, tmp_path):
        # Round 4: bool is an int subclass — `true` must not pass as version.
        art = self._baseline()
        art["content_version"] = True
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_non_finite_numbers_rejected_at_load(self, monkeypatch, tmp_path):
        # Round 5: python json accepts Infinity/NaN; the response encoder
        # (allow_nan=False) then 500s — reject at load (parse_constant).
        # Round 22: fixture moved to an ORDINARY table — the distance table
        # gained its own row schema (round 18), which was rejecting this
        # fixture for an unrelated reason and masking the control.
        import json as _json

        art = self._baseline()
        art["blocks"]["analytics.fans"]["items"].append({"v": 123456789})
        raw = _json.dumps(art).replace("123456789", "Infinity")
        reg = self._install(monkeypatch, tmp_path, raw=raw)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_nested_item_corruption_degrades_whole_artifact(self, monkeypatch, tmp_path):
        # Round 5: a table holding a string item is corrupt — the all-or-
        # nothing invariant applies to ITEMS, not just containers.
        art = self._baseline()
        art["blocks"]["analytics.fans"]["items"].append("corrupt")
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_exponent_overflow_infinity_rejected(self, monkeypatch, tmp_path):
        # Round 6: 1e400 parses to float('inf') via parse_float (bypassing
        # parse_constant); the strict round-trip must reject it at load.
        # Round 22: moved to an ordinary table (see non_finite above).
        import json as _json

        art = self._baseline()
        art["blocks"]["analytics.fans"]["items"].append({"v": 123456789})
        raw = _json.dumps(art).replace("123456789", "1e400")
        reg = self._install(monkeypatch, tmp_path, raw=raw)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_depth_bound_covers_wrapped_response_margin(self, monkeypatch, tmp_path):
        # Round 8: preflight dumped the RAW artifact while responses serialize
        # it WRAPPED — depth is now bounded explicitly (32), iteratively,
        # independent of interpreter recursion limits.
        art = self._baseline()
        nested: object = "leaf"
        for _ in range(40):
            nested = [nested]
        art["blocks"]["analytics.fans"]["items"].append({"v": nested})
        reg = self._install(monkeypatch, tmp_path, art)
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
        art = self._baseline()
        art["block_count"] = len(art["blocks"]) - 1
        reg = self._install(monkeypatch, tmp_path, art, fix_count=False)
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
        # Round 17: rebuilt on the full valid baseline; no production
        # constant is monkeypatched away any more.
        import os

        art = self._baseline(version=2)
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 2
        f = tmp_path / "content_blocks.v1.json"
        f.write_text("{corrupt", encoding="utf-8")
        os.utime(f, (1, 1))  # force a different mtime signature
        assert reg.content_version() == 0, "runtime corruption must be re-examined"
        reg._clear_artifact_cache()

    def test_missing_required_slug_fails_manifest(self, monkeypatch, tmp_path):
        # Round 11 (SOTA-A): count-consistent junk must fail the code-anchored
        # required-slug manifest. Round 22: an ordinary slug — a deleted
        # distance table also trips the closure, which was masking this.
        art = self._baseline()
        del art["blocks"]["analytics.fans"]
        art["blocks"]["junk.extra"] = {"kind": "text", "text": "junk"}
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_wrong_kind_for_required_slug_rejected(self, monkeypatch, tmp_path):
        # The manifest pins KIND, not just presence. Round 22: an ORDINARY
        # slug — the band-closure checks also reject a broken distance table,
        # which was masking this control.
        art = self._baseline()
        art["blocks"]["analytics.fans"] = {"kind": "text", "text": "not a table"}
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_structured_junk_slug_rejected(self, monkeypatch, tmp_path):
        # Round 16: slugs are structurally validated (lowercase dotted path).
        art = self._baseline()
        art["blocks"]["BAD SLUG!"] = {"kind": "text", "text": "x"}
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_count_only_artifact_with_fillers_degrades(self, monkeypatch, tmp_path):
        # Round 18 (SOTA-A): the completeness gate was count-plus-five-slugs —
        # THIS artifact (five valid band slugs + 206 junk fillers, count
        # consistent) served at v1 with every remaining editorial block
        # missing. It must degrade under the full manifest.
        art = self._baseline()
        keep = ("gauge.band.oneliner", "gauge.splash.band_blurb",
                "gauge.verdict.lead", "gauge.verdict.detail",
                "gauge.verdict.distance")
        n_dropped = len(art["blocks"]) - len(keep)
        art["blocks"] = {s: b for s, b in art["blocks"].items() if s in keep}
        for i in range(n_dropped):
            art["blocks"][f"filler.block{i:03d}"] = {"kind": "text", "text": f"f {i}"}
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_missing_single_editorial_slug_degrades(self, monkeypatch, tmp_path):
        # Round 18: completeness is per-slug, not statistical — ONE missing
        # editorial block (count kept consistent) degrades the artifact.
        art = self._baseline()
        del art["blocks"]["hero.intro"]
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_manifest_matches_shipped_artifact_exactly(self):
        # The manifest IS the meaning of v1 completeness: it must name
        # exactly the shipped slug set with the shipped kinds.
        import json as _json

        import app.content_registry as reg

        raw = _json.loads(reg._BLOCKS_FILE.read_text(encoding="utf-8"))
        shipped = {slug: blk["kind"] for slug, blk in raw["blocks"].items()}
        assert reg.REQUIRED_FILE_SLUG_KINDS == shipped

    def test_verdict_row_missing_template_degrades(self, monkeypatch, tmp_path):
        # Round 18 (SOTA-A): a band-only row passed _valid_member and closure
        # yet serves a verdict with no copy at all.
        art = self._baseline()
        art["blocks"]["gauge.verdict.lead"]["items"].append(
            {"band": "hold", "trend_broken": "any"})
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_verdict_row_missing_trend_selector_degrades(self, monkeypatch, tmp_path):
        art = self._baseline()
        art["blocks"]["gauge.verdict.detail"]["items"].append(
            {"band": "hold", "template": "t"})
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_verdict_row_invalid_trend_selector_degrades(self, monkeypatch, tmp_path):
        art = self._baseline()
        art["blocks"]["gauge.verdict.lead"]["items"].append(
            {"band": "hold", "trend_broken": "sometimes", "template": "t"})
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_unhashable_trend_selector_degrades_never_500s(self, monkeypatch, tmp_path):
        # Round 19 (SOTA-A): `x in frozenset` HASHES x — a valid-JSON [] as
        # trend_broken raised TypeError out of the round-18 schema check into
        # score/dashboard/dynamic. Same crash class as round 17's band fix;
        # any exception from this call is the bug.
        art = self._baseline()
        art["blocks"]["gauge.verdict.lead"]["items"].append(
            {"band": "hold", "trend_broken": [], "template": "t"})
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_verdict_band_missing_fired_trend_degrades(self, monkeypatch, tmp_path):
        # Round 20 (SOTA-A): band presence alone let an artifact carry only
        # trend_broken="no" rows for an action band — a FIRED trend state had
        # no truthful template. Coverage requires "any", or yes AND no.
        art = self._baseline()
        for row in art["blocks"]["gauge.verdict.lead"]["items"]:
            if row["band"] == "hold":
                row["trend_broken"] = "no"
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_analytics_family_carries_as_of(self):
        # Round 24 (SOTA-A): analytics.bsadf.expl carried a measured Jul-2026
        # BSADF verdict with no as_of and no lexicon word. The ENTIRE
        # analytics namespace describes the frozen Jul-2026 battery run —
        # family rule, like ai2026: every analytics.* block is machine-dated.
        import json as _json

        import app.content_registry as reg

        raw = _json.loads(reg._BLOCKS_FILE.read_text(encoding="utf-8"))
        family = [s for s in raw["blocks"] if s.startswith("analytics.")]
        assert len(family) >= 15, "analytics family unexpectedly small"
        for slug in family:
            assert re.fullmatch(r"\d{4}-\d{2}",
                                str(raw["blocks"][slug].get("as_of") or "")), (
                f"{slug} describes the frozen battery run without as_of")

    def test_frozen_label_maps_carry_as_of(self):
        # Round 23 (SOTA-A): gauge.series.suffix ("— feared market",
        # "— challenged") is the gauge-page twin of the ai2026 suffixes —
        # frozen hedge labels with no lexicon word must still be datable.
        import json as _json

        import app.content_registry as reg

        raw = _json.loads(reg._BLOCKS_FILE.read_text(encoding="utf-8"))
        for slug in ("gauge.series.suffix",):
            assert re.fullmatch(r"\d{4}-\d{2}",
                                str(raw["blocks"][slug].get("as_of") or "")), slug

    def test_methodology_count_matches_copy(self):
        # Round 23 (SOTA-A): playbook.methods carries M0-M10 = ELEVEN rows,
        # but the intro copy said "Ten ordered screens"/"ten-screen". The
        # copy must agree with the table it summarizes. (The source site
        # carries the same defect — flagged for the wiring PRs.)
        import json as _json

        import app.content_registry as reg

        raw = _json.loads(reg._BLOCKS_FILE.read_text(encoding="utf-8"))
        rows = raw["blocks"]["playbook.methods"]["items"]
        assert len(rows) == 11
        assert [r["id"] for r in rows] == [f"M{i}" for i in range(11)]
        assert "Eleven ordered screens (M0–M10)" in raw["blocks"]["playbook.intro.summary"]["text"]
        assert "eleven-screen checklist" in raw["blocks"]["playbook.intro.expl"]["text"]
        assert "ten-screen" not in raw["blocks"]["playbook.intro.expl"]["text"]

    def test_ai2026_family_carries_as_of(self):
        # Round 20 (SOTA-A): atlas.matrix.cash.ai2026 ("The live phase-1
        # haven... ICI, 1 Jul 2026") evaded the recency lexicon. The ai2026
        # column IS the frozen present-cycle assessment — every member of the
        # family must be machine-datable, lexicon or not.
        import json as _json

        import app.content_registry as reg

        raw = _json.loads(reg._BLOCKS_FILE.read_text(encoding="utf-8"))
        family = [s for s in raw["blocks"] if "ai2026" in s]
        assert len(family) >= 18, "ai2026 family unexpectedly small"
        for slug in family:
            assert re.fullmatch(r"\d{4}-\d{2}",
                                str(raw["blocks"][slug].get("as_of") or "")), (
                f"{slug} is a frozen 2026-cycle assessment without as_of")

    def test_editorial_placeholders_dated_and_never_deictic(self):
        # Round 21 (SOTA-A): the gold-lead clock placeholder said
        # "now -> +19 mo" — a frozen Jul-2026 analytical window phrased
        # relative to REQUEST time, silently moving the implied market-peak
        # window forward after Jul 2026. Two standing rules: (1) no dynamic
        # placeholder may use deictic relative time; (2) every slot whose
        # placeholder carries a frozen editorial value must be machine-dated.
        import app.content_registry as reg

        deictic = re.compile(r"\b(now|today|tomorrow|ago|from now)\b", re.I)
        pending = (reg._PENDING, "Pending.")
        dated = 0
        for slot in reg.DYNAMIC_SLOTS:
            assert not deictic.search(slot.placeholder), (
                f"{slot.slug} placeholder is deictic: {slot.placeholder!r}")
            if slot.placeholder in pending or slot.placeholder.startswith("Automated"):
                continue
            assert re.fullmatch(r"\d{4}-\d{2}", str(slot.as_of or "")), (
                f"{slot.slug} serves a frozen editorial value without as_of")
            dated += 1
        assert dated >= 38, "editorial placeholder census unexpectedly small"

    def test_dynamic_payload_carries_as_of(self, client):
        payload = client.get("/api/v1/content/dynamic").json()["data"]["slots"]
        assert payload["analytics.clock.gold-lead.value"]["as_of"] == "2026-07"
        assert payload["headline_note"]["as_of"] is None

    def test_distance_row_missing_case_degrades(self, monkeypatch, tmp_path):
        # The distance table is case-keyed; its rows have a schema too.
        art = self._baseline()
        art["blocks"]["gauge.verdict.distance"]["items"].append({"template": "t"})
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_splash_blurb_missing_canonical_band_degrades(self, monkeypatch, tmp_path):
        # Round 17 (SOTA-C): gauge.splash.band_blurb was entirely unvalidated
        # at runtime — a swapped artifact missing its de-risk blurb passed the
        # loader and ||hold client patterns rendered HOLD for the highest-
        # severity band. Every band map must cover the canonical vocabulary.
        art = self._baseline()
        del art["blocks"]["gauge.splash.band_blurb"]["entries"]["de-risk"]
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_coherent_band_omission_degrades(self, monkeypatch, tmp_path):
        # Round 17 (SOTA-A): closure used to be relative to whatever the
        # oneliner self-declared — omitting de-risk from EVERY band block at
        # once passed. The vocabulary is canonical in code; this fixture is
        # exactly the old bypass and must degrade.
        art = self._baseline()
        for m in ("gauge.band.oneliner", "gauge.splash.band_blurb"):
            del art["blocks"][m]["entries"]["de-risk"]
        for t in ("gauge.verdict.lead", "gauge.verdict.detail"):
            art["blocks"][t]["items"] = [
                r for r in art["blocks"][t]["items"] if r["band"] != "de-risk"]
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_declared_extra_band_must_resolve_in_tables(self, monkeypatch, tmp_path):
        # The union check survives: a band a map declares BEYOND the canonical
        # set must still resolve to rows in every verdict table.
        art = self._baseline()
        for m in ("gauge.band.oneliner", "gauge.splash.band_blurb"):
            art["blocks"][m]["entries"]["futureband"] = "x"
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_unhashable_band_value_degrades_never_500s(self, monkeypatch, tmp_path):
        # Round 17 (SOTA-A): an unhashable "band" value (list/dict) raised
        # TypeError out of the closure's set build, past the load-time
        # try/except, and 500'd every artifact-backed route. It must degrade
        # the artifact whole — this call raising ANY exception is the bug.
        art = self._baseline()
        art["blocks"]["gauge.verdict.lead"]["items"].append(
            {"band": ["de-risk"], "trend_broken": "any", "template": "t"})
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_dated_editorial_carries_as_of(self):
        # Round 17 (SOTA-A): frozen editorial asserting calendar recency
        # ("right now (<= 6 weeks old)", "Today's surge") was served with no
        # freshness marker. Every calendar-anchored block must carry as_of;
        # the allowlist names blocks whose today/now is LIVE-referential —
        # copy that rides the live payload it explains — reviewed one by one.
        import json as _json

        import app.content_registry as reg

        # Round 18 (SOTA-A): gauge.reg.s5 was wrongly on this list — its
        # "Today's spreads are near 25-year tights" is a frozen market fact,
        # not live-referential copy; it is now dated + stamped. The rest were
        # re-reviewed after that miss: each block's "today/now" refers to the
        # live payload it rides (band state, coverage, fusion output).
        LIVE_REFERENTIAL = {
            "gauge.band.oneliner", "gauge.coverage.tip",
            "gauge.epistemic.not_probability", "gauge.fusion.caveat",
            "gauge.fusion.header", "gauge.ladder.ceiling",
            "gauge.verdict.lead", "gauge.verdict.detail",
        }
        # Round 23 (SOTA-A): frozen-STATE markers join the lexicon — "at a
        # record", "since Apr 2025"-style claims are calendar-anchored too.
        recency = re.compile(
            r"\b(today|today's|right now|current(ly)?|this cycle|latest"
            r"|at a record|record high|all-time|since \w+ 20\d{2})\b", re.I)
        raw = _json.loads(reg._BLOCKS_FILE.read_text(encoding="utf-8"))
        stamped = 0
        for slug, block in raw["blocks"].items():
            if slug in LIVE_REFERENTIAL:
                continue
            if recency.search(_json.dumps(block, ensure_ascii=False)):
                assert re.fullmatch(r"\d{4}-\d{2}", str(block.get("as_of") or "")), (
                    f"{slug} asserts calendar recency but carries no as_of stamp")
                stamped += 1
        assert stamped >= 25, "recency sweep found suspiciously few dated blocks"

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
        # KIND must degrade the artifact. Round 22: rebuilt on the baseline
        # (the old 5-block fixture was multiply-invalid and pinned nothing);
        # hero.intro flips text->map, a kind-only violation.
        art = self._baseline()
        art["blocks"]["hero.intro"] = {"kind": "map", "entries": {"k": "v"}}
        reg = self._install(monkeypatch, tmp_path, art)
        assert reg.content_version() == 0
        reg._clear_artifact_cache()

    def test_malformed_slug_degrades_whole_artifact(self, monkeypatch, tmp_path):
        # Round 13: slug shape is schema — a slug with spaces/notes must
        # degrade the artifact (this check caught a real defect in the
        # shipped artifact on first contact). Round 22: rebuilt on the
        # baseline as a single-violation fixture.
        art = self._baseline()
        art["blocks"]["bad slug (with note)"] = {"kind": "text", "text": "x"}
        reg = self._install(monkeypatch, tmp_path, art)
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

    def test_runtime_floor_and_closure_enforced_in_loader(self, monkeypatch, tmp_path):
        # Round 16 (SOTA-C): completeness and band closure must fire in the
        # RUNTIME loader, not only in CI pins. Round 18 superseded the count
        # floor with the full manifest — the small artifact still degrades,
        # now for the stronger reason; closure has its own single-violation
        # tests above.
        import app.content_registry as reg

        f = tmp_path / "content_blocks.v1.json"
        small = ('{"content_version": 1, "block_count": 5, "blocks": {'
                 '"gauge.band.oneliner": {"kind": "map", "entries": {"hold": "h"}},'
                 '"gauge.splash.band_blurb": {"kind": "map", "entries": {"hold": "h"}},'
                 '"gauge.verdict.lead": {"kind": "table", "items": [{"band": "hold", "template": "x"}]},'
                 '"gauge.verdict.detail": {"kind": "table", "items": [{"band": "hold", "template": "x"}]},'
                 '"gauge.verdict.distance": {"kind": "table", "items": [{"case": "c", "template": "x"}]}}}')
        f.write_text(small, encoding="utf-8")
        monkeypatch.setattr(reg, "_BLOCKS_FILE", f)
        reg._clear_artifact_cache()
        assert reg.content_version() == 0, "5-block artifact must fail completeness"
        reg._clear_artifact_cache()

    def test_degenerate_slugs_rejected_by_structured_regex(self, monkeypatch, tmp_path):
        # Round 16 (SOTA-A): the charset regex passed '..' and trailing dots —
        # the shipped artifact itself carried two such slugs (renamed).
        import app.content_registry as reg

        assert reg._SLUG_RE.fullmatch("gauge.band.oneliner")
        for bad in ("gauge.", ".gauge", "a..b", ".", "playbook.experts.r1..24"):
            assert not reg._SLUG_RE.fullmatch(bad), bad
        # Round 22 (SOTA-A): the round-16 rewrite dropped round 13's frozen
        # {1,120} cap — restored; 120 loads, 121 degrades.
        assert reg._SLUG_RE.fullmatch("a" * 120)
        assert not reg._SLUG_RE.fullmatch("a" * 121)
        assert not reg._SLUG_RE.fullmatch("x." + "a" * 119)
