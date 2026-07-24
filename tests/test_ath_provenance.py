"""PIN-F (2026-07-23 operator decision) — honest ATH-basis provenance label.

The production red flag is UNCHANGED: `within_2pct_of_ath` is still computed as
closes[-1] >= 0.98 * max(adjusted-SPY provider-window closes). What changes is
observability only: every snapshot now carries an explicit label that the
current basis is the adjusted-SPY provider-window maximum and that the
historical ATH seed is NOT complete. The label must not alter score behavior.
"""

from __future__ import annotations

from app.services import compute


def test_ath_provenance_constant():
    assert compute.ATH_PROVENANCE == {
        "ath_basis": "adjusted_spy_provider_window_max",
        "ath_history_complete": False,
    }


def test_snapshot_data_carries_ath_provenance():
    import dataclasses
    fields = {f.name for f in dataclasses.fields(compute.SnapshotData)}
    assert "ath_provenance" in fields


def test_red_flag_rule_untouched():
    # governance cleanup: the rule VALUE is still 0.98, but its single causal
    # source is now the artifact (no hardcoded 0.98 in the gather path).
    from app import methodology as M
    assert M.get_path("red_flags", "index_near_ath_frac") == 0.98
    assert compute._NEAR_ATH_FRAC == 0.98
    import inspect
    src = inspect.getsource(compute.gather_inputs)
    assert "_NEAR_ATH_FRAC * max(closes)" in src
    assert "0.98" not in src
