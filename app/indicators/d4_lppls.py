"""D4 — LPPLS Confidence. weight = 0.20. LITERATURE-GROUNDED.

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["d4"]; summary:

    lppls PyPI 0.6.24 (PINNED). v3.3.2 SINGLE-ENDPOINT DENSE SCAN: one fitting
    endpoint t2 = today, start times t1 shrinking from dt=750 down to dt=30
    trading days in steps of 5 (~144 windows); filters m in (0,1), w in [4,25].
    confidence = fraction of start-time windows whose calibration passes the
    filters AT THIS ENDPOINT (Sornette et al. 2015; Demirer et al. 2019) —
    the library's per-endpoint pos_conf, reported as the scalar directly.
    Three dt-band fractions (short 30-63 / medium 63-252 / long 252-750) are
    partitioned from the SAME fits (Demirer 2019 multi-scale structure at zero
    extra cost) and surfaced as payload diagnostics — never headline inputs.

    Design note (v3.3.2): the previous grid slid ~40 endpoints of <=120-day
    windows and averaged pos_conf over the most recent 5. A "now" gauge needs
    the present endpoint only; sampling ONE endpoint densely is both cheaper
    (~144 fits vs ~600) and examines 6x the scale range (dt up to 750 vs 120).
    Values are therefore NOT comparable across the v3.3.0->v3.3.2 boundary.

    lppls 0.6.24 API NOTE: filter_conditions_config is a FLAT dict[str, float];
    mp_compute_nested_fits returns RAW fits; qualification (is_qualified ->
    pos_conf) is produced by compute_indicators, whose DataFrame also carries
    the per-fit `_fits` dicts this module partitions into bands.

    STATE CONTRACT (v3.3.2, supersedes the v3.3.0 tri-state):
      VALID             computed, confidence > 0            quality 1.0 / 0.5
      VALID_ZERO        computed, genuinely zero — a real   quality 1.0 / 0.5
                        reading that ENTERS the aggregation
      INSUFFICIENT_DATA < MIN_CLOSES closes, nothing fitted  quality 0.0
      FLOOR             timeout / crash / no fit windows —   quality 0.0
                        the row stays in the payload but the
                        value is EXCLUDED from the geometric
                        mean and Block D renormalizes; an
                        uncomputed indicator must never
                        masquerade as a confident zero
    quality: 1.0 when >= LPPLS_MIN_WINDOWS_FULL_QUALITY start-time windows
    were evaluated, 0.5 below that (partial history), 0.0 when nothing was
    computed. quality feeds the coverage gate, never the score itself.

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

import math

from app import methodology as _M

# lppls 0.6.24 filter thresholds — a FLAT dict[str, float] (NOT the older
# list-of-{"condition_1": {...}} form). Only m/w bounds are overridden; O_min,
# D_min and the tc-window fractions keep the library's DS-LPPLS defaults
# (O_min=2.5, D_min=0.5, tc_min_days=60, tc_max_days=252, tc_*_frac=0.5).
FILTER_CONDITIONS: dict[str, float] = _M.as_dict("indicators", "d4", "filter_conditions")

# Single-endpoint dense-scan parameters (v3.3.2). t2 = the latest close; start
# times shrink the window from LPPLS_WINDOW_MAX down to LPPLS_SMALLEST_WINDOW
# in LPPLS_INNER_INCREMENT-day steps -> ~144 start-time windows at ONE endpoint
# (the library grid yields dt in {34, 39, ..., 749} for a 750-close window).
# Atom N2800 budget: ~144 fits x max_searches=15 projects ~650-850 s (measured
# against the v3.2 calibration of 675k point-searches ~ 64 s on a dev core,
# Atom ~8x slower) — inside the 1500 s subprocess timeout with margin. Raise
# max_searches only after a timed host run.
LPPLS_WORKERS = _M.get_path("indicators", "d4", "workers")
LPPLS_WINDOW_MAX = _M.get_path("indicators", "d4", "window_max")          # 750 trading days
LPPLS_SMALLEST_WINDOW = _M.get_path("indicators", "d4", "smallest_window")      # 30
LPPLS_INNER_INCREMENT = _M.get_path("indicators", "d4", "inner_increment")       # 5
LPPLS_MAX_SEARCHES = _M.get_path("indicators", "d4", "max_searches")
LPPLS_MIN_WINDOWS_FULL_QUALITY = _M.get_path("indicators", "d4", "min_windows_full_quality")  # 100
MIN_CLOSES = _M.get_path("indicators", "d4", "min_closes")
# dt bands (trading days) for the multi-scale diagnostics; long extends to the
# scan maximum so every fitted window belongs to exactly one band.
LPPLS_BANDS: tuple[tuple[str, int, int], ...] = tuple(
    tuple(b) for b in _M.get_path("indicators", "d4", "bands"))
# Frames the confidence result on the subprocess stdout (lppls prints fit
# exceptions to stdout, so the result line must be unambiguously identifiable).
_RESULT_SENTINEL = "LPPLSCONF:"


def sub_score(confidence: float) -> float:
    # v3.7.8/L-01: a non-finite confidence must never silently become a VALID
    # sub-score. The callers guard this (the FLOOR path handles bad fits), so a
    # NaN/inf reaching here is a contract violation -> raise, do not clip.
    if not math.isfinite(confidence):
        raise ValueError(f"LPPLS confidence is non-finite: {confidence!r}")
    return max(0.0, min(1.0, confidence))


def _quality(n_windows_evaluated: int) -> float:
    """Coverage-gate quality from the evaluated start-time window count:
    full history -> 1.0; a shortened scan (< ~100 windows, i.e. < ~530 closes)
    -> 0.5; nothing computed -> 0.0 (callers use the FLOOR/INSUFFICIENT paths)."""
    if n_windows_evaluated <= 0:
        return 0.0
    return 1.0 if n_windows_evaluated >= LPPLS_MIN_WINDOWS_FULL_QUALITY else 0.5


def _positive_qualified(fits: list[dict]) -> tuple[int, int] | None:
    """(qualifying, positive) window counts using the SAME normalization as the
    library's pos_conf: a window is 'positive-bubble' when b < 0, and confidence
    = (qualified positive windows) / (positive windows). Returns None on a
    schema surprise so the scalar path never depends on this."""
    try:
        pos = [bool(f["is_qualified"]) for f in fits if float(f["b"]) < 0.0]
    except (KeyError, TypeError, ValueError):
        return None
    return (sum(pos), len(pos))


def band_fractions(fits: list[dict]) -> dict[str, dict[str, float | int]] | None:
    """Partition confidence by window length dt = t2 - t1 into the LPPLS_BANDS
    scales, using the SAME positive-bubble normalization as the headline
    pos_conf (qualified-positive / positive within each band). Returns
    {band: {"conf": frac|None, "n": positive-window count}}; None on a schema
    surprise (the scalar path must never depend on this)."""
    try:
        rows = [(float(f["t2"]) - float(f["t1"]), bool(f["is_qualified"]), float(f["b"]))
                for f in fits]
    except (KeyError, TypeError, ValueError):
        return None
    out: dict[str, dict[str, float | int]] = {}
    last = LPPLS_BANDS[-1][0]
    for name, lo, hi in LPPLS_BANDS:
        # positive-bubble windows in this dt band; top band inclusive of the max
        pos = [q for dt, q, b in rows
               if b < 0.0 and (lo <= dt <= hi if name == last else lo <= dt < hi)]
        out[name] = {"conf": (sum(pos) / len(pos)) if pos else None, "n": len(pos)}
    return out


def _floor(n_closes: int, reason: str) -> dict:
    return {"state": "FLOOR", "value": None, "quality": 0.0,
            "n_windows_evaluated": 0, "n_windows_positive": 0, "n_windows_qualifying": 0,
            "n_closes": n_closes, "window": None, "bands": None,
            "reason": reason[-200:]}


def compute_confidence(daily_closes: list[float]) -> dict:
    """Fit ONE dense-scanned endpoint (t2 = the latest close) and return:

        {state, value, quality, n_windows_evaluated, n_windows_qualifying,
         n_closes, window, bands[, reason]}

    value = the LPPLS confidence at t2: the fraction of POSITIVE-BUBBLE start-
    time windows (fit b < 0; dt from LPPLS_SMALLEST_WINDOW to LPPLS_WINDOW_MAX)
    whose calibrated parameters pass the DS-LPPLS filters — per Sornette et al.
    (2015) / Demirer et al. (2019). value ==
    n_windows_qualifying / n_windows_POSITIVE (the library's pos_conf
    denominator), NOT / n_windows_evaluated (v3.7.4/L-03 docstring fix).
    States/quality per the module-header contract. Requires lppls==0.6.24.
    """
    from app.logging_conf import get_logger

    log = get_logger(__name__)
    n = len(daily_closes)
    if n < MIN_CLOSES:  # log N explicitly so a data shortfall is never again
        # mistaken for a code fault (that confusion is exactly what happened).
        # A-02: this contract must hold WITHOUT the optional lppls engine, so the
        # length guard precedes the import (the INSUFFICIENT_DATA path fits nothing).
        return {"state": "INSUFFICIENT_DATA", "value": None, "quality": 0.0,
                "n_windows_evaluated": 0, "n_windows_positive": 0, "n_windows_qualifying": 0,
                "n_closes": n, "window": None, "bands": None}

    import numpy as np
    from lppls import lppls as lppls_mod

    window = min(n, LPPLS_WINDOW_MAX)
    log.info("lppls_fit", n=n, window=window, workers=LPPLS_WORKERS,
             inner_increment=LPPLS_INNER_INCREMENT, max_searches=LPPLS_MAX_SEARCHES)

    # Feed EXACTLY `window` closes: the outer loop then has a single position,
    # i.e. one endpoint t2 = today (probed against lppls 0.6.24: len(res) == 1).
    tail = daily_closes[-window:]
    time_idx = np.arange(window, dtype=float)
    # v3.7.8/L-01: prices must be finite and strictly positive before log — a
    # zero/negative/NaN close would otherwise produce -inf/NaN log-prices and a
    # non-finite fit. FLOOR instead (auditable), never a silent bad number.
    arr = np.asarray(tail, dtype=float)
    if arr.size == 0 or not np.all(np.isfinite(arr)) or np.any(arr <= 0.0):
        return _floor(n, "non-finite or non-positive price input")
    price = np.log(arr)
    model = lppls_mod.LPPLS(observations=np.array([time_idx, price]))

    # Step 1: raw nested fits (start times shrink toward t2). filter config is
    # stored on the model here but qualification happens in compute_indicators.
    res = model.mp_compute_nested_fits(
        workers=LPPLS_WORKERS,
        window_size=window,
        smallest_window_size=LPPLS_SMALLEST_WINDOW,
        outer_increment=1,                       # single outer position anyway
        inner_increment=LPPLS_INNER_INCREMENT,
        max_searches=LPPLS_MAX_SEARCHES,
        filter_conditions_config=FILTER_CONDITIONS,
    )
    # Step 2: qualification -> per-endpoint pos_conf; the `_fits` column carries
    # the per-window is_qualified flags this module partitions into dt bands.
    indicators = model.compute_indicators(res, filter_conditions_config=FILTER_CONDITIONS)
    if len(indicators) == 0:
        return _floor(n, "compute_indicators returned no rows")
    row = indicators.iloc[-1]
    fits = list(row["_fits"]) if "_fits" in indicators.columns else \
        list((res[0] or {}).get("res") or [])
    n_eval = len(fits)                       # total fit windows -> drives quality
    if n_eval == 0:
        return _floor(n, "no fit windows produced")
    raw_value = float(row["pos_conf"])
    if not math.isfinite(raw_value):    # v3.7.8/L-01: a non-finite pos_conf FLOORs
        return _floor(n, f"non-finite pos_conf: {raw_value!r}")
    value = max(0.0, min(1.0, raw_value))
    bands = band_fractions(fits)
    # Report the counts on the SAME denominator as `value` (positive-bubble
    # windows, b < 0), so value == n_windows_qualifying / n_windows_positive
    # exactly — no repeat of the "6/40 beside 0.0" inconsistency.
    pq = _positive_qualified(fits)
    schema_note = None
    if pq is not None:
        n_qual, n_pos = pq
        # v3.7.8/L-01: the reported value MUST equal qualifying/positive on the
        # same denominator (that is the whole point of computing them together).
        # A mismatch means the schema shifted under us -> FLOOR, do not publish a
        # value inconsistent with its own audit counts.
        expected = 0.0 if n_pos == 0 else n_qual / n_pos
        if abs(value - expected) > 5e-4:
            return _floor(n, f"pos_conf/count mismatch: value={value}, expected={expected}")
    else:
        # Schema surprise (v3.7.4/L-08): report the counters as UNKNOWN rather
        # than fabricating n_pos=n_eval / n_qual=round(value*n_eval). The value
        # (library pos_conf) is still valid; only the audit counts are lost.
        n_pos = n_qual = None
        schema_note = "window counts unavailable (unexpected lppls fit schema)"
    out = {"state": "VALID_ZERO" if value == 0.0 else "VALID",
           "value": value, "quality": _quality(n_eval),
           "n_windows_evaluated": n_eval, "n_windows_positive": n_pos,
           "n_windows_qualifying": n_qual, "n_closes": n,
           "window": window, "bands": bands}
    if schema_note:
        out["note"] = schema_note
    return out


def compute_confidence_isolated(daily_closes: list[float], timeout_s: int = 1800) -> dict:
    """Run the LPPLS fit in a SUBPROCESS and return the state-dict result.

    Isolation exists because the lppls dependency stack (scipy, scikit-learn,
    numba) ships native wheels that can die with SIGILL on CPUs below their
    build baseline (e.g. pre-SSE4.2 Atoms) — an uncatchable in-process crash.
    A crash or timeout maps to state="FLOOR" (quality 0.0): the caller keeps
    the payload row visible but EXCLUDES d4 from the aggregation and
    renormalizes Block D — an uncomputed indicator must never masquerade as a
    confident zero (v3.3.2 floor semantics; epistemic guardrail #5).
    """
    import json
    import subprocess
    import sys

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.indicators.d4_lppls"],
            input=json.dumps({"closes": daily_closes}),
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return _floor(len(daily_closes), f"subprocess timed out after {timeout_s}s")
    if proc.returncode != 0:
        return _floor(len(daily_closes),
                      f"subprocess rc={proc.returncode}: {proc.stderr.strip()[-200:]}")
    # lppls prints fit exceptions to stdout (lppls.py:960) from forked workers,
    # so stdout is NOT pure JSON. The result is framed with a sentinel and is
    # the last line the parent writes (after the Pool joins) — parse that line.
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(_RESULT_SENTINEL):
            return json.loads(line[len(_RESULT_SENTINEL):])
    return _floor(len(daily_closes), f"no result line: {proc.stdout.strip()[-200:]}")


if __name__ == "__main__":
    import json as _json
    import sys as _sys

    _payload = _json.loads(_sys.stdin.read())
    # Route all fit-time output (tqdm, lppls' print(e) in parent AND forked
    # workers) to stderr so stdout carries only the framed result line.
    _real_stdout = _sys.stdout
    _sys.stdout = _sys.stderr
    try:
        _result = compute_confidence(_payload["closes"])
    except Exception as _exc:  # any in-process fit exception -> auditable FLOOR
        _result = _floor(len(_payload["closes"]), repr(_exc))
    finally:
        _sys.stdout = _real_stdout
    _sys.stdout.write(f"{_RESULT_SENTINEL}{_json.dumps(_result)}\n")
    _sys.stdout.flush()
