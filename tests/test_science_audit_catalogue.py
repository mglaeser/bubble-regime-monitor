"""The science audit lists what the service itself declares, and nothing the
code no longer does (app/references.py: KNOWN_ISSUES is "everything currently
known to be UNVERIFIED, CONTESTED, PROXIED, JUDGMENTAL, or a documented
DEVIATION"; the status page shows it).

Executed on a copy of the production database, 2026-11-11: 533 of 615
snapshots carry the S5 stamp (s5_lag provisional_positional, known_defects
C-01, C-02, C-03) and 533 the _ath_provenance stamp; 490 of 615 carry
override_required_count 3 beside override_fireable_universe_count 3 (the
first 125 predate the two columns); the newest S4 reading is 0.05, computed
against cached Monte Carlo critical values (r/gsadf.R calls radf_mc_cv).
"""

from __future__ import annotations

from pathlib import Path

from app.references import KNOWN_ISSUES

_TEXT = " ".join(f"{k['title']} {k['detail']}" for k in KNOWN_ISSUES)


def test_every_deviation_a_payload_declares_is_in_the_catalogue():
    # A change that removes one of the two stamps removes its entry with it.
    from app.services.compute import ATH_PROVENANCE, S5_PROVISIONAL_LAG

    for defect in S5_PROVISIONAL_LAG["known_defects"]:
        assert defect in _TEXT, f"S5 payload declares {defect}; the science audit does not"
    assert S5_PROVISIONAL_LAG["s5_lag"] in _TEXT
    assert ATH_PROVENANCE["ath_basis"] in _TEXT


def test_the_catalogue_describes_the_critical_values_the_r_script_computes():
    script = Path(__file__).resolve().parents[1] / "r" / "gsadf.R"
    calls = [line for line in script.read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("#")]
    assert any("radf_mc_cv(" in line for line in calls)
    assert not any("radf_wb_cv(" in line for line in calls)
    assert "wild-bootstrap" not in _TEXT and "radf_wb_cv" not in _TEXT


def test_the_catalogue_does_not_say_s4_is_capped_when_a_non_rejection_scores_0_05():
    from app import methodology as M

    assert M.get_path("gsadf", "contested_rule") == "asymmetric"
    assert "still capped" not in _TEXT


def test_the_status_page_shows_the_declared_deviations():
    from app.services.status import _science_audit

    ids = {flag["id"] for flag in _science_audit(None, [], [], {})["flags"]}
    assert {"s5-positional-lag", "ath-window-high"} <= ids
    assert "gsadf-wb-cv" not in ids


def test_the_contested_entry_says_what_the_block_does_to_the_override():
    # app/engine/snapshot_contract.py: red flag 1 is fireable only while GSADF
    # is not contested, and contested is the configured default.
    from app import methodology as M
    from app.config import Settings

    assert Settings.model_fields["gsadf_contested"].default is True
    assert M.get_path("override", "min_flags") == 3
    entry = next(k for k in KNOWN_ISSUES if k["id"] == "gsadf-contested")
    assert "all three remaining flags" in entry["detail"]
