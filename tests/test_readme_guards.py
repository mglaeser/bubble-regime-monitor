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


# --- the sources table, the message limits and the admin rows ----------------
# Each test holds a README cell to the code or the setting it describes. The
# production figures in the docstrings are from the 615 snapshots stored
# between 2026-07-11 and 2026-11-10.

#: The row of the sources table that carries each scored indicator's own
#: reading. A change that renames a row renames it here. V is not in this map:
#: it is the multiplier, the pipeline builds no IndicatorOutput for it, and
#: IndicatorOutput.stale is the only reader of the limits, so the artifact's
#: entry for V gates nothing.
_SLA_ROW = {"s1": "CAPE (S1)", "s2": "Concentration (S2)", "s3": "Semis run-up (S3)",
            "s4": "GSADF (S4)", "s5": "Credit tightness (S5)", "d1": "Breadth (D1)",
            "d2": "Margin (D2)", "d3": "Hyperscaler FCF (D3)", "d4": "LPPLS (D4)"}


def _sources_table() -> dict[str, list[str]]:
    """The rows of 'Data sources & freshness SLAs', keyed by their first cell."""
    body = README[README.index("## Data sources & freshness SLAs"):README.index("\n## API")]
    rows = {}
    for line in body.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0] != "Indicator" and not set(cells[0]) <= set("-: "):
            rows[cells[0]] = cells[1:]
    return rows


def test_readme_sla_column_is_the_frozen_artifact():
    """The SLA cell of each scored indicator's row is the limit the engine
    applies to that reading: the artifact's freshness_sla_days, and
    app/services/compute.py reads nothing else. The table said 45d for D2 and
    gave S5 no row of its own, only HY OAS with 3d. In production D2 was 46
    days old on 4 snapshots of 2026-10-16 and not stale (the limit is 75); S5
    was 5 to 45 days old and not stale on 405 snapshots since 2026-07-29, and
    stale on each of the 189 where it was older than 45."""
    from app.services.compute import FRESHNESS_SLA_DAYS, compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    scored = compute_snapshot(make_golden_raw_inputs(), mc_samples=1_000, mc_seed=20260711)
    assert set(_SLA_ROW) == set(scored.indicators) == set(FRESHNESS_SLA_DAYS) - {"v"}
    rows = _sources_table()
    found = {key: rows[label][-1] if label in rows else "no such row"
             for key, label in _SLA_ROW.items()}
    assert found == {key: f"{FRESHNESS_SLA_DAYS[key]}d" for key in _SLA_ROW}


def test_readme_s2_row_lists_the_one_fallback_the_code_asks(monkeypatch):
    """Concentration has two sources, the SSGA sheet and then Slickcharts. The
    JPMAM guide is compared by hand (app/sources/ssga.py); the row listed it as
    a tier after Slickcharts."""
    from app.sources import SourceError, ssga

    asked = []

    def down(name, url):
        asked.append(name)
        raise SourceError("down")

    monkeypatch.setattr(ssga, "fetch", down)
    with pytest.raises(SourceError):
        ssga.top10_concentration()
    assert asked == ["ssga_spy_xlsx", "slickcharts"]

    fallback = _sources_table()["Concentration (S2)"][1]
    assert "Slickcharts" in fallback
    assert "→" not in fallback and "->" not in fallback, "S2 has one fallback tier"


def test_readme_s4_row_names_the_value_a_failed_test_keeps():
    """A GSADF run that yields no statistic keeps the methodology's 0.25 in
    Block S, as state FLOOR with quality 0.0, so the coverage gate counts S4's
    weight as lost; 0.05 is what a completed test that does not reject scores.
    The row said 'floor 0.05'. In production the 6 snapshots without a
    statistic (ids 1, 2 and 11 to 14, 2026-07-11 to 2026-07-15) all stored
    0.25."""
    import dataclasses

    from app.indicators import s4_gsadf
    from app.references import REGISTRY
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    fallback = _sources_table()["GSADF (S4)"][1]
    assert s4_gsadf.sub_score(None, None, None, True) == s4_gsadf.SUB_CONTESTED_OR_STALE == 0.25
    assert "0.25" in fallback and "floor 0.05" not in fallback

    # The golden raw inputs carry no GSADF result: the state the cell describes.
    # The same inputs with a statistic show what the coverage gate loses.
    raw = make_golden_raw_inputs()
    failed = compute_snapshot(raw, mc_samples=1_000, mc_seed=20260711)
    tested = compute_snapshot(
        dataclasses.replace(raw, bsadf_stat=1.1315, bsadf_cv90=1.1393, bsadf_cv95=1.378),
        mc_samples=1_000, mc_seed=20260711)
    s4, weight = failed.indicators["s4"], REGISTRY["s4"].weight
    assert (s4.sub_score, s4.state, s4.quality, s4.dropped) == (0.25, "FLOOR", 0.0, False)
    assert tested.indicators["s4"].sub_score == s4_gsadf.SUB_NULL == 0.05
    assert (tested.coverage["S"]["coverage"] - failed.coverage["S"]["coverage"]
            == pytest.approx(weight))
    for stated in (f"`{s4.state}`", f"quality {s4.quality}", f"its {weight:g} as lost"):
        assert stated in fallback


