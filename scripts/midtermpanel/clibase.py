"""Shared entry-point plumbing: typed exits, job outputs, sanitized failure.

## Why the CLIs share this rather than each doing it

Four entry points run inside a workflow that holds a provider key. Every one of
them must fail the same way: a machine-readable code, a sanitized reason, a
deterministic exit status, and NO traceback. A traceback from a job holding a
credential can print an `Authorization` header out of a chained exception — the
defect `trustedlane.errors.refuse` documents and detaches with `from None`.

Four copies of that discipline would be four chances to get it wrong once. The
one that got it wrong would be the one that ran on the failure path nobody
exercised, which is the path where the key is most likely to be in scope.

## Exit codes

    0   the step did its job
    2   a typed refusal — the panel decided not to proceed, and said why
    3   an unexpected exception — a bug, reported without its traceback

2 and 3 are distinct on purpose. A refusal is the system working; an unexpected
exception is the system being wrong about itself, and an operator triaging a red
run needs to know which one they are looking at before reading anything else.
"""

from __future__ import annotations

import os
import sys

from trustedlane.errors import LaneRefusal

from .errors import PanelRefusal

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNEXPECTED = 3

#: The category a trusted-lane refusal is reported under.
#:
#: One category for the whole class, with the lane's own code and reason carried
#: as fields beside it. The alternative — promoting the inner reason to the
#: top-level category — would let the trusted lane define mid-term categories,
#: and an operator grepping for a mid-term category would be reading a string
#: chosen by a different package.
TRUSTED_LANE_CATEGORY = "trusted_lane_refusal"

#: Longer than any reason `trustedlane.errors.refuse` composes, short enough
#: that an unbounded string cannot be laundered through this line into the log.
MAX_TRUSTED_REASON_CHARS = 1024

#: Shape a lane code may take. `refuse()` defaults to `TRUSTED_LANE_REFUSED`;
#: anything that is not a plain identifier is not a code we will echo.
_CODE_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "abcdefghijklmnopqrstuvwxyz0123456789_.:-")
UNPRINTABLE_CODE = "UNPRINTABLE_CODE"

#: Substrings that must never reach stdout, stderr or the job summary.
#:
#: A denylist is a weak control and this one is deliberately a last line rather
#: than the first: the real protection is that `refuse()` composes messages from
#: fixed strings and never interpolates provider or server text. This catches the
#: case where that discipline slipped, and it is asserted by test over real
#: refusal output rather than assumed.
NEVER_PRINT = ("Bearer ", "Authorization", "Traceback (most recent call last)",
               "sk-", "ghp_", "github_pat_")


def _printable_code(code) -> str:
    """A lane code we are willing to echo, or a fixed stand-in.

    The code is echoed unquoted so an operator can grep for it, which means a
    code carrying a newline would forge a second log line. Constrained to a
    plain identifier rather than sanitized in place: there is no legitimate
    code outside that shape, so anything else is a defect worth showing as
    one."""
    text = code if isinstance(code, str) else ""
    if not text or len(text) > 64 or not set(text) <= _CODE_CHARS:
        return UNPRINTABLE_CODE
    return text


def sanitized_trusted_reason(reason) -> tuple:
    """`(text, ok)` — whether a lane reason may be echoed verbatim.

    `trustedlane.errors.refuse` composes reasons from fixed strings and is
    intended to be sanitized already. This is the final output guard anyway,
    because "intended to be" is the assumption every leak in this repository
    has been made of, and because `LaneRefusal` is an ordinary class that any
    code — including code reached through a provider response — can construct
    directly with an arbitrary reason.

    Failing closed loses the diagnostic, which is the whole point of the
    change. That trade is deliberate: a withheld reason costs one more run to
    diagnose, and a leaked `Authorization` header costs a credential."""
    text = reason if isinstance(reason, str) else ""
    if not text:
        return ("", False)
    if len(text) > MAX_TRUSTED_REASON_CHARS:
        return ("", False)
    # Control characters, not just newline: a carriage return rewrites the line
    # in a terminal and an escape sequence can hide the rest of it.
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return ("", False)
    lowered = text.lower()
    if any(token.lower() in lowered for token in NEVER_PRINT):
        return ("", False)
    return (text, True)


def trusted_refusal_line(refusal) -> str:
    """The one line a `LaneRefusal` is allowed to print.

    Reports `category=trusted_lane_refusal` and carries the lane's own code and
    reason as fields. Deliberately NOT a `TRUSTED_*` evidence claim: this is a
    mid-term single-repository run, and the trusted lane is the source of the
    inner refusal, not a lane this run is entitled to claim it executed."""
    code = _printable_code(getattr(refusal, "code", None))
    reason, ok = sanitized_trusted_reason(getattr(refusal, "reason", None))
    head = (f"MIDTERM_PANEL_REFUSED: category={TRUSTED_LANE_CATEGORY} "
            f"trusted_code={code}")
    return f"{head} trusted_reason={reason}" if ok else f"{head} reason_redacted=true"


