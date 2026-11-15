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


def test_raw_inputs_declares_no_field_the_pipeline_never_reads_or_never_sets():
    """Every RawInputs field is read somewhere in app/, and is given a value
    somewhere: assigned, passed as a keyword (the golden inputs in
    tests/conftest.py), or a container the gather fills in place.

    A field that is set and never read stores a fact nobody is told; a field
    that is read and never set is a branch that cannot run. Four such fields
    stood here: real10y_as_of, lppls_note and vix_ratio_fallback were set once
    and read nowhere, and margin_note was read once (as a prefix of d2's note)
    and set nowhere. A dropped d4 words its own note from lppls_result and
    gather_errors, S1 is dated by cape_as_of, and the VIX ratio's fallback is
    named by vix_ratio_source.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    paths = [*sorted((root / "app").rglob("*.py")), root / "tests" / "conftest.py"]
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in paths]
    module = ast.parse(Path(compute.__file__).read_text(encoding="utf-8"))
    raw_inputs = next(node for node in module.body
                      if isinstance(node, ast.ClassDef) and node.name == "RawInputs")
    declared = [node for node in raw_inputs.body if isinstance(node, ast.AnnAssign)]
    fields = [node.target.id for node in declared]
    # field(default_factory=...): source_health and gather_errors, appended to
    # and keyed into, never assigned.
    containers = {node.target.id for node in declared
                  if isinstance(node.value, ast.Call)
                  and getattr(node.value.func, "id", "") == "field"}

    loads: set[str] = set()
    stores: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                (loads if isinstance(node.ctx, ast.Load) else stores).add(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                stores.add(node.arg)

    assert "breadth_pct" in fields and "gather_errors" in containers, "the scan sees the class"
    never_read = [f for f in fields if f not in loads]
    never_set = [f for f in fields if f not in stores and f not in containers]
    assert (never_read, never_set) == ([], [])


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
