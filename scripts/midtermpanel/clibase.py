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

from .errors import PanelRefusal

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNEXPECTED = 3

#: Substrings that must never reach stdout, stderr or the job summary.
#:
#: A denylist is a weak control and this one is deliberately a last line rather
#: than the first: the real protection is that `refuse()` composes messages from
#: fixed strings and never interpolates provider or server text. This catches the
#: case where that discipline slipped, and it is asserted by test over real
#: refusal output rather than assumed.
NEVER_PRINT = ("Bearer ", "Authorization", "Traceback (most recent call last)",
               "sk-", "ghp_", "github_pat_")


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