def test_readme_s5_row_names_the_tiers_in_the_order_the_code_takes_them():
    """S5 reads the Fed's excess bond premium; without it the Baa spread, then
    HY OAS, and without all three it is dropped (REGISTRY s5). The table named
    HY OAS as S5's source. In production the premium scored S5 on 601 of the
    615 snapshots, HY OAS on 11 and the Baa spread on 3."""
    import dataclasses

    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    months = [float(i) for i in range(36)]
    raw = dataclasses.replace(make_golden_raw_inputs(), ebp_history=months,
                              baa_spread_history_bps=months)
    taken = []
    for gone in (None, "ebp_history", "baa_spread_history_bps", "hy_oas_history_bps"):
        if gone:
            raw = dataclasses.replace(raw, **{gone: None})
        s5 = compute_snapshot(raw, mc_samples=1_000, mc_seed=20260711).indicators["s5"]
        taken.append("dropped" if s5.dropped else (s5.data_source, s5.quality))
    assert taken == [("fed_ebp", 1.0), ("fred_BAA_DGS10", 0.5), ("fred_BAMLH0A0HYM2", 0.3),
                     "dropped"]

    rows = _sources_table()
    assert "Credit tightness (S5)" in rows, "the sources table has no row for S5's own reading"
    primary, fallback, _ = rows["Credit tightness (S5)"]
    assert "Excess Bond Premium" in primary
    places = [fallback.find(word) for word in
              ("`BAA`", "quality 0.5", "`BAMLH0A0HYM2`", "quality 0.3", "dropped")]
    assert -1 not in places and places == sorted(places)


def test_readme_message_limits_are_the_settings():
    """The lengths the README states are the settings' defaults. It said 200
    characters for the engine-on digest over iMessage (350 since the owner's
    request of 2026-10-04, docs/MESSAGE_ENGINE.md decision 28) and 1800 s for
    the LPPLS fit (1500)."""
    import re

    from app.config import Settings

    default = {name: field.default for name, field in Settings.model_fields.items()}
    row = next(line for line in README.splitlines() if line.startswith("| Length |"))
    imessage = re.split(r"(?<!\\)\|", row)[3]     # the SMS cell holds an escaped pipe
    stated = {
        f"{default['sms_max_len']} characters with the engine off": imessage,
        f"{default['message_engine_imessage_max_chars']} with it on": imessage,
        f"`LPPLS_TIMEOUT_S`, default {default['lppls_timeout_s']} s": README,
    }
    assert [text for text, where in stated.items() if text not in where] == []


def test_readme_names_every_admin_alert_action():
    """The admin alert row names every action the API serves under
    /api/v1/admin/alerts/. `render`, the preview of the reviewed test message,
    was served and not listed."""
    from app.main import create_app

    prefix = "/api/v1/admin/alerts/"
    actions = sorted(p[len(prefix):] for p in create_app().openapi()["paths"]
                     if p.startswith(prefix))
    row = next(line for line in README.splitlines()
               if line.startswith("| `POST /api/v1/admin/alerts/"))
    named = row.split("|")[1]
    assert actions, "no admin alert routes found"
    assert [a for a in actions if f"`{a}`" not in named and f"alerts/{a}`" not in named] == []


def test_readme_refresh_row_names_no_ping():
    """A recompute sends alerts and the failure alarm or the all-clear. The
    external ping went on the owner's word of 2026-10-04 (the comment in
    app/routers/admin.py, run_recompute_guarded); this row still listed it."""
    on_request = README[README.index("### On request"):README.index("### Never sent")]
    row = next(line for line in on_request.splitlines()
               if line.startswith("| `POST /api/v1/admin/refresh` |"))
    assert "ping" not in row


def test_readme_digest_howto_names_admission():
    """Engine on or off, scheduled or by the test call, the digest passes
    admission right before the wire: without it send_daily_digest answers
    status "refused" with its blockers and sends nothing. The how-to named the
    transports and the test call, and not this condition."""
    howto = README[README.index("## Daily digest"):
                   README.index("## Every message the service sends")]
    for stated in ("admission", "admin/alerts/promote", '"status": "refused"'):
        assert stated in howto
