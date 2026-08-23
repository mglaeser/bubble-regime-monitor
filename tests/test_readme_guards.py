"""README drift guards (v3.7.1).

Root cause of the README rot found in the 2026-07 audits: only documentation
pinned by a test stayed current (references.py's CHANGELOG was perfect, the
README's version list was six releases behind). These tests give the README
the same treatment — a version bump, a golden-fixture change, or a new API
router now FAILS the suite until the README is updated too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def test_readme_mentions_latest_release():
    """The newest CHANGELOG entry's version must appear in the README, and the
    CHANGELOG head must itself match service_version — so bumping the version
    without touching BOTH the changelog and the README breaks the build."""
    from app.config import get_settings
    from app.references import CHANGELOG

    latest = CHANGELOG[-1]["version"]                 # e.g. "v3.7.1"
    assert latest == f"v{get_settings().service_version}"
    assert latest in README, f"README does not mention the latest release {latest}"


def test_readme_golden_number_matches_engine():
    """The worked example's headline in the README must equal what the engine
    actually computes on the golden fixture (the pre-v3.3.0 value 40.35 sat in
    the README for four releases because nothing checked it)."""
    from app.engine.aggregate import RedFlags, deterministic_score
    from app.engine.montecarlo import BASE_WEIGHTS_D, BASE_WEIGHTS_S
    from tests.conftest import GOLDEN_SUB_D, GOLDEN_SUB_S

    res = deterministic_score(dict(GOLDEN_SUB_S), dict(GOLDEN_SUB_D), 1.0, RedFlags(),
                              BASE_WEIGHTS_S, BASE_WEIGHTS_D)
    assert f"{res.score:.2f}" in README, (
        f"README worked example is stale: engine computes {res.score:.2f} "
        "on the golden fixture but the README does not contain that number")


def test_readme_has_no_stale_alpha_claim():
    """The v3.3.0-resolved deviation must never be re-documented as current."""
    flat = README.replace(" ", "")
    assert "ALPHA_RANGE=(0.25,0.75)" not in flat
    assert "usesU(0.25,0.75)" not in flat.replace("`", "")


def test_readme_documents_every_api_router():
    """Every mounted /api/v1/<section> router must appear in the README — a new
    endpoint section without README coverage fails here (the dashboard-feed and
    webhook routers were missing for three releases)."""
    from app.main import create_app

    # routers are included lazily (_IncludedRouter has no .path), so enumerate
    # via the OpenAPI schema — the authoritative list of served paths anyway
    paths = create_app().openapi()["paths"]
    sections = {"/api/v1/" + p.split("/")[3] for p in paths if p.startswith("/api/v1/")}
    assert sections, "no /api/v1 routes found — app factory changed?"
    missing = sorted(s for s in sections if s not in README)
    assert not missing, f"README is missing API sections: {missing}"


def test_readme_golden_table_matches_the_fixture():
    """The golden table's Sub-score column must equal the fixture's sub-scores.

    Only the string "52.43" was guarded, so the table could drift from the
    arithmetic printed a few lines below it — and did: a methodology change
    edited the S4 row to the LIVE value, which the golden fixture (no GSADF
    input at all) cannot produce under any rule."""
    import re
    from pathlib import Path

    from tests.conftest import GOLDEN_SUB_D, GOLDEN_SUB_S

    expected = {**GOLDEN_SUB_S, **GOLDEN_SUB_D}
    text = Path("README.md").read_text(encoding="utf-8")
    body = text[text.index("## Golden fixture"):]
    body = body[:body.index("\n## ", 1)] if "\n## " in body[1:] else body

    found = {}
    for line in body.splitlines():
        m = re.match(r"\|\s*([SD]\d)\s*\|[^|]*\|[^|]*\|\s*\*\*([0-9.]+)\*\*\s*\|", line)
        if m:
            found[m.group(1).lower()] = float(m.group(2))

    assert found, "no golden-table rows parsed — the table shape changed"
    assert found == pytest.approx(expected), (
        f"README golden table disagrees with the fixture: {found} != {expected}")
