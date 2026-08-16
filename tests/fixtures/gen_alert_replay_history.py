"""Generate `tests/fixtures/alert_replay_history.json`.

    python -m tests.fixtures.gen_alert_replay_history

The output is COMMITTED, and `docs/alert-stage1-gate.json` is derived from it,
so regenerating changes the gate evidence. The arc below is written out slot by
slot rather than sampled or randomized: a fixture that could differ between two
runs would make the determinism gate meaningless.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.alerts.canonical import sha256_of
from tests.test_alert_evaluation import make_input

TARGET = Path(__file__).resolve().parent / "alert_replay_history.json"

SLOTS = (2, 6, 10, 14, 18, 22)

# (day, slot_hour, effective, base, median, degraded, rf4, breadth)
SCRIPT = [
    # --- a quiet stretch in hold -------------------------------------------
    (10, 2,  "hold", "hold", 44.0, False, False, 58.0),
    (10, 6,  "hold", "hold", 44.5, False, False, 57.5),
    (10, 10, "hold", "hold", 45.0, False, False, 57.0),
    # --- drift into trim ----------------------------------------------------
    (10, 14, "trim", "trim", 52.0, False, False, 56.0),
    (10, 18, "trim", "trim", 53.0, False, False, 55.0),
    (10, 22, "trim", "trim", 54.0, False, False, 54.0),
    # --- a blind slot: coverage degraded, band suppressed -------------------
    (11, 2,  None, None, None, True, None, None),
    # --- back to trim, then a de-risk entry that PERSISTS -------------------
    (11, 6,  "trim", "trim", 55.0, False, False, 53.0),
    (11, 10, "de-risk", "de-risk", 63.0, False, True, 47.0),
    (11, 14, "de-risk", "de-risk", 64.0, False, True, 46.5),
    (11, 18, "de-risk", "de-risk", 64.5, False, True, 46.0),
    # --- an UNKNOWN in the middle of a firing episode ----------------------
    (11, 22, None, None, None, True, None, None),
    # --- still de-risk afterwards: UNKNOWN resolved nothing ----------------
    (12, 2,  "de-risk", "de-risk", 63.5, False, True, 46.0),
    # --- recovery back through trim to hold --------------------------------
    (12, 6,  "trim", "trim", 54.0, False, False, 52.0),
    (12, 10, "trim", "trim", 51.0, False, False, 55.0),
    (12, 14, "hold", "hold", 46.0, False, False, 57.0),
    (12, 18, "hold", "hold", 44.0, False, False, 58.0),
    # --- a single-snapshot de-risk excursion (the transient the mandate
    #     asks replay to COUNT rather than assume away) ----------------------
    (12, 22, "de-risk", "de-risk", 62.0, False, True, 48.0),
    (13, 2,  "hold", "hold", 45.0, False, False, 57.0),
    (13, 6,  "hold", "hold", 44.0, False, False, 58.0),
]

def build() -> dict:
    records = []
    for day, hour, effective, base, median, degraded, rf4, breadth in SCRIPT:
        stamp = f"2026-07-{day:02d}T{hour:02d}:00:00+00:00"
        record = make_input(
            identity=f"replay-{day:02d}{hour:02d}",
            computed_at=stamp,
            effective=effective,
            base=base,
            median=median,
            point=median,
            iqr=(median - 2.0, median + 3.0) if median is not None else None,
            degraded=degraded,
            suppressed=degraded,
            rf4=rf4,
            rf4_fireable=rf4 is not None,
            rf4_period=f"2026-07-{day:02d}",
            breadth=breadth,
            breadth_period=f"2026-07-{day:02d}",
            faber="out" if effective == "de-risk" else ("in" if effective else None),
        )
        records.append(record.model_dump(mode="json"))

    return {
        "note": [
            "SYNTHETIC replay history for the Stage 1 gate artifact.",
            "Not market data of record, not a forecast, and not derived from any",
            "person. It exists to exercise the evaluator's state machine across a",
            "hold -> trim -> de-risk -> recovery arc, two UNKNOWN slots inside a",
            "firing episode, and one transient single-snapshot de-risk excursion.",
            "Regenerate deliberately: changing it changes the committed evidence.",
        ],
        "schema_version": 1,
        "slots_utc": list(SLOTS),
        "sha256_of_inputs": sha256_of(records),
        "inputs": records,
    }


def main() -> int:
    payload = build()
    TARGET.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"wrote {TARGET} — {len(payload['inputs'])} inputs, "
          f"sha256 {payload['sha256_of_inputs'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
