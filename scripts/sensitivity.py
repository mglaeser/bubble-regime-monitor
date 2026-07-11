"""Annual PSS sensitivity script (Paruolo-Saisana-Saltelli 2013).

Estimates each indicator's FIRST-ORDER MAIN EFFECT — the Pearson correlation
ratio, i.e. the fraction of headline variance explained by conditioning on
that indicator — non-parametrically (binned conditional-mean regression of
the MC score on each sub-score across draws), compares each normalized main
effect to the indicator's nominal weight, and FLAGS any
|nominal - effective| > 0.10.

Emits a dated report to /data/snapshots and prints a governance warning.
Run annually and after any weight change. If any flag trips, open a
governance review before shipping new weights (do not silently retune).

Usage: python scripts/sensitivity.py [--samples 100000] [--seed 20260711]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.aggregate import EPSILON, renormalize  # noqa: E402
from app.engine.montecarlo import (  # noqa: E402
    ALPHA_RANGE,
    BASE_WEIGHTS_D,
    BASE_WEIGHTS_S,
    DIRICHLET_CONCENTRATION,
)

# Golden-fixture raw inputs (replace with live readings for a production run).
FIXTURE = {
    "s1": 0.92, "top10": 36.4, "runup": 108.0, "s4": 0.25, "s5": 0.80,
    "breadth": 56.0, "d2": 0.49, "d3": 0.30, "d4": 0.005, "V": 1.0,
}

FLAG_THRESHOLD = 0.10


def simulate(n: int, seed: int) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Draw sub-scores + weights per the section 5.3 MC and return
    (per-indicator sub-score draws, final scores)."""
    rng = np.random.default_rng(seed)
    subs: dict[str, np.ndarray] = {}
    subs["s1"] = np.full(n, FIXTURE["s1"])
    lo = rng.uniform(16, 20, n)
    hi = rng.uniform(38, 44, n)
    subs["s2"] = np.clip((FIXTURE["top10"] - lo) / (hi - lo), 0, 1)
    subs["s3"] = rng.beta(21, 19, n)
    subs["s4"] = np.full(n, FIXTURE["s4"])
    subs["s5"] = np.full(n, FIXTURE["s5"])
    lo2 = rng.uniform(35, 45, n)
    hi2 = rng.uniform(70, 80, n)
    subs["d1"] = np.clip((hi2 - FIXTURE["breadth"]) / (hi2 - lo2), 0, 1)
    subs["d2"] = np.full(n, FIXTURE["d2"])
    subs["d3"] = np.full(n, FIXTURE["d3"])
    subs["d4"] = np.full(n, FIXTURE["d4"])

    s_ids = list(BASE_WEIGHTS_S)
    d_ids = list(BASE_WEIGHTS_D)
    wS = rng.dirichlet(np.array([BASE_WEIGHTS_S[k] for k in s_ids]) * DIRICHLET_CONCENTRATION, size=n)
    wD = rng.dirichlet(np.array([BASE_WEIGHTS_D[k] for k in d_ids]) * DIRICHLET_CONCENTRATION, size=n)
    S = np.exp(np.sum(wS * np.log(np.column_stack([subs[k] for k in s_ids]) + EPSILON), axis=1)) - EPSILON
    D = np.exp(np.sum(wD * np.log(np.column_stack([subs[k] for k in d_ids]) + EPSILON), axis=1)) - EPSILON
    D = np.minimum(D * FIXTURE["V"], 1.0)
    a = rng.uniform(*ALPHA_RANGE, n)
    scores = 100.0 * np.power(np.maximum(S, 0), a) * np.power(np.maximum(D, 0), 1 - a)
    return subs, scores


def main_effect(x: np.ndarray, y: np.ndarray, bins: int = 50) -> float:
    """Correlation ratio eta^2 = Var(E[y|x]) / Var(y), binned non-parametric."""
    if np.ptp(x) == 0.0:
        return 0.0
    edges = np.quantile(x, np.linspace(0, 1, bins + 1))
    idx = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, bins - 1)
    total_var = float(np.var(y))
    if total_var == 0.0:
        return 0.0
    cond_means = np.zeros(bins)
    counts = np.zeros(bins)
    np.add.at(cond_means, idx, y)
    np.add.at(counts, idx, 1)
    mask = counts > 0
    cond_means[mask] /= counts[mask]
    between = float(np.sum(counts[mask] * (cond_means[mask] - y.mean()) ** 2) / len(y))
    return between / total_var


def run(n: int, seed: int) -> dict:
    subs, scores = simulate(n, seed)
    nominal = {**{k: v for k, v in renormalize(BASE_WEIGHTS_S, set(BASE_WEIGHTS_S)).items()},
               **{k: v for k, v in renormalize(BASE_WEIGHTS_D, set(BASE_WEIGHTS_D)).items()}}
    effects = {k: main_effect(subs[k], scores) for k in subs}
    total = sum(effects.values()) or 1.0
    normalized = {k: v / total for k, v in effects.items()}
    flags = {k: abs(nominal[k] - normalized[k]) > FLAG_THRESHOLD for k in effects}
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "n": n, "seed": seed,
        "note": ("Indicators pinned to constants in this fixture run have zero variance and "
                 "hence zero estimable main effect; their nominal-vs-effective gap is expected "
                 "and reflects the fixture, not the framework."),
        "nominal_weights": nominal,
        "first_order_main_effects": effects,
        "normalized_effective_weights": normalized,
        "flags_gt_0_10": flags,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=20260711)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    report = run(args.samples, args.seed)
    out_dir = args.out or Path("data/snapshots")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    path = out_dir / f"sensitivity-{stamp}.json"
    path.write_text(json.dumps(report, indent=2))
    print(f"report written: {path}")
    tripped = [k for k, v in report["flags_gt_0_10"].items() if v]
    if tripped:
        print("GOVERNANCE WARNING: |nominal - effective| > 0.10 for: " + ", ".join(tripped))
        print("Open a governance review before shipping new weights (do not silently retune).")
