"""The science audit (app/references.py: KNOWN_ISSUES) names every deviation
a payload declares about itself."""

from __future__ import annotations

from app.references import KNOWN_ISSUES
from app.services.compute import ATH_PROVENANCE, S5_PROVISIONAL_LAG

_TEXT = " ".join(f"{k['title']} {k['detail']}" for k in KNOWN_ISSUES)


def test_every_deviation_a_payload_declares_is_in_the_catalogue():
    for defect in S5_PROVISIONAL_LAG["known_defects"]:
        assert defect in _TEXT, f"S5 payload declares {defect}; the science audit does not"
    assert S5_PROVISIONAL_LAG["s5_lag"] in _TEXT
    assert ATH_PROVENANCE["ath_basis"] in _TEXT