def emit_outputs(outputs: dict, *, stream=None) -> str:
    """Write job outputs to `$GITHUB_OUTPUT`, or to a stream when testing.

    Uses the heredoc form for every value. A bare `name=value` breaks on any
    value containing a newline, and "it worked until a description contained a
    line break" is a failure that shows up first in production."""
    lines = []
    for key in sorted(outputs):
        value = outputs[key]
        if isinstance(value, bool):
            value = "true" if value else "false"
        value = "" if value is None else str(value)
        delimiter = f"__MIDTERM_{key.upper()}__"
        if delimiter in value:
            raise ValueError(f"output {key} contains its own delimiter")
        lines.append(f"{key}<<{delimiter}\n{value}\n{delimiter}")
    blob = "\n".join(lines) + ("\n" if lines else "")
    target = stream if stream is not None else os.environ.get("GITHUB_OUTPUT")
    if target is None:
        # Not running under Actions. Printing them keeps a local run readable
        # instead of silently discarding the only result the step produces.
        sys.stdout.write(blob)
    elif hasattr(target, "write"):
        target.write(blob)
    else:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(blob)
    return blob


def summary(text: str) -> None:
    """Append to the job summary, or stdout when not under Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")
    else:
        sys.stdout.write(text.rstrip() + "\n")


def require_env(environ: dict, names, *, where: str) -> dict:
    """Every named variable must be present and non-blank.

    Blank is refused as well as missing. An unset workflow expression renders as
    the empty string rather than failing, so `env: X: ${{ steps.a.outputs.b }}`
    where `b` was never set arrives here as `""` — and a check that only tested
    for presence would proceed with an empty candidate SHA."""
    missing = [n for n in names if not str(environ.get(n) or "").strip()]
    if missing:
        raise PanelRefusal(
            "MIDTERM_PANEL_REFUSED",
            f"category=required_environment_absent where={where} "
            f"variables={sorted(missing)} — an unset workflow expression "
            "renders as an empty string rather than failing, so absence and "
            "blankness are the same defect here")
    return {n: str(environ[n]).strip() for n in names}


def run(main_callable, *, name: str) -> int:
    """Invoke an entry point, converting every outcome into a typed exit.

    The bare `except Exception` is deliberate and is the reason this wrapper
    exists. Letting an unexpected exception propagate prints a traceback, and a
    traceback from a credential-bearing job can carry the credential. The type
    name is reported; the message is not, because an arbitrary exception's
    message is arbitrary text from wherever it came from."""
    try:
        main_callable()
    except PanelRefusal as refusal:
        sys.stderr.write(f"{refusal.code}: {refusal.reason}\n")
        return EXIT_REFUSED
    except LaneRefusal as refusal:
        # BEFORE the generic catch, and a separate branch rather than a wider
        # `except (PanelRefusal, LaneRefusal)`: the two classes mean different
        # things. A PanelRefusal is this package deciding; a LaneRefusal is the
        # trusted lane deciding, and the evidence has to keep saying which.
        #
        # Without this branch a lane refusal fell through to `except Exception`
        # and printed `exception_class=LaneRefusal` with the reason withheld —
        # which is what a decision the lane took deliberately looked like on
        # panel run 31608202983, indistinguishable from a crash.
        sys.stderr.write(f"{trusted_refusal_line(refusal)}\n")
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - see docstring
        sys.stderr.write(
            f"MIDTERM_PANEL_UNEXPECTED: entry_point={name} "
            f"exception_class={type(exc).__name__} — the message and traceback "
            "are withheld; this job may hold a provider key\n")
        return EXIT_UNEXPECTED
    return EXIT_OK


def self_test_requested(argv) -> bool:
    return "--self-test" in (argv or [])


def self_test_report(name: str, checks: dict) -> int:
    """`--self-test` proves the entry point is importable and wired.

    Deliberately does NOT touch the network, the provider, or any credential.
    Its whole job is to answer "would `python -m midtermpanel.<x>` have worked",
    which is the question the packaging fix in this branch exists for and the one
    a hosted dry-run needs answered before it spends anything."""
    failures = sorted(k for k, ok in checks.items() if not ok)
    for key in sorted(checks):
        sys.stdout.write(f"{'ok  ' if checks[key] else 'FAIL'} {name}: {key}\n")
    if failures:
        sys.stderr.write(f"MIDTERM_PANEL_SELF_TEST_FAILED entry_point={name} "
                         f"checks={failures}\n")
        return EXIT_REFUSED
    sys.stdout.write(f"MIDTERM_PANEL_SELF_TEST_OK entry_point={name}\n")
    return EXIT_OK
