"""D4 is reproducible: the same closes give the same reading.

AGENTS.md ground rule 6: "Reproducibility is pinned." README: a "transparent,
reproducible" heuristic. The LPPLS fit draws every fit's starting point at
random; drawn unseeded, a recompute of unchanged closes published another D4
and another headline. In production (2026-07-11 to 2026-11-10) D4 differed in
399 of the 407 consecutive snapshot pairs in which no other input had changed,
the headline moved by up to 1.73 points on such a pair, and in 11 of the 16
changes between the trim and de-risk bands D4 was the only input that had moved
(one of them sent the priority-1 alert of 2026-09-30 22:01 UTC).
"""

from __future__ import annotations

import math
import random

import pytest


def _closes(n: int) -> list[float]:
    """A fixed series shaped like a market's: a faster-than-exponential rise
    with a log-periodic wobble, plus a fixed random walk of 0.6 % a day. On
    it six unseeded fits gave six readings, 0.60 to 0.77 (2026-11-11), so an
    unseeded fit does not pass this test by chance."""
    walk = random.Random(3)
    critical, drift, out = n + 60, 0.0, []
    for t in range(n):
        left = critical - t
        drift += walk.gauss(0.0, 0.006)
        wobble = 0.03 * math.cos(8.0 * math.log(left))
        out.append(math.exp(6.0 - 0.09 * left ** 0.45 * (1.0 + wobble) + drift))
    return out


def test_the_same_closes_give_the_same_d4_through_the_production_path():
    # The real engine in the real subprocess, as a recompute runs it. The image
    # and CI install lppls from the locks; an environment without it self-skips.
    pytest.importorskip("lppls")
    from app.indicators.d4_lppls import MIN_CLOSES, compute_confidence_isolated

    closes = _closes(MIN_CLOSES)
    first = compute_confidence_isolated(closes, timeout_s=600)
    second = compute_confidence_isolated(closes, timeout_s=600)
    assert first["state"] == "VALID", first
    # the fit ran and windows qualified: two equal FLOORs or zeros prove nothing
    assert first["n_windows_qualifying"] > 0
    # the whole result: the value, the window counts and the three scale bands.
    # No value is pinned: the fit is the same on one machine, and its last bits
    # may differ between machines (JIT and BLAS rounding).
    assert first == second
