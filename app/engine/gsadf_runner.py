"""Rscript subprocess bridge for the S4 GSADF indicator.

Contract (r/gsadf.R, JSON on stdin/stdout):
    stdin:  {"series": [monthly log prices...]}
    stdout: {"gsadf": <sup stat>, "cv90": <90% CV>, "cv95": <95% CV>,
             "bsadf": <endpoint stat>, "bsadf_cv90": .., "bsadf_cv95": ..,
             "bsadf_n": <sequence length>, "bsadf_argmax": <1-based argmax>,
             "bsadf_n_finite": <how many of the sequence are finite>}
    The bsadf_* fields may be null when exuber returns no usable sequence or the
    simulated CV matrix is not row-aligned with it; they are typed Optional and a
    None flows to the contested/stale floor rather than to a comparison.

If R/Rscript is unavailable or the script fails, return None so the S4
sub-score falls back to the contested/stale floor 0.25 with a provenance
note (never crash). The 0.05 value is reserved for a SUCCESSFULLY executed
test that finds no explosiveness (see s4_gsadf.sub_score).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.logging_conf import get_logger

log = get_logger(__name__)

R_SCRIPT = Path(__file__).resolve().parents[2] / "r" / "gsadf.R"


@dataclass
class GsadfOutput:
    gsadf: float
    cv90: float
    cv95: float
    # Endpoint BSADF + the endpoint row of the simulated BSADF CVs. Optional so a
    # partial R response degrades to the s4 floor instead of raising (guardrail 5).
    bsadf: float | None = None
    bsadf_cv90: float | None = None
    bsadf_cv95: float | None = None
    bsadf_n: int | None = None
    bsadf_argmax: int | None = None
    # How much of the BSADF history was computable. bsadf_argmax is None when
    # this is below bsadf_n: the sup cannot be honestly date-stamped over a
    # sequence with holes, though the ENDPOINT may still be perfectly valid.
    bsadf_n_finite: int | None = None


def run(monthly_log_prices: list[float], timeout_s: int | None = None) -> GsadfOutput | None:
    if shutil.which("Rscript") is None:
        log.warning("gsadf_rscript_unavailable")
        return None
    if timeout_s is None:
        from app.config import get_settings

        timeout_s = get_settings().gsadf_timeout_s
    # F-01/L-07: the GSADF constants are loaded from the canonical frozen artifact
    # and passed to R via the request, so r/gsadf.R does not independently hardcode
    # them (the R script still keeps the same values as a defensive fallback).
    from app import methodology as _M
    params = {
        "lag": _M.get_path("gsadf", "lag"),
        "mc_nrep": _M.get_path("gsadf", "mc_nrep"),
        "mc_seed": _M.get_path("gsadf", "mc_seed"),
    }
    try:
        proc = subprocess.run(
            ["Rscript", str(R_SCRIPT)],
            input=json.dumps({"series": monthly_log_prices, "params": params}),
            capture_output=True, text=True, timeout=timeout_s, check=True,
        )
        out = json.loads(proc.stdout.strip())
        def _opt_f(key: str) -> float | None:
            v = out.get(key)
            return None if v is None else float(v)

        def _opt_i(key: str) -> int | None:
            v = out.get(key)
            return None if v is None else int(v)

        return GsadfOutput(gsadf=float(out["gsadf"]), cv90=float(out["cv90"]),
                           cv95=float(out["cv95"]),
                           bsadf=_opt_f("bsadf"), bsadf_cv90=_opt_f("bsadf_cv90"),
                           bsadf_cv95=_opt_f("bsadf_cv95"),
                           bsadf_n=_opt_i("bsadf_n"),
                           bsadf_argmax=_opt_i("bsadf_argmax"),
                           bsadf_n_finite=_opt_i("bsadf_n_finite"))
    except subprocess.CalledProcessError as exc:
        log.warning("gsadf_run_failed", returncode=exc.returncode,
                    stdout=(exc.stdout or "")[-400:], stderr=(exc.stderr or "")[-400:])
        return None
    except subprocess.TimeoutExpired as exc:
        # Surface R's own stderr on a timeout too (was swallowed): distinguishes a
        # genuinely slow fit from e.g. a package-load hang. (v3.3.1)
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        log.warning("gsadf_run_timeout", timeout_s=timeout_s, stderr=err[-400:])
        return None
    except Exception as exc:
        log.warning("gsadf_run_failed", error=str(exc))
        return None


_r_check: dict[str, object] | None = None


def r_selfcheck(force: bool = False) -> dict[str, object]:
    """Cached check that Rscript exists and `library(exuber)` loads.

    Surfaced in /readyz so a missing R runtime is visible as a clear message
    instead of a mysterious S4 floor."""
    global _r_check
    if _r_check is not None and not force:
        return _r_check
    if shutil.which("Rscript") is None:
        _r_check = {"ok": False, "note": "Rscript not on PATH — GSADF floors at 0.25"}
        return _r_check
    try:
        proc = subprocess.run(["Rscript", "-e", "library(exuber)"],
                              capture_output=True, text=True, timeout=180)
        if proc.returncode == 0:
            _r_check = {"ok": True, "note": "Rscript + exuber available"}
        else:
            _r_check = {"ok": False,
                        "note": f"library(exuber) failed: {(proc.stderr or '')[-200:]}"}
    except Exception as exc:
        _r_check = {"ok": False, "note": f"Rscript self-check error: {exc}"}
    log.info("gsadf_r_selfcheck", **_r_check)
    return _r_check
