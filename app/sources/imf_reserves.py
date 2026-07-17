"""IMF official-reserves shares — NON-SCORING dashboard context (v3.7.5).

Two quarterly, world-aggregate metrics for the companion dashboard. They are
CONTEXT only — neither enters a block, weight, or aggregation (guardrail #5):

  * cofer_ust_share_pct — USD share of ALLOCATED FX reserves. This IS a COFER
    series (COFER = Currency Composition of Official FX Reserves, FX only).
    Computed as USD-allocated / total-allocated * 100 from two COFER amount
    series (both in USD millions), so it needs no separate "share" codelist.

  * cofer_gold_share_pct — gold's share of TOTAL reserves. COFER excludes gold
    entirely, so this is NOT a COFER number despite the (frozen) key name; it
    comes from IFS: monetary gold at market value / total reserves * 100.

Transport: the classic IMF SDMX-JSON REST service (CompactData), which returns
plain JSON. Series keys are FREQ.REF_AREA.INDICATOR; the world aggregate is
REF_AREA=W00. (The newer https://api.imf.org/external/sdmx/3.0 service is the
long-term migration target; keep the keys below as the single source of truth.)

  ┌─────────────────────────────────────────────────────────────────────────┐
  │ CONFIRM THE SERIES KEYS ON GREENBOX — this build environment cannot      │
  │ reach imf.org, so the keys below are documented constants, not live-     │
  │ verified. The COFER RAXGFXAR<CCY>_USD amount pattern is well established; │
  │ the IFS world-gold/total-reserve keys are best-effort. One probe (needs  │
  │ a network policy that allows imf.org) confirms all four at once (the     │
  │ dataset/key pairs are the four *_KEY constants defined below):           │
  │                                                                          │
  │   base=http://dataservices.imf.org/REST/SDMX_JSON.svc/CompactData        │
  │   for DK in COFER/Q.W00.RAXGFXARUSD_USD COFER/Q.W00.RAXGFXAR_USD          │
  │             IFS/Q.W00.RAFAGOLDV_USD IFS/Q.W00.RAFA_USD ; do               │
  │     echo "== $DK =="; curl -s "$base/$DK" | head -c 500; echo; done       │
  │                                                                          │
  │ A wrong key degrades ONLY its own metric to available:false (no worse    │
  │ than the pre-v3.7.5 hardcoded placeholder) — it never fabricates a value │
  │ and never fails the recompute.                                           │
  └─────────────────────────────────────────────────────────────────────────┘

Both metrics are quarterly with a ~1-quarter publication lag, so they carry a
quarterly freshness SLA and degrade gracefully between releases.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.http_client import fetch
from app.sources import SourceError

SDMX_BASE = "http://dataservices.imf.org/REST/SDMX_JSON.svc/CompactData"

# (dataset, series_key) — see the confirm-on-greenbox banner above.
COFER_USD_ALLOCATED = ("COFER", "Q.W00.RAXGFXARUSD_USD")   # USD allocated FX, $mn
COFER_TOTAL_ALLOCATED = ("COFER", "Q.W00.RAXGFXAR_USD")    # total allocated FX, $mn
IFS_GOLD_VALUE = ("IFS", "Q.W00.RAFAGOLDV_USD")            # monetary gold @ market, $mn
IFS_TOTAL_RESERVES = ("IFS", "Q.W00.RAFA_USD")             # total reserves incl gold, $mn


@dataclass
class ReserveShares:
    """Each field is independently optional: one source failing never nulls the
    other. Shares are percent (0..100); as_of is the quarter-END ISO date."""

    usd_share_pct: float | None = None
    usd_share_as_of: str | None = None
    gold_share_pct: float | None = None
    gold_share_as_of: str | None = None


# ---------------------------------------------------------------------------
# SDMX period helpers (quarterly "2025-Q2"; tolerate monthly/annual defensively)
# ---------------------------------------------------------------------------

_QUARTER_END_MONTH = {1: "03", 2: "06", 3: "09", 4: "12"}
_QUARTER_END_DAY = {"03": "31", "06": "30", "09": "30", "12": "31"}


def _period_sortkey(period: str) -> tuple[int, int]:
    """Order SDMX periods chronologically. 'YYYY-Qn' -> (year, quarter);
    'YYYY-MM' -> (year, month); bare 'YYYY' -> (year, 0)."""
    if "-Q" in period:
        y, q = period.split("-Q", 1)
        return (int(y), int(q))
    if "-" in period:
        y, m = period.split("-", 1)
        return (int(y), int(m[:2]))
    return (int(period), 0)


def _quarter_end_iso(period: str) -> str:
    """Best-effort ISO date for staleness. Quarter -> quarter-END; a monthly or
    annual period returns a sensible month/year-END; anything else echoes back."""
    if "-Q" in period:
        y, q = period.split("-Q", 1)
        mm = _QUARTER_END_MONTH.get(int(q), "12")
        return f"{int(y):04d}-{mm}-{_QUARTER_END_DAY[mm]}"
    if "-" in period:
        y, m = period.split("-", 1)
        mm = m[:2]
        return f"{int(y):04d}-{mm}-{_QUARTER_END_DAY.get(mm, '28')}"
    return f"{int(period):04d}-12-31"


# ---------------------------------------------------------------------------
# fetch + parse (CompactData JSON: DataSet.Series.Obs, dict-or-list at each level)
# ---------------------------------------------------------------------------


def _series(dataset: str, series_key: str) -> dict[str, float]:
    """period -> value for one CompactData series. Malformed observations are
    skipped individually; a total failure raises SourceError."""
    url = f"{SDMX_BASE}/{dataset}/{series_key}"
    resp = fetch(f"imf:{dataset}:{series_key}", url)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SourceError(f"IMF {dataset}/{series_key}: non-JSON response") from exc

    dataset_node = (payload.get("CompactData") or {}).get("DataSet") or {}
    series = dataset_node.get("Series")
    if isinstance(series, list):
        series = next((s for s in series if s), None)
    if not isinstance(series, dict):
        return {}

    obs = series.get("Obs")
    if isinstance(obs, dict):
        obs = [obs]
    if not isinstance(obs, list):
        return {}

    out: dict[str, float] = {}
    for o in obs:
        if not isinstance(o, dict):
            continue
        period = o.get("@TIME_PERIOD")
        value = o.get("@OBS_VALUE")
        if period is None or value in (None, ""):
            continue
        try:
            out[str(period)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _share_of(numerator: tuple[str, str], denominator: tuple[str, str]) -> tuple[float, str] | None:
    """(pct, quarter-end ISO) for numer/denom on their LATEST common period, or
    None if either series is empty, they share no period, or the denom is 0."""
    num = _series(*numerator)
    den = _series(*denominator)
    common = sorted(set(num) & set(den), key=_period_sortkey)
    if not common:
        return None
    period = common[-1]
    if not den[period]:
        return None
    return round(num[period] / den[period] * 100.0, 2), _quarter_end_iso(period)


def fetch_reserve_shares() -> ReserveShares:
    """USD-allocated share (COFER) and gold-of-total share (IFS). The two are
    fetched independently — a failure in one leaves the other populated. This
    function never raises: any error yields a None field (available:false)."""
    out = ReserveShares()

    try:
        usd = _share_of(COFER_USD_ALLOCATED, COFER_TOTAL_ALLOCATED)
        if usd is not None:
            out.usd_share_pct, out.usd_share_as_of = usd
    except Exception as exc:  # noqa: BLE001 — degrade this metric only
        _log().warning("imf_cofer_usd_share_failed", error=str(exc)[:160])

    try:
        gold = _share_of(IFS_GOLD_VALUE, IFS_TOTAL_RESERVES)
        if gold is not None:
            out.gold_share_pct, out.gold_share_as_of = gold
    except Exception as exc:  # noqa: BLE001 — degrade this metric only
        _log().warning("imf_ifs_gold_share_failed", error=str(exc)[:160])

    return out


def _log():
    from app.logging_conf import get_logger

    return get_logger(__name__)
