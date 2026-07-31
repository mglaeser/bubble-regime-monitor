"""The process boundary for a credential-bearing step: no traceback escapes.

Adversarial review of the D1/D2 lanes found that `errors.refuse` used a bare
`raise`, so a refusal raised from inside an `except` block carried the original
exception as `__context__` and Python printed the whole chain. The refusal
message said "the path is not reported"; the line above it printed the path.
In `transport._send` the chained exception can be a `ValueError` whose text is
the entire `Authorization` header.

`refuse` now raises `from None`, which removes the chain. That is not enough on
its own. An uncaught `LaneRefusal` still prints its own traceback — file names,
line numbers, and the source line of every frame — and the repository already
treats that as a defect: `tests/test_probe_error_redaction.py` forbids
`Traceback` from reaching stdout, stderr or the job summary of the
credential-bearing probe. A lane that holds the same key must meet the same bar.

So the workflow steps call `main()` here rather than the runtime directly. This
module catches everything, prints one sanitized line, and exits non-zero:

    LaneRefusal      -> the code and the sanitized reason, exit 2
    anything else    -> the exception CLASS ONLY, exit 3

The second case matters as much as the first. A `TypeError` from a malformed
operator-records document, or an `OSError` from a runner disk, is not a
`LaneRefusal` — and letting it propagate would print a traceback whose frames
include local variables' source lines from a function holding a credential.
Reporting the class and nothing else keeps a genuine crash diagnosable without
publishing what it was carrying.
"""

from __future__ import annotations

import sys

from .errors import LaneRefusal

EXIT_REFUSED = 2
EXIT_CRASHED = 3


def run_lane(entrypoint, *, stream=None) -> int:
    """Call `entrypoint()` and let nothing untrusted out of this process.

    Returns an exit code rather than calling `sys.exit`, so the behaviour is
    testable — a function that exits cannot be asserted about without a
    subprocess, and this one has to be."""
    out = stream if stream is not None else sys.stdout
    try:
        result = entrypoint()
    except LaneRefusal as refusal:
        # `reason` is built entirely from repository-chosen strings and
        # validated scalars. It is the one piece of text safe to print.
        print(f"{refusal.code}: {refusal.reason}", file=out)
        return EXIT_REFUSED
    except BaseException as exc:  # noqa: BLE001 — the class, never the text
        # NOT `Exception`: a KeyboardInterrupt or SystemExit propagating out of
        # a credential-bearing step would also print a traceback.
        print(f"TRUSTED_LANE_CRASHED: exception_class={type(exc).__name__} — "
              "the exception text is deliberately not printed; it can carry a "
              "path, a header or a response fragment, and this process holds a "
              "provider key", file=out)
        return EXIT_CRASHED
    _print_payload(result, out)
    return 0


def _print_payload(result, out) -> None:
    """Print only the payload, and only if it is the shape we expect.

    A lane result carries `evidence` and `payload`; the evidence holds a
    signature and the payload holds counts. Printing the whole result would put
    the signature in a log for no reason, so only the payload goes out."""
    import json

    if not isinstance(result, dict) or "payload" not in result:
        print("TRUSTED_LANE_RESULT_UNRECOGNISED", file=out)
        return
    try:
        print(json.dumps(result["payload"], sort_keys=True), file=out)
    except (TypeError, ValueError) as exc:
        print(f"TRUSTED_LANE_PAYLOAD_UNSERIALISABLE "
              f"exception_class={type(exc).__name__}", file=out)


def main(phase: str) -> int:
    """Entry point the workflow templates call. `phase` selects the lane."""
    if phase == "D1":
        from .d1cli import main as lane
    elif phase == "D2":
        from .d2cli import main as lane
    else:
        print(f"TRUSTED_LANE_REFUSED: category=unknown_lane phase={phase!r}",
              file=sys.stdout)
        return EXIT_REFUSED
    return run_lane(lane)


if __name__ == "__main__":  # pragma: no cover - exercised via run_lane
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
