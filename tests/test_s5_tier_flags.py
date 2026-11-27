"""S5's HY OAS tier, the last tier of its chain, says it is a fallback.

Written from the chain as documented (docs/frozen_pin_manifest.md section 11:
"s5 chain: EBP (>=24 mo) -> BAA-DGS10 proxy (>=24 mo) -> HY-OAS 3yr -> drop";
SOURCE_REGISTRY "fred_hyoas": "FRED S5 fallbacks: BAA-DGS10 proxy +
BAMLH0A0HYM2 (HY OAS)") and from the field's contract (app/schemas.py:
fallback_used is "True if a fallback source served this reading").
"""

from __future__ import annotations

from app.services.compute import compute_snapshot
from tests.conftest import make_golden_raw_inputs


def test_the_hy_oas_tier_is_a_fallback():
    s5 = compute_snapshot(make_golden_raw_inputs(), mc_samples=2_000,
                          mc_seed=20260711).indicators["s5"]
    assert (s5.data_source, s5.fallback_used) == ("fred_BAMLH0A0HYM2", True)
