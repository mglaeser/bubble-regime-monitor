"""Rscript subprocess bridge for the S4 GSADF indicator.

Contract (r/gsadf.R, JSON on stdin/stdout):
    stdin:  {"series": [monthly log prices...]}
    stdout: {"gsadf": <stat>, "cv90": <90% CV>, "cv95": <95% CV>}

If R/Rscript is unavailable or the script fails, return None so the S4
sub-score falls back to 0.05 with a provenance note (never crash).
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
TIMEOUT_S = 600


@dataclass
class GsadfOutput:
    gsadf: float
    cv90: float
    cv95: float


def run(monthly_log_prices: list[float]) -> GsadfOutput | None:
    if shutil.which("Rscript") is None:
        log.warning("gsadf_rscript_unavailable")
        return None
    try:
        proc = subprocess.run(
            ["Rscript", str(R_SCRIPT)],
            input=json.dumps({"series": monthly_log_prices}),
            capture_output=True, text=True, timeout=TIMEOUT_S, check=True,
        )
        out = json.loads(proc.stdout.strip())
        return GsadfOutput(gsadf=float(out["gsadf"]), cv90=float(out["cv90"]),
                           cv95=float(out["cv95"]))
    except Exception as exc:
        log.warning("gsadf_run_failed", error=str(exc))
        return None
