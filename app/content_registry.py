"""UI content served as a resource — the registry behind /api/v1/content/*.

The frontend carries no explanations, references, or scientific content of its
own (docs/CONTENT_API.md): only buttons, titles, labels and interaction text
live in the page. Everything grounded is defined HERE (or imported from
app.references, the single source of truth for methodology text) and served by
app/routers/content.py, so a change in metric understanding is an API-side
edit that updates every frontend.

Two registries:

* STATIC blocks — explanatory text, catalogues and taxonomies that change only
  with the code. Scientific text is imported from app.references, never
  re-typed; the recompute schedule is derived from app.engine.recompute_slots.
* DYNAMIC slots — text that should track live data. Each slot declares a hard,
  regex-checkable contract (max_len + regex). This module ships placeholder
  values that satisfy their own contracts; the generation stage replaces them
  under the identical shape (source: placeholder | generated | fallback).
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.content_manifest import REQUIRED_FILE_SLUG_KINDS
from app.engine.recompute_slots import schedule_display
from app.references import DISCLAIMER

# The versioned block artifact (save-haven prose migrated per the shallow-
# frontend program). Absent file -> built-ins only; the artifact ships in the
# same PR that first references its slugs.
_BLOCKS_FILE = Path(__file__).resolve().parents[1] / "config" / "content_blocks.v1.json"


_EMPTY_ARTIFACT: dict[str, Any] = {"content_version": 0, "blocks": {}}

# Slug shape: dot-SEPARATED non-empty lowercase segments — the round-13 charset
# regex still passed degenerate slugs ("..", leading/trailing dots, bare
# "gauge." producing an empty display sub-slug); round 16 structures it. Two
# shipped slugs were caught and renamed on first contact (again).
_SLUG_RE = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)*$")

# Completeness = the FULL code-anchored manifest (app/content_manifest.py):
# every v1 slug present with its declared kind, or the artifact degrades
# whole. Round 18 (SOTA-A): the previous gate — five required slugs plus a
# 200-block count floor — accepted an artifact of band slugs + junk fillers
# with all 206 remaining editorial blocks missing. The round-16 _MIN_BLOCKS
# floor is strictly subsumed: manifest coverage implies len(blocks) >= 211.
# (REQUIRED_FILE_SLUG_KINDS is imported at the top of this module.)

# The band vocabulary is CANONICAL, not artifact-self-declared (round 17).
# app/models.py:61-65 pins the authoritative action states (hold | trim |
# de-risk | suppressed); display adds the degraded-suppression and fallback
# sentinels, and "unknown" is the explicit not-scored state (round 9: an
# unrecognized band must never fall through to HOLD action copy).
_CANONICAL_BANDS: frozenset[str] = frozenset({
    "hold", "trim", "de-risk", "suppressed",
    "suppressed (block degraded)", "fallback", "unknown",
})
_BAND_MAP_SLUGS = ("gauge.band.oneliner", "gauge.splash.band_blurb")
# gauge.verdict.distance is deliberately NOT here: its rows are case-keyed
# (case/template), not band-keyed — see the shipped artifact.
_BAND_TABLE_SLUGS = ("gauge.verdict.lead", "gauge.verdict.detail")
# Verdict-row schema (round 18, SOTA-A): a band-only row ({"band": "hold"})
# passed _valid_member (non-empty dict) and closure (band str) yet serves a
# verdict with no template and no trend selector — clients render nothing.
_TREND_SELECTORS: frozenset[str] = frozenset({"yes", "no", "any"})
_DISTANCE_TABLE_SLUG = "gauge.verdict.distance"

_cache_lock = threading.Lock()
# Published as ONE tuple so a reader can never see a key from one load paired
# with a value from another (round 12: non-atomic publication under threads).
_cache: tuple[tuple[int, int, int] | None, dict[str, Any]] | None = None


def _file_artifact() -> dict[str, Any]:
    # Cache keyed on (mtime_ns, size, inode) of the OPEN file descriptor:
    # key and content are provably bound to the same file state — a separate
    # stat()-then-open() lets a rename land between the two, caching one
    # file's content under another's key (TOCTOU; round 13). Atomic-rename
    # deployment always changes the inode, so a replaced artifact is
    # re-examined even when a copy tool preserves mtime and size; an in-place
    # rewrite preserving all three is a filesystem-level act outside this
    # control's threat model.
    global _cache
    try:
        fh = _BLOCKS_FILE.open(encoding="utf-8")
    except OSError:
        with _cache_lock:
            _cache = (None, dict(_EMPTY_ARTIFACT))
            return _cache[1]
    with fh:
        st = os.fstat(fh.fileno())
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        cached = _cache
        if cached is not None and cached[0] == key:
            return cached[1]
        with _cache_lock:
            cached = _cache
            if cached is not None and cached[0] == key:
                return cached[1]
            value = _load_artifact(fh)
            _cache = (key, value)
            return value


def _clear_artifact_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def artifact_view() -> tuple[dict[str, Any], int]:
    """One coherent snapshot of (blocks, version) from a SINGLE artifact read —
    a response must never mix one load's blocks with another's version."""
    artifact = _file_artifact()
    version = artifact.get("content_version", 0)
    blocks = artifact.get("blocks", {})
    return (blocks if isinstance(blocks, dict) else {},
            version if type(version) is int else 0)


def _load_artifact(fh: Any) -> dict[str, Any]:
    # NEVER couple request handling to artifact health: a missing, unreadable
    # or malformed artifact degrades WHOLLY to built-ins at version 0 (the
    # never-500-on-data-failure invariant). Validation is deep, not root-only:
    # an artifact with a structurally invalid blocks/version must not keep
    # advertising its version while serving built-ins; and a hostile
    # deeply-nested file raises RecursionError, which must not escape into a
    # request handler (panel findings, PR #97 rounds 2-3).
    def _reject_constant(name: str) -> Any:
        # Python's json accepts Infinity/NaN by default; Starlette's response
        # encoder (allow_nan=False) then 500s — reject at load instead.
        raise ValueError(f"non-finite JSON constant: {name}")

    try:
        loaded = json.load(fh, parse_constant=_reject_constant)
        # A payload that json.load accepts can still be unserializable at
        # RESPONSE time (lone UTF-16 surrogates raise UnicodeEncodeError; a
        # 1e400 exponent overflows to float('inf') via parse_float, bypassing
        # parse_constant, and the allow_nan=False response encoder raises).
        # Prove the whole artifact round-trips under the RESPONSE encoder's
        # own strictness here, or reject it whole.
        json.dumps(loaded, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (OSError, ValueError, RecursionError, UnicodeEncodeError):
        return dict(_EMPTY_ARTIFACT)
    version = loaded.get("content_version") if isinstance(loaded, dict) else None
    blocks = loaded.get("blocks") if isinstance(loaded, dict) else None
    declared = loaded.get("block_count") if isinstance(loaded, dict) else None
    # Completeness self-attestation: the artifact declares its own block
    # count; a truncated-but-valid subset must not serve under the artifact's
    # version (panel finding, round 10).
    if type(declared) is not int or not isinstance(blocks, dict) or declared != len(blocks):
        return dict(_EMPTY_ARTIFACT)
    # bool is an int subclass in Python: `true` must not pass as a version.
    if (type(version) is not int or version < 1 or not isinstance(blocks, dict)
            or not all(isinstance(slug, str) and _SLUG_RE.fullmatch(slug)
                       for slug in blocks)
            or not all(isinstance(blocks.get(slug), dict)
                       and blocks[slug].get("kind") == kind
                       for slug, kind in REQUIRED_FILE_SLUG_KINDS.items())
            or _max_depth(blocks) > 32
            or not all(_valid_member(b) for b in blocks.values())
            or not _bands_closed(blocks)):
        # All-or-nothing: an artifact with ANY corrupt member degrades whole —
        # partial content must never be served under the artifact's version.
        return dict(_EMPTY_ARTIFACT)
    return {"content_version": version, "blocks": blocks}


def _bands_closed(blocks: dict[str, Any]) -> bool:
    """Band-vocabulary closure at RUNTIME against a CANONICAL vocabulary.

    Round 17 (SOTA-A + SOTA-C): closure relative to whatever the oneliner
    happened to declare let a v1 artifact omit the de-risk copy from every
    band block at once, and left gauge.splash.band_blurb entirely
    unvalidated — a swapped artifact missing its fallback/de-risk blurbs
    passed the loader, and ||hold client patterns rendered HOLD copy for
    the highest-severity band. Every band-keyed map must cover ALL of
    _CANONICAL_BANDS, and every band-keyed verdict table must cover the
    canonical set PLUS any extra band a map declares, or the artifact
    degrades whole."""
    vocab: set[str] = set(_CANONICAL_BANDS)
    for map_slug in _BAND_MAP_SLUGS:
        block = blocks.get(map_slug, {})
        entries = block.get("entries") if isinstance(block, dict) else None
        if not isinstance(entries, dict) or not _CANONICAL_BANDS.issubset(entries):
            return False
        vocab.update(entries)
    for table_slug in _BAND_TABLE_SLUGS:
        table = blocks.get(table_slug, {})
        items = table.get("items") if isinstance(table, dict) else None
        if not isinstance(items, list):
            return False
        # str-check BEFORE any hashing: an unhashable "band" value (list/
        # dict) raised TypeError out of a set comprehension here, past the
        # load-time try/except, and 500'd every artifact-backed route
        # (round 17, SOTA-A). A band-table row whose band is not a string
        # is corruption — the artifact degrades whole, it never crashes.
        rows: set[str] = set()
        for row in items:
            band = row.get("band") if isinstance(row, dict) else None
            if not isinstance(band, str):
                return False
            # Full row schema, not band-presence alone (round 18): every
            # verdict row needs its template copy and a trend selector.
            template = row.get("template")
            if not isinstance(template, str) or not template.strip():
                return False
            # isinstance BEFORE membership: `x in frozenset` hashes x, so a
            # valid-JSON [] here raised TypeError into three routes — the
            # same crash class as round 17's band fix, reintroduced by the
            # round-18 schema check and caught by SOTA-A (round 19).
            trend = row.get("trend_broken")
            if not isinstance(trend, str) or trend not in _TREND_SELECTORS:
                return False
            rows.add(band)
        if not vocab.issubset(rows):
            return False
    # The distance table is case-keyed, not band-keyed — but its rows have a
    # schema too: a non-empty case selector and template copy (round 18).
    distance = blocks.get(_DISTANCE_TABLE_SLUG, {})
    d_items = distance.get("items") if isinstance(distance, dict) else None
    if not isinstance(d_items, list):
        return False
    for row in d_items:
        if not isinstance(row, dict):
            return False
        case = row.get("case")
        template = row.get("template")
        if not (isinstance(case, str) and case.strip()
                and isinstance(template, str) and template.strip()):
            return False
    return True


def _max_depth(obj: Any) -> int:
    """Iterative nesting depth (no recursion): the preflight round-trip dumps
    the RAW artifact, but responses serialize it WRAPPED in {data:{...},meta}
    — an artifact near the interpreter recursion limit passes a raw dump yet
    raises inside the response encoder. An explicit bound (32) decouples
    artifact health from interpreter limits entirely."""
    depth, stack = 0, [(obj, 1)]
    while stack:
        node, d = stack.pop()
        depth = max(depth, d)
        if d > 64:  # hard stop: deeper than any legitimate artifact
            return d
        if isinstance(node, dict):
            stack.extend((v, d + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, d + 1) for v in node)
    return depth


def _valid_member(block: Any) -> bool:
    # Deep, not container-shallow: the all-or-nothing invariant means every
    # ITEM must satisfy its kind's shape, not just the container's presence.
    if not isinstance(block, dict):
        return False
    kind = block.get("kind")
    if kind in ("text", "template"):
        text = block.get("text")
        return isinstance(text, str) and bool(text.strip())
    if kind in ("links", "endpoint_catalog", "table"):
        items = block.get("items")
        return (isinstance(items, list) and bool(items)
                and all(isinstance(i, dict) and i for i in items))
    if kind == "list":
        items = block.get("items")
        return (isinstance(items, list) and bool(items)
                and all((isinstance(i, str) and i.strip())
                        or (isinstance(i, dict) and i) for i in items))
    if kind == "map":
        entries = block.get("entries")
        return (isinstance(entries, dict) and bool(entries)
                and all(isinstance(v, str) and v.strip() for v in entries.values()))
    return False


def content_version() -> int:
    version = _file_artifact().get("content_version", 0)
    return version if isinstance(version, int) else 0

ADVICE_TAG = "Research, not advice."


def _text(text: str) -> dict[str, Any]:
    return {"kind": "text", "text": text}


def dashboard_payload() -> dict[str, Any]:
    """Blocks + version from ONE artifact snapshot — a response must never
    pair one load's blocks with another load's version (round 12)."""
    file_blocks, version = artifact_view()
    blocks = dict(_builtin_blocks())
    for slug, block in file_blocks.items():
        if slug not in blocks and isinstance(block, dict):
            blocks[slug] = block
    return {"blocks": blocks, "content_version": version}


def static_blocks() -> dict[str, dict[str, Any]]:
    """All static content blocks, keyed by slug: built-ins + the versioned
    block artifact (file slugs never override built-ins)."""
    blocks: dict[str, dict[str, Any]] = dashboard_payload()["blocks"]
    return blocks


def gauge_display() -> dict[str, Any]:
    """The gauge display deck (Q8: copy rides the score payload) — every
    file block under the gauge. prefix (the A1 ledger's namespace for the
    deck), keyed by its sub-slug (e.g. band.oneliner, badge.static)."""
    prefix = "gauge."
    return {slug[len(prefix):]: block
            for slug, block in static_blocks().items() if slug.startswith(prefix)}


def _builtin_blocks() -> dict[str, dict[str, Any]]:
    return {
        # The canonical disclaimer (app.references.DISCLAIMER) with markdown
        # emphasis markers stripped for plain-text display surfaces.
        "disclaimer_full": _text(DISCLAIMER.replace("**", "")),
        # Canonical alias for the companion dashboard's frozen content
        # contract: its client, fallback generator and acceptance suite all
        # gate on blocks['site.disclaimer'] carrying 'not investment advice'.
        "site.disclaimer": _text(DISCLAIMER.replace("**", "")),
        "advice_tag": _text(ADVICE_TAG),
        "intro.science_audit": _text(
            "Everything currently unclear, incomplete, contested, proxied, judgmental, "
            "or deviating from the written spec — flagged and made visible. Scientific "
            "correctness is the leading design aspect."
        ),
        "intro.sources": _text(
            "Live success/failure of each external fetch from the most recent recompute, "
            "with the scientific/official basis of each source."
        ),
        "intro.feed_sources": _text(
            "Per-item health of the companion-dashboard feed (GET /api/v1/dashboard/feed) — "
            "incl. the CNN Fear & Greed pull. These sources feed display only; each degrades "
            "independently and never touches the score."
        ),
        "intro.legs": _text(
            "The trend trigger, fast alarm, and action-band thresholds that gate the "
            "headline, with their literature basis and caveats."
        ),
        "intro.api": _text("Interactive, machine-readable spec with schemas and try-it-out:"),
        "api.doc_links": {
            "kind": "links",
            "items": [
                {"label": "Swagger UI →", "href": "/docs"},
                {"label": "ReDoc →", "href": "/redoc"},
                {"label": "openapi.json →", "href": "/openapi.json"},
                {"label": "this status as JSON →", "href": "/api/v1/status"},
                {"label": "methodology JSON →", "href": "/api/v1/meta/methodology"},
            ],
        },
        "api.endpoints": {
            "kind": "endpoint_catalog",
            "items": [
                {"method": "GET", "path": "/api/v1/score",
                 "purpose": "Headline median, IQR, band, both blocks, red flags, legs, judgment"},
                {"method": "GET", "path": "/api/v1/score/history",
                 "purpose": "Historical snapshots (raw/daily/monthly)"},
                {"method": "GET", "path": "/api/v1/indicators/{id}",
                 "purpose": "Full WHAT/HOW/WHY methodology + references"},
                {"method": "GET", "path": "/api/v1/legs/trend",
                 "purpose": "Faber trend states (SPY, QQQ)"},
                {"method": "GET", "path": "/api/v1/legs/fast-alarm",
                 "purpose": "VIX term structure, VRP, SKEW"},
                {"method": "GET", "path": "/api/v1/meta/methodology",
                 "purpose": "Framework, references, falsification, changelog"},
                {"method": "GET", "path": "/api/v1/dashboard/feed",
                 "purpose": "Dashboard feed: monthly series + scalar metrics "
                            "(DASHBOARD_FEED_SPEC.md)"},
                {"method": "GET", "path": "/api/v1/content/dashboard",
                 "purpose": "UI content blocks (this page's text, served as a resource)"},
                {"method": "GET", "path": "/api/v1/content/dynamic",
                 "purpose": "Dynamic content slots with hard length/regex contracts"},
                {"method": "GET", "path": "/api/v1/status",
                 "purpose": "This status document"},
                {"method": "GET", "path": "/readyz",
                 "purpose": "Per-source health matrix + GSADF R self-check"},
                {"method": "POST", "path": "/api/v1/admin/refresh",
                 "purpose": "Trigger a recompute (X-API-Key)"},
            ],
        },
        # Empty states that state operational facts belong to the API; the
        # schedule fragment is derived from the canonical slot table.
        "empty.snapshot": _text(
            "No snapshot computed yet — trigger a recompute or wait for the "
            f"{schedule_display()} schedule."
        ),
        "empty.feed": _text("No feed payload persisted yet — builds with the next recompute."),
        "empty.providers": _text(
            "No price-provider health recorded yet (first recompute pending)."
        ),
        # Grounding taxonomy → display severity, previously duplicated in the page.
        "taxonomy.grounding": {
            "kind": "map",
            "entries": {
                "literature-grounded": "ok",
                "literature-adjacent": "info",
                "judgmental": "warn",
                "contested": "error",
                "lagging-confirmation": "info",
            },
        },
    }


@dataclass(frozen=True)
class DynamicSlot:
    """One dynamic-content slot and its hard, regex-checkable contract."""

    slug: str
    purpose: str
    max_len: int
    regex: str
    placeholder: str


_PENDING = "Automated note pending - not yet generated."


def _ascii_slot(slug: str, purpose: str, max_len: int) -> DynamicSlot:
    """Single-line printable-ASCII slot; length bound repeated in the regex so
    a consumer can validate with the regex alone."""
    placeholder = _PENDING if len(_PENDING) <= max_len else "Pending."
    return DynamicSlot(slug, purpose, max_len, rf"^[\x20-\x7E]{{1,{max_len}}}$", placeholder)


def _save_haven_slots() -> list[DynamicSlot]:
    """The companion-dashboard slot registry (A1 ledger contracts, ruling Q47).
    Analytics slots serve today's frozen editorial values as placeholders until
    the Phase-E battery produces them server-side."""
    slots: list[DynamicSlot] = [
        # Banner placeholders carry the CURRENT editorial values (A2: analytics/
        # banner slots serve today's frozen editorial state until generation
        # lands) — a placeholder shaped like a date must never fabricate one.
        DynamicSlot("atlas.explorer.potential-banner.anchor-date",
                    "Anchor month of the potential-crisis banner", 8,
                    r"^[A-Z][a-z]{2} 20\d{2}$", "Jul 2026"),
        DynamicSlot("atlas.explorer.potential-banner.backfill-window",
                    "Backfill window of the potential-crisis banner", 24,
                    r"^[\x20-\x7E]{1,24}$", "Jul 2021 - Jul 2026"),
        # The placeholder states DATES and asserts no recency claim — a
        # "<= N weeks old" phrasing self-invalidates as time passes (panel
        # finding, round 10); the dynamic slot exists to keep this current.
        DynamicSlot("atlas.explorer.potential-banner.freshness",
                    "Source-freshness line of the potential-crisis banner", 120,
                    r"^[\x20-\x7E]{1,120}$",
                    "sources as of BIS 28 Jun / ECB 2 Jun & 27 May / Fed 8 May 2026"),
        _ascii_slot("atlas.crises.ai2026.peak", "AI-2026 crisis peak label", 48),
        _ascii_slot("atlas.crises.ai2026.cause", "AI-2026 crisis cause prose", 600),
        _ascii_slot("atlas.crises.ai2026.highlight", "AI-2026 crisis highlight prose", 700),
        _ascii_slot("gauge.verdict.lead", "Gauge hero verdict lead sentence", 120),
        _ascii_slot("gauge.verdict.detail", "Gauge hero verdict detail sentence", 360),
        _ascii_slot("gauge.verdict.distance", "Gauge verdict distance clause", 200),
        _ascii_slot("gauge.judgment_call", "Gauge judgment-call line", 300),
        _ascii_slot("gauge.freshness", "Gauge freshness stamp", 24),
        _ascii_slot("gauge.badge.static", "Gauge static-mode badge text", 120),
        _ascii_slot("gauge.badge.live_tip", "Gauge live-mode badge tooltip", 120),
        _ascii_slot("gauge.badge.partial_tip", "Gauge partial-mode badge tooltip", 140),
        _ascii_slot("gauge.live_backfill.banner", "Gauge live-backfill banner", 160),
        _ascii_slot("gauge.live_backfill.static_note", "Gauge live-backfill static note", 160),
        _ascii_slot("gauge.live_backfill.editorial_line", "Gauge live-backfill editorial line", 120),
        _ascii_slot("gauge.hero.run_line", "Gauge hero run line", 60),
        _ascii_slot("gauge.metric.note", "Gauge metric provenance note", 120),
        _ascii_slot("gauge.status.audit_flag.title", "Gauge audit-flag title", 80),
        _ascii_slot("gauge.status.audit_flag.detail", "Gauge audit-flag detail", 280),
        _ascii_slot("gauge.status.audit_flag.ref", "Gauge audit-flag reference", 40),
        _ascii_slot("analytics.markov.p_turbulent", "Markov turbulent-state probability line", 32),
        _ascii_slot("analytics.markov.states", "Markov state descriptions", 240),
        _ascii_slot("analytics.granger.summary", "Granger lead-lag summary", 400),
        _ascii_slot("analytics.hedgeweight.verdict", "Hedge-weighting verdict paragraph", 520),
        _ascii_slot("playbook.etoro.verified", "eToro instrument verification date line", 48),
        _ascii_slot("playbook.expert.as_of", "Expert buy-list as-of stamp", 24),
    ]
    for crisis_asset, n in (("gold", 520), ("bonds", 450), ("cash", 320),
                            ("jpy", 160), ("btc", 200)):
        slots.append(_ascii_slot(f"atlas.matrix.{crisis_asset}.ai2026",
                                 f"AI-2026 matrix note for {crisis_asset}", n))
    # Numeric analytics placeholders carry the TRUE frozen editorial values
    # from the dashboard's battery (A2; ASCII-normalized) — a zero-shaped
    # placeholder would fabricate a measurement (panel finding, PR #97).
    tail_editorial = {
        "gold": {"bf": "-0.08", "bc": "-0.02", "hit": "42%", "lam": "0.26"},
        "ust10y": {"bf": "+0.08", "bc": "-0.02", "hit": "33%", "lam": "0.31"},
        "cash": {"bf": "+0.01", "bc": "+0.01", "hit": "100%", "lam": "0.37"},
    }
    # Per-stat domain regexes (round 11): hit is a 0-100 percentage, bf/bc are
    # signed betas, lam lives in [0,1] — a generic numeric regex admitted
    # domain-impossible values like 999%.
    tail_regex = {
        "bf": r"^[+-]\d\.\d{2}$",
        "bc": r"^[+-]\d\.\d{2}$",
        "hit": r"^(100|[1-9]?\d(\.\d{1,2})?)%$",
        "lam": r"^(0\.\d{2}|1\.00)$",
    }
    for asset, stats in tail_editorial.items():
        for stat, value in stats.items():
            slots.append(DynamicSlot(
                f"analytics.tail.{asset}.{stat}",
                f"Tail-regression {stat} statistic for {asset}", 8,
                tail_regex[stat], value))
    explos_editorial = [
        ("Raw log price", "+0.75", "borderline (crit 0.62-0.78, seed-sensitive)"),
        ("Linear-trend residual", "-0.76", "not explosive"),
        ("Broken-trend residual (break Dec '22)", "-1.20", "not explosive"),
        ("Earnings-proxy residual (17%/yr)", "-0.83", "not explosive"),
    ]
    for i, (label, stat, verdict) in enumerate(explos_editorial, start=1):
        slots.append(DynamicSlot(f"analytics.explos.{i}.label",
                                 f"Explosiveness row {i} label", 48,
                                 r"^[\x20-\x7E]{1,48}$", label))
        slots.append(DynamicSlot(f"analytics.explos.{i}.stat",
                                 f"Explosiveness row {i} statistic", 6,
                                 r"^[+-]\d\.\d{2}$", stat))
        slots.append(DynamicSlot(f"analytics.explos.{i}.verdict",
                                 f"Explosiveness row {i} verdict", 44,
                                 r"^[\x20-\x7E]{1,44}$", verdict))
    clock_editorial = {
        "weighted": "~ -12 mo", "dotcom": "p ~ 0 / +1",
        "lppl": "+2.5 mo", "gold-lead": "now -> +19 mo",
    }
    for clock, value in clock_editorial.items():
        slots.append(DynamicSlot(f"analytics.clock.{clock}.value",
                                 f"Analytics clock {clock} value", 16,
                                 r"^[\x20-\x7E]{1,16}$", value))
        slots.append(_ascii_slot(f"analytics.clock.{clock}.caption",
                                 f"Analytics clock {clock} caption", 180))
    hedge_editorial = {
        "cash": "0.88", "gold": "0.85", "chf": "0.59", "usd": "0.44",
        "ust10y": "0.42", "jpy": "0.21", "btc": "0.11",
    }
    for asset, score in hedge_editorial.items():
        # Domain-tight: a hedge score lives in [0, 1] — the regex must not
        # admit 1.50 (panel finding, PR #97).
        slots.append(DynamicSlot(f"analytics.hedgeweight.{asset}.score",
                                 f"Hedge weighting score for {asset}", 4,
                                 r"^(0\.\d{2}|1\.00)$", score))
        slots.append(_ascii_slot(f"analytics.hedgeweight.{asset}.reason",
                                 f"Hedge weighting reason for {asset}", 140))
    return slots


DYNAMIC_SLOTS: tuple[DynamicSlot, ...] = (
    DynamicSlot(
        slug="headline_note",
        purpose=("One-line interpretation of the current headline score and action band "
                 "for the status page"),
        max_len=200,
        regex=r"^[\x20-\x7E]{1,200}$",
        placeholder="Automated regime note pending - not yet generated.",
    ),
    DynamicSlot(
        slug="audit_note",
        purpose="One-line summary of the current science-audit state for the status page",
        max_len=160,
        regex=r"^[\x20-\x7E]{1,160}$",
        placeholder="Automated audit note pending - not yet generated.",
    ),
    DynamicSlot(
        slug="dashboard_regime_note",
        purpose=("Context line for the companion dashboard relating current regime metrics "
                 "to the headline"),
        max_len=240,
        regex=r"^[\x20-\x7E]{1,240}$",
        placeholder="Automated dashboard note pending - not yet generated.",
    ),
    *_save_haven_slots(),
)


def dynamic_slots_payload() -> dict[str, dict[str, Any]]:
    """Serve every dynamic slot. Placeholder-backed until generation lands."""
    return {
        s.slug: {
            "text": s.placeholder,
            "source": "placeholder",
            "updated_at": None,
            "purpose": s.purpose,
            "constraints": {"max_len": s.max_len, "regex": s.regex},
        }
        for s in DYNAMIC_SLOTS
    }
