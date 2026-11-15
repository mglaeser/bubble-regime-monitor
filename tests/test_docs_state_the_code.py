"""Statements a reader acts on, held to the code they describe.

Comments are not pinned one by one. Pinned here is what is served (an OpenAPI
description), what an internal document says about how the engine gets its
constants, and the executed facts behind two corrected docstrings.

Where a number decided the wording it is production's (the database's
read-only copy, 615 snapshots from 2026-07-11 to 2026-11-10; the service log
from 2026-10-05 to 2026-11-10 with 222 scheduled recomputes):

- a recompute ended 1.4 to 5.7 minutes after its slot's :00 (median 2.7), and
  the LPPLS fit in it took 16 to 37 s (median 19);
- red flag 4 was ACTIVE on 176 of the 490 snapshots that carry the typed
  contract, its distance negative on all 176; flags 1 to 3 never fired, their
  distances negative on 490 of 490;
- the breadth sweep is a job of its own (app/scheduler.py); the recompute reads
  the cache it fills.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (ROOT / "docs" / "frozen_pin_manifest.md").read_text(encoding="utf-8")
#: The governance note: everything above the first rule of the page.
MANIFEST_HEADER = " ".join(MANIFEST.split("\n---\n", 1)[0].replace(">", " ").split())


def test_the_refresh_route_names_no_breadth_sweep():
    """`POST /api/v1/admin/refresh` said the recompute runs the breadth sweep.
    The sweep is a scheduler job of its own; the recompute never calls it."""
    from app.main import create_app
    from app.services import compute

    operation = create_app().openapi()["paths"]["/api/v1/admin/refresh"]["post"]
    assert "breadth sweep" not in operation["description"]
    assert "refresh_breadth" not in inspect.getsource(compute)


def test_the_manifest_says_the_engine_loads_the_artifact():
    """The page said the engine was not wired to the artifact. It is: the
    constants are read from `frozen_methodology.json` at import."""
    from app import methodology
    from app.services.compute import FRESHNESS_SLA_DAYS

    assert "**NOT** yet wired" not in MANIFEST
    assert "not yet switched to load" not in MANIFEST
    assert "app/methodology.py" in MANIFEST_HEADER
    assert FRESHNESS_SLA_DAYS == methodology.as_dict("freshness_sla_days")


def test_the_manifest_quotes_the_golden_numbers(golden_subscores, golden_mc_inputs):
    """52.43 is the golden fixture's deterministic point score. The page called
    it the seeded Monte Carlo median, which is 52.56."""
    from app.engine.aggregate import RedFlags, deterministic_score
    from app.engine.montecarlo import BASE_WEIGHTS_D, BASE_WEIGHTS_S, monte_carlo

    sub_s, sub_d = golden_subscores
    point = deterministic_score(sub_s, sub_d, 1.0, RedFlags(),
                                BASE_WEIGHTS_S, BASE_WEIGHTS_D).score
    median = monte_carlo(golden_mc_inputs, n=100_000, seed=20260711).median
    assert (f"{point:.2f}", f"{median:.2f}") == ("52.43", "52.56")
    assert f"point score is **{point:.2f}**" in MANIFEST_HEADER
    assert f"**{median:.2f}**" in MANIFEST_HEADER
    assert f"median) is **{point:.2f}**" not in MANIFEST_HEADER


def _flag_facts(*, runup_pp: float, breadth_pct: float) -> dict[str, dict]:
    from app.engine.aggregate import evaluate_red_flags
    from app.engine.snapshot_contract import build_red_flag_meta

    flags = evaluate_red_flags(
        gsadf_explosive_p05=False, gsadf_contested=True, semi_runup_pp=runup_pp,
        hy_oas_bps=300.0, hy_oas_3yr_tight_bps=280.0, breadth_pct=breadth_pct,
        index_within_2pct_of_ath=True)
    return build_red_flag_meta(
        red_flags=flags, observed_at="2026-11-10T22:00:00+00:00",
        gsadf_stat=1.0, gsadf_cv95=1.4, gsadf_contested=True, gsadf_available=True,
        gsadf_as_of="2026-10-31", gsadf_stale=False,
        semi_runup_pp=runup_pp, semis_as_of="2026-11-10", semis_stale=False,
        hy_oas_bps=300.0, hy_oas_tight_bps=280.0, hy_oas_as_of="2026-11-10",
        hy_oas_stale=False, breadth_pct=breadth_pct, breadth_as_of="2026-11-10",
        breadth_stale=False, near_ath_available=True, near_ath_state=True)["flags"]


def test_a_negative_distance_is_the_firing_side_for_rf4_and_the_calm_side_for_rf2():
    """`distance_to_threshold` is the reading minus the threshold for every
    flag. Flags 1 to 3 fire above theirs, flag 4 (breadth) below its own, so
    one sign means opposite things: the two cases RedFlagFact's docstring
    states."""
    facts = _flag_facts(runup_pp=100.0, breadth_pct=40.0)
    assert facts["rf4"]["state"] == "ACTIVE"
    assert facts["rf4"]["distance_to_threshold"] == pytest.approx(-10.0)
    assert facts["rf2"]["state"] == "INACTIVE"
    assert facts["rf2"]["distance_to_threshold"] == pytest.approx(-50.0)

    calm = _flag_facts(runup_pp=160.0, breadth_pct=60.0)
    assert calm["rf4"]["state"] == "INACTIVE"
    assert calm["rf4"]["distance_to_threshold"] == pytest.approx(10.0)
    assert calm["rf2"]["state"] == "ACTIVE"
    assert calm["rf2"]["distance_to_threshold"] == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("status", "calls", "waits"),
    [(429, 3, [1.0, 2.0]), (503, 3, [1.0, 2.0]), (403, 1, [])],
)
def test_a_429_is_retried_twice_and_a_403_not_at_all(monkeypatch, status, calls, waits):
    """What app/sources/edgar.py says of its requests, executed on the shared
    client it uses: a 429 or a 5xx is asked again after 1 s and after 2 s, a
    403 is not asked again. No request leaves the test."""
    from app import http_client

    seen: list[str] = []
    slept: list[float] = []

    class _Client:
        def get(self, url, **_kwargs):
            seen.append(url)
            return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(http_client, "get_client", lambda: _Client())
    monkeypatch.setattr(http_client._get_with_retry.retry, "sleep", slept.append)

    with pytest.raises(httpx.HTTPStatusError):
        # A label of its own, so no other test meets this one's failures.
        http_client.fetch(f"docs_state_the_code_{status}", "https://data.sec.gov/x")
    assert len(seen) == calls
    assert slept == waits
