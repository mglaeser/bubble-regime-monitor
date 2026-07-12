"""D4 — LPPLS Confidence. weight = 0.20. LITERATURE-GROUNDED.

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["d4"]; summary:

    lppls PyPI 0.6.24 (PINNED), mp_compute_nested_fits on the NDX/QQQ-proxy
    daily closes over a 2-3 yr window; filters m in (0,1), w in [4,25];
    confidence = positive-bubble confidence (pos_conf) from compute_indicators,
    averaged over the most recent windows; sub_score = confidence.
    On computation failure -> DROP the indicator and renormalize Block D
    (NEVER a neutral placeholder — that was a v1 error).

    lppls 0.6.24 API NOTE: filter_conditions_config is a FLAT dict[str, float]
    (keys: m_min/m_max/w_min/w_max/O_min/D_min/tc_min_days/tc_max_days/
    tc_min_frac/tc_max_frac) — passing the older list-of-{"condition_1": {...}}
    form raises "filter_conditions_config must be a dict[str, float] or None".
    mp_compute_nested_fits returns RAW fits; qualification (is_qualified ->
    pos_conf/neg_conf) is produced by compute_indicators, which must be called
    separately. Getting either wrong is what silently broke D4.

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

# lppls 0.6.24 filter thresholds — a FLAT dict[str, float] (NOT the older
# list-of-{"condition_1": {...}} form). Only m/w bounds are overridden; O_min,
# D_min and the tc-window fractions keep the library's DS-LPPLS defaults
# (O_min=2.5, D_min=0.5, tc_min_days=60, tc_max_days=252, tc_*_frac=0.5).
FILTER_CONDITIONS: dict[str, float] = {
    "m_min": 0.0, "m_max": 1.0, "w_min": 4.0, "w_max": 25.0,
}

# Nested-fit parameters. Sized for the target host (Intel Atom N2800, 1.86 GHz,
# workers pinned to 1). EMPIRICAL: outer_increment=8 measured ~87 s on a modern
# core here but TIMED OUT past 600 s on the N2800 (the Atom is ~7-10x slower).
# These lighter values measure ~64 s here -> ~500-640 s projected on the Atom.
# LPPLS has a per-window overhead floor, so cutting fit-units has diminishing
# returns; the real safety margin comes from the raised lppls_timeout_s (1500 s).
# D4 is a weight-0.20 "noisy corroborator", so a coarser-but-completing fit is
# strictly better than a perpetual timeout-drop. If it still times out on some
# host it simply drops and Block D renormalizes.
LPPLS_WORKERS = 1
LPPLS_WINDOW_SIZE = 120
LPPLS_SMALLEST_WINDOW = 30
LPPLS_OUTER_INCREMENT = 16
LPPLS_INNER_INCREMENT = 6
LPPLS_MAX_SEARCHES = 15
# Average pos_conf over this many most-recent windows for the "current" reading.
LPPLS_RECENT_WINDOWS = 5
MIN_CLOSES = 500
# Frames the confidence result on the subprocess stdout (lppls prints fit
# exceptions to stdout, so the result line must be unambiguously identifiable).
_RESULT_SENTINEL = "LPPLSCONF:"


def sub_score(confidence: float) -> float:
    return max(0.0, min(1.0, confidence))


def _confidence_from_indicators(pos_conf: list[float]) -> float:
    """Current positive-bubble confidence = mean pos_conf over the most recent
    LPPLS_RECENT_WINDOWS windows (smooths single-window noise). pos_conf is
    already in [0, 1]; an all-zero tail is a legitimate 0.0 reading, not a drop."""
    if not pos_conf:
        raise ValueError("LPPLS produced no fit windows")
    tail = pos_conf[-min(len(pos_conf), LPPLS_RECENT_WINDOWS):]
    return max(0.0, min(1.0, sum(tail) / len(tail)))


def compute_confidence(daily_closes: list[float]) -> float:
    """Fit LPPLS nested windows on 2-3 yr of daily closes; return confidence.

    Raises on any failure so the caller DROPS the indicator and renormalizes
    Block D (never a neutral placeholder). Requires the pinned lppls==0.6.24.
    """
    import numpy as np
    from lppls import lppls as lppls_mod

    from app.logging_conf import get_logger

    log = get_logger(__name__)
    n = len(daily_closes)
    if n < MIN_CLOSES:  # log N explicitly so a data shortfall is never again
        # mistaken for a code fault (that confusion is exactly what happened).
        raise ValueError(f"insufficient price history (N={n}; need >= {MIN_CLOSES})")
    log.info("lppls_fit", n=n, workers=LPPLS_WORKERS, window_size=LPPLS_WINDOW_SIZE,
             outer_increment=LPPLS_OUTER_INCREMENT)

    time_idx = np.arange(n, dtype=float)
    price = np.log(np.asarray(daily_closes, dtype=float))
    model = lppls_mod.LPPLS(observations=np.array([time_idx, price]))

    # Step 1: raw nested fits. filter_conditions_config is stored on the model
    # here but qualification happens in compute_indicators (0.6.24 contract).
    res = model.mp_compute_nested_fits(
        workers=LPPLS_WORKERS,
        window_size=min(n, LPPLS_WINDOW_SIZE),
        smallest_window_size=LPPLS_SMALLEST_WINDOW,
        outer_increment=LPPLS_OUTER_INCREMENT,
        inner_increment=LPPLS_INNER_INCREMENT,
        max_searches=LPPLS_MAX_SEARCHES,
        filter_conditions_config=FILTER_CONDITIONS,
    )
    # Step 2: qualify fits -> per-window positive/negative-bubble confidence.
    indicators = model.compute_indicators(res, filter_conditions_config=FILTER_CONDITIONS)
    pos_conf = [float(v) for v in indicators["pos_conf"].tolist()]
    return _confidence_from_indicators(pos_conf)


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
    # lppls prints fit exceptions to stdout (lppls.py:960) from forked workers,
    # so stdout is NOT pure JSON. The result is framed with a sentinel and is
    # the last line the parent writes (after the Pool joins) — parse that line.
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(_RESULT_SENTINEL):
            return float(json.loads(line[len(_RESULT_SENTINEL):])["confidence"])
    raise RuntimeError(f"LPPLS subprocess produced no result line: {proc.stdout.strip()[-300:]}")


if __name__ == "__main__":
    import json as _json
    import sys as _sys

    _payload = _json.loads(_sys.stdin.read())
    # Route all fit-time output (tqdm, lppls' print(e) in parent AND forked
    # workers) to stderr so stdout carries only the framed result line.
    _real_stdout = _sys.stdout
    _sys.stdout = _sys.stderr
    try:
        _conf = compute_confidence(_payload["closes"])
    finally:
        _sys.stdout = _real_stdout
    _sys.stdout.write(f"{_RESULT_SENTINEL}{_json.dumps({'confidence': _conf})}\n")
    _sys.stdout.flush()
