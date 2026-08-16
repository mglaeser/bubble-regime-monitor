"""Build the synthetic replay history from its committed arc.

`alert_replay_history.json` holds the ARC — twenty recompute slots describing a
hold → trim → de-risk → recovery sequence, with two blind slots and one
transient excursion. This module turns those rows into `AlertInput` objects.

Deriving rather than committing the serialized inputs is the point. An
`AlertInput` carries observation keys, revision keys and computation
fingerprints, and every one of them is *computed* from the values in the arc
rather than chosen. Committing the derivation would trade twenty reviewable
rows for two thousand lines of content hashes that no reviewer can check and
that a secret scanner cannot distinguish from credentials.

The builder is the same `make_input` the evaluation tests use, on purpose: one
builder means a change to the input shape shows up in the gate artifact as a
reviewable diff instead of leaving the evidence describing an input the system
no longer produces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.alerts.canonical import sha256_of
from app.alerts.dto import AlertInput

SOURCE = Path(__file__).resolve().parent / "alert_replay_history.json"


def document() -> dict[str, Any]:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def load() -> list[AlertInput]:
    """The arc, as evaluable inputs, oldest first."""
    from tests.test_alert_evaluation import make_input

    payload = document()
    month = payload["month"]
    records: list[AlertInput] = []
    for slot in payload["slots"]:
        day, hour = slot["day"], slot["hour"]
        effective = slot.get("effective")
        median = slot.get("median")
        breadth = slot.get("breadth")
        degraded = bool(slot.get("degraded", False))
        # A blind slot has no red-flag reading at all — `None` is UNAVAILABLE
        # here, which is not the same as `False`.
        rf4 = slot.get("rf4", None if degraded else False)
        period = f"{month}-{day:02d}"
        stamp = f"{month}-{day:02d}T{hour:02d}:00:00+00:00"
        records.append(make_input(
            identity=f"replay-{day:02d}{hour:02d}",
            computed_at=stamp,
            effective=effective,
            base=effective,
            median=median,
            point=median,
            iqr=(median - 2.0, median + 3.0) if median is not None else None,
            degraded=degraded,
            suppressed=degraded,
            rf4=rf4,
            rf4_fireable=rf4 is not None,
            rf4_period=period,
            breadth=breadth,
            breadth_period=period,
            faber="out" if effective == "de-risk" else ("in" if effective else None),
        ))
    return records


def digest() -> str:
    """Content identity of the ARC — what a gate artifact should cite.

    Taken over the committed rows rather than over the built inputs: the arc is
    what a person edits, and it is the thing whose change should be visible.
    """
    payload = document()
    return sha256_of({"schema_version": payload["schema_version"],
                      "month": payload["month"], "slots": payload["slots"]})
