"""D4 — LPPLS Confidence. weight = 0.20. LITERATURE-GROUNDED.

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["d4"]; summary:

    lppls PyPI 0.6.24 (PINNED), mp_compute_nested_fits on ^ndx & smh.us daily
    closes over a 2-3 yr window; filters m in (0,1), w in [4,25];
    confidence = fraction of fitting windows passing; sub_score = confidence.
    On computation failure -> DROP the indicator and renormalize Block D
    (NEVER a neutral placeholder — that was a v1 error).

CAVEAT (verbatim): LPPLS has documented false-alarm / too-early behavior; one
published evaluation reports ~90% recall but ~29% precision (it fires often
in ordinary bull markets). It is also computationally heavy. Treat the
sub-score as a noisy corroborator, not a stand-alone alarm.

EPISTEMIC GUARDRAILS (verbatim):
1. NOT-A-PROBABILITY. The headline is a 0-100 regime heuristic = structured
   expert judgment; it is uncalibrated and is not investment advice.
2. n ~= 4 CALIBRATION IMPOSSIBILITY. The reference class of comparable US
   equity manias is ~= {1929, 2000, 2007, 2021}. With ~4 events, no honest
   probability calibration is possible.
3. REFERENCE-CLASS CAVEAT. The current episode may be rational
   general-purpose-technology (GPT) repricing rather than a bubble. Chen,
   Chen & Huang (2026, arXiv 2604.25826) show GSADF-type tests spuriously
   reject the no-bubble null 93-100% of the time under hump-shaped GPT
   fundamentals; hence the GSADF indicator carries a low weight and a
   permanent CONTESTED flag.
4. NOMINAL != EFFECTIVE WEIGHTS. Nominal weights rarely equal a variable's
   realized influence (Paruolo, Saisana & Saltelli 2013). The service ships
   an annual sensitivity script computing first-order main effects and
   comparing them to nominal weights, flagging any |nominal - effective| > 0.10.
5. NEVER HTTP 500 ON DATA FAILURE. On any upstream data failure the service
   must fall back down a defined chain, or drop the indicator and renormalize
   its block, always attaching a provenance note. Upstream failure must never
   surface as a 500.
"""

from __future__ import annotations

FILTER_CONDITIONS = {"m_min": 0.0, "m_max": 1.0, "w_min": 4.0, "w_max": 25.0}


def confidence_from_fits(pass_flags: list[bool]) -> float:
    """confidence = fraction of fitting windows whose calibrated parameters
    satisfy the bubble-consistency filters."""
    if not pass_flags:
        raise ValueError("no LPPLS fitting windows")
    return sum(pass_flags) / len(pass_flags)


def sub_score(confidence: float) -> float:
    return max(0.0, min(1.0, confidence))


def compute_confidence(daily_closes: list[float]) -> float:
    """Fit LPPLS nested windows on 2-3 yr of daily closes; return confidence.

    Raises on any failure so the caller DROPS the indicator and renormalizes
    Block D (never a neutral placeholder). Requires the pinned lppls==0.6.24.
    """
    import os

    import numpy as np
    from lppls import lppls as lppls_mod

    if len(daily_closes) < 500:
        raise ValueError(f"insufficient price history (N={len(daily_closes)}; need >= 500)")
    time_idx = np.arange(len(daily_closes), dtype=float)
    price = np.log(np.asarray(daily_closes, dtype=float))
    model = lppls_mod.LPPLS(observations=np.array([time_idx, price]))
    res = model.mp_compute_nested_fits(
        workers=max(1, min(4, os.cpu_count() or 1)),  # respect container CPUs
        window_size=min(len(daily_closes), 504),
        smallest_window_size=120,
        outer_increment=21,
        inner_increment=5,
        max_searches=25,
        filter_conditions_config=[{"condition_1": {
            "tc_range": [0.0, 0.2], "m_range": [FILTER_CONDITIONS["m_min"], FILTER_CONDITIONS["m_max"]],
            "w_range": [FILTER_CONDITIONS["w_min"], FILTER_CONDITIONS["w_max"]],
            "O_min": 2.5, "D_min": 0.5,
        }}],
    )
    flags: list[bool] = []
    for window in res:
        qualified = window.get("res", []) if isinstance(window, dict) else []
        for fit in qualified:
            flags.append(bool(fit.get("qualified", {}).get("condition_1", False)))
    if not flags:
        raise ValueError("LPPLS produced no fit windows")
    return confidence_from_fits(flags)


def compute_confidence_isolated(daily_closes: list[float], timeout_s: int = 1800) -> float:
    """Run the LPPLS fit in a SUBPROCESS and return the confidence.

    Isolation exists because the lppls dependency stack (scipy, scikit-learn,
    numba) ships native wheels that can die with SIGILL on CPUs below their
    build baseline (e.g. pre-SSE4.2 Atoms) — an uncatchable in-process crash.
    In a subprocess, any crash/timeout surfaces as an exception here, and the
    caller drops D4 and renormalizes Block D (epistemic guardrail #5).
    """
    import json
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "app.indicators.d4_lppls"],
        input=json.dumps({"closes": daily_closes}),
        capture_output=True, text=True, timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"LPPLS subprocess failed rc={proc.returncode}: {proc.stderr.strip()[-300:]}"
        )
    return float(json.loads(proc.stdout.strip())["confidence"])


if __name__ == "__main__":
    import json as _json
    import sys as _sys

    _payload = _json.loads(_sys.stdin.read())
    print(_json.dumps({"confidence": compute_confidence(_payload["closes"])}))
