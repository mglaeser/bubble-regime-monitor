"""The status page is SHALLOW: no grounded/scientific text lives in the HTML.

Guard in the spirit of test_readme_guards: reads app/routers/status.html from
disk and fails if scientific literals return to the page. All grounded content
must come from /api/v1/content/* or /api/v1/status (docs/CONTENT_API.md);
only buttons, titles, labels, interaction text and neutral fallbacks may be
baked. Complements the XSS/self-containment pins in tests/test_status.py.
"""

from __future__ import annotations

from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "routers" / "status.html").read_text()

# Literals that lived in the page before the shallow refactor. Each one now has
# an API home; its return to the HTML is a regression against point-0.
BANNED_LITERALS = [
    "research instrument",          # the disclaimer body -> content: disclaimer_full
    "1929",                         # reference-class fact -> disclaimer_full
    "not investment advice",        # -> disclaimer_full
    "Research, not advice",         # footer tag -> content: advice_tag
    "02/06/10/14/18/22",            # cron fact -> derived server-side (schedule_display)
    "CNN",                          # source claim -> content: intro.feed_sources
    "Fear &amp; Greed",             # -> intro.feed_sources
    "const ENDPOINTS",              # endpoint catalogue -> content: api.endpoints
    "Faber trend states",           # endpoint purpose text -> api.endpoints
    "GSADF",                        # endpoint purpose text -> api.endpoints
    "literature-grounded",          # grounding taxonomy -> content: taxonomy.grounding
    "judgmental",                   # -> taxonomy.grounding
    "Scientific correctness",       # audit intro -> content: intro.science_audit
    "scientific/official basis",    # sources intro -> content: intro.sources
    "Swagger UI",                   # doc links -> content: api.doc_links
    "builds with the next recompute",       # empty-state fact -> empty.feed
    "first recompute pending",              # empty-state fact -> empty.providers
    "trigger a recompute or wait",          # empty-state fact -> empty.snapshot
]

# Hydration wiring that must exist for the shallow page to fill itself.
REQUIRED_MARKERS = [
    "/api/v1/content/dashboard",
    "/api/v1/content/dynamic",
    'id="disclaimer"',
    'id="intro-audit"',
    'id="intro-sources"',
    'id="intro-feed"',
    'id="intro-legs"',
    'id="intro-api"',
    'id="doclinks"',
    'id="endpoints"',
    "textContent",
    # The disclaimer gate: grounded values may only render once the disclaimer
    # is on screen; the gated failure branch and the fail-closed fetch are
    # load-bearing (cross-vendor panel finding, PR #95).
    "data display disabled",
    "if(!r.ok) throw",
    "sameOrigin",
    # Reentrancy: content only ever advances on a successful fetch, so a failed
    # 60s refresh can never blank the disclaimer over stale grounded values.
    "last-known-good",
    # Staleness honesty: after a failed refresh the retained sections are
    # explicitly labeled stale, never presented as current.
    "REFRESH FAILED - showing data as of",
    # Commit ordering: overlapping loads may not render out of order.
    "seq!==LOAD_SEQ",
    # Retained (last-known-good) content is labeled stale when its refresh
    # failed — it may not silently pose as current beside newer status data.
    # Blocks and slots are labeled SEPARATELY; a first-load failure is absence,
    # never mislabeled as "from an earlier load".
    "CONTENT REFRESH FAILED - explanatory blocks shown from an earlier load.",
    "NOTE REFRESH FAILED - automated notes shown from an earlier load.",
    # State commits (not just renders) are seq-guarded: an older response can
    # never win in the CONTENT/SLOTS globals.
    "a newer load owns state from here on",
    # Round 8: 2xx shape-misses are failures; the gate purges rendered
    # sections when the disclaimer is unavailable; loads are single-flight
    # with bounded fetches so slow replies still commit.
    "Response-shape validator",
    "purgeGrounded",
    "if(LOADING) return",
]


class TestShallowPage:
    def test_no_banned_scientific_literals(self):
        for literal in BANNED_LITERALS:
            assert literal not in PAGE, (
                f"grounded literal {literal!r} is back in status.html - it must be "
                "served by the content API instead (docs/CONTENT_API.md)"
            )

    def test_hydration_wiring_present(self):
        for marker in REQUIRED_MARKERS:
            assert marker in PAGE, f"missing hydration marker {marker!r}"

    def test_headings_stay(self):
        # Titles/labels are allowed and pinned so the page keeps its structure.
        for heading in ("Headline", "Science audit", "Data source pulls",
                        "Price providers", "API reference", "Changelog"):
            assert heading in PAGE

    def test_failure_path_is_generic(self):
        # The fetch-failure branch may never state facts; it must keep the
        # generic prefix exactly (tests document the contract).
        assert "Failed to load status: " in PAGE


class TestTheDynamicContentMarker:
    """Every dynamic text on the page ends in DOT ABOVE (U+02D9) at the
    text's own size, green within the text's shade when a model wrote the
    text, the text's own colour when it came from a template (owner,
    2026-09-21). The page draws; the API decides."""

    def test_the_glyph_and_its_two_tints(self):
        assert "const DYN_GLYPH = '\\u02D9';" in PAGE
        assert ".dyn{font-size:inherit" in PAGE                    # the text's own size
        assert ".dyn.template{color:currentColor}" in PAGE         # the text's own colour
        assert ".dyn.generated{color:#5fa77a;color:color-mix(in srgb, currentColor 55%, var(--ok) 45%)}" in PAGE

    def test_the_judgment_and_every_rendered_slot_carry_it(self):
        assert "el.appendChild(dyn(j, snap.judgment_provenance||'generated'))" in PAGE
        for slot in ("headline_note", "audit_note"):
            assert f"dyn(h('div','slot', note), slotProvenance('{slot}'))" in PAGE, slot
        # no slot is rendered without the marker
        assert "appendChild(h('div','slot', note))" not in PAGE

    def test_the_tint_follows_the_apis_provenance_only(self):
        assert "s.provenance==='generated'" in PAGE
        assert "'generated':'template'" in PAGE

