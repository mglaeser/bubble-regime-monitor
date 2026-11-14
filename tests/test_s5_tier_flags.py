"""Each tier of S5's chain says whether it is a fallback.

Written from the chain as documented (docs/frozen_pin_manifest.md section 11:
"s5 chain: EBP (>=24 mo) -> BAA-DGS10 proxy (>=24 mo) -> HY-OAS 3yr -> drop";
SOURCE_REGISTRY "fred_hyoas": "FRED S5 fallbacks: BAA-DGS10 proxy +
BAMLH0A0HYM2 (HY OAS)") and from the field's contract (app/schemas.py:
fallback_used is "True if a fallback source served this reading").

The second tier said True and the third, the weakest (quality 0.3), said
False: the value dated from when HY OAS was the first tier. Production,
615 snapshots to 2026-11-10: 601 on the EBP, 3 on the proxy (True) and 11 on
HY OAS (False), all 14 from 2026-07-11 to 07-15, before the EBP was read. The
tier has not served since; had it, the status page's "S5 served via a fallback
source" would not have shown.
"""

from __future__ import annotations

import pytest

from app.services.compute import compute_snapshot
from tests.conftest import make_golden_raw_inputs

EBP = [0.1 * ((i % 17) - 8) for i in range(120)]
BAA = [200.0 + (i % 40) for i in range(120)]


@pytest.mark.parametrize(("ebp", "baa", "source", "fallback"), [
    (EBP, BAA, "fed_ebp", False),
    (None, BAA, "fred_BAA_DGS10", True),
    (None, None, "fred_BAMLH0A0HYM2", True),     # the weakest tier, quality 0.3
])
def test_only_the_ebp_tier_is_not_a_fallback(ebp, baa, source, fallback):
    raw = make_golden_raw_inputs()
    raw.ebp_history, raw.ebp_as_of = ebp, raw.hy_oas_as_of
    raw.baa_spread_history_bps, raw.baa_spread_as_of = baa, raw.hy_oas_as_of
    s5 = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711).indicators["s5"]
    assert (s5.data_source, s5.fallback_used) == (source, fallback)
    assert s5.payload()["fallback_used"] is fallback
