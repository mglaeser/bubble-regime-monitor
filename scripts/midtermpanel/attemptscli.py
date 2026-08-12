"""`python -m midtermpanel.attemptscli` — report attempts, whatever else failed.

Runs `if: always()` in the count and panel jobs, after the step that may have
refused. Its only job is to turn the attempt journal into job outputs so that
`finalize` — a different job, on a different runner, with a different
`RUNNER_TEMP` — can state exactly how many provider attempts a run made.

The job boundary is the reason this exists as a step rather than as a read
inside `finalize`. `RUNNER_TEMP` is per-job: the journal the count job wrote is
not on the finalize job's disk at all, so it has to cross as an output.

Holds no credential and opens no socket. It reads one local file and writes
`$GITHUB_OUTPUT`. It exits 0 even when the journal is absent, because this step
running red on a run that already refused would replace the real cause with a
bookkeeping error — the absence is reported in `journal_present` instead.
"""

from __future__ import annotations

import os
import sys

from . import attemptjournal
from .clibase import emit_outputs, run, self_test_report, self_test_requested

#: The job outputs this step produces, named here for the same reason
#: `preflightcli.PUBLIC_OUTPUTS` is: the workflow declares job outputs, and a
#: declaration whose producer stopped emitting it yields the empty string at
#: run time rather than failing. The producer-side test reads this.
PUBLIC_OUTPUTS = ("provider_attempts", "count_attempts", "generation_attempts",
                  "journal_present")


def perform(environ: dict) -> dict:
    temp = str(environ.get("RUNNER_TEMP") or "").strip()
    path = attemptjournal.journal_path(temp) if temp else ""
    counts = attemptjournal.counts(path)
    emit_outputs(counts)
    sys.stdout.write(
        "MIDTERM_PROVIDER_ATTEMPTS: "
        f"provider_attempts={counts['provider_attempts']} "
        f"count_attempts={counts['count_attempts']} "
        f"generation_attempts={counts['generation_attempts']} "
        f"journal_present={str(counts['journal_present']).lower()}\n")
    return counts


def main() -> None:
    perform(dict(os.environ))


def _self_test() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        empty = attemptjournal.counts(attemptjournal.journal_path(temp))
        path = attemptjournal.journal_path(temp)
        attemptjournal.record_attempt(
            path, operation=attemptjournal.COUNT_OPERATION, attempt=1,
            endpoint="/v1/responses/input_tokens", payload=b"{}")
        one = attemptjournal.counts(path)
    return self_test_report("attemptscli", {
        "module imports": True,
        "perform is callable": callable(perform),
        "absent journal reports zero": empty["provider_attempts"] == 0,
        "absent journal says it is absent": not empty["journal_present"],
        "a recorded attempt is counted": one["provider_attempts"] == 1,
        "a count attempt is not a generation attempt":
            one["generation_attempts"] == 0,
    })


if __name__ == "__main__":
    if self_test_requested(sys.argv[1:]):
        raise SystemExit(_self_test())
    raise SystemExit(run(main, name="attemptscli"))
