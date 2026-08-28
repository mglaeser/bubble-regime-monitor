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

from dataclasses import dataclass
from typing import Any

from app.engine.recompute_slots import schedule_display
from app.references import DISCLAIMER

ADVICE_TAG = "Research, not advice."


def _text(text: str) -> dict[str, Any]:
    return {"kind": "text", "text": text}


def static_blocks() -> dict[str, dict[str, Any]]:
    """All static content blocks, keyed by slug. Pure; no I/O."""
    return {
        # The canonical disclaimer (app.references.DISCLAIMER) with markdown
        # emphasis markers stripped for plain-text display surfaces.
        "disclaimer_full": _text(DISCLAIMER.replace("**", "")),
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


# Single-line printable ASCII; the length bound is repeated in the regex so a
# consumer can validate with the regex alone.
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
