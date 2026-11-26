"""A source's post-fetch derivation belongs inside that source's error boundary.

`gather_inputs` isolates every upstream behind `_track`, which records health
and returns None on failure so the indicator later drops and its block
renormalises (epistemic guardrail #5). That isolation only covers what is
CALLED INSIDE it. The EBP step used to fetch inside `_track` and then derive
outside it:

    ebp = _track(raw, "fed_ebp", fed_ebp_src.fetch_ebp)   # succeeded
    raw.ebp_as_of = _month_end_iso(ebp[-1][0][:7])        # raised, unguarded

so when the Fed changed its date format the fetch stayed green and the
DERIVATION raised — past every per-source guard, out of `gather_inputs`, and
into the scheduler, which recorded "recompute_failed" and wrote no snapshot.
Six times a day for twelve days, behind a green /healthz.
"""

from __future__ import annotations

import inspect

from app.services import compute


class TestTrackAbsorbsDerivationFailures:
    def test_a_raising_callable_degrades_that_source_only(self):
        raw = compute.RawInputs()

        def _derivation_that_raises() -> list[float]:
            # The production failure, verbatim: int("1/1/") on a US-format date.
            return [float(int("1/1/"))]

        assert compute._track(raw, "fed_ebp", _derivation_that_raises) is None
        assert "fed_ebp" in raw.gather_errors
        assert raw.source_health[-1]["source"] == "fed_ebp"
        assert raw.source_health[-1]["ok"] is False

    def test_the_failure_is_recorded_as_unhealthy_not_silently_swallowed(self):
        """A derivation failure must reach /readyz. The old shape reported
        `fed_ebp: ok` right up to the moment it took the recompute down."""
        raw = compute.RawInputs()

        def _boom() -> None:
            raise ValueError("invalid literal for int() with base 10: '1/1/'")

        compute._track(raw, "fed_ebp", _boom)
        assert not any(entry["ok"] for entry in raw.source_health)


class TestVendorDateDerivationsAreTracked:
    """Structural pin, in the style of tests/test_ath_provenance.py.

    The behaviour above proves the boundary works; these assert the two vendor
    date derivations are actually wired through it, which is the part that
    regressed."""

    def test_ebp_derivation_is_wired_through_track(self):
        src = inspect.getsource(compute.gather_inputs)
        assert '_track(raw, "fed_ebp", _ebp)' in src
        # the pre-fix wiring, which tracked the bare adapter and derived outside
        assert '_track(raw, "fed_ebp", fed_ebp_src.fetch_ebp)' not in src

    def test_hy_oas_date_parse_is_wired_through_track(self):
        src = inspect.getsource(compute.gather_inputs)
        assert '_track(raw, "fred_BAMLH0A0HYM2", _hy_oas)' in src
        assert 'lambda: fred_src.observations("BAMLH0A0HYM2")' not in src
