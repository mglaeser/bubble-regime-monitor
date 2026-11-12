#!/usr/bin/env python3
"""The depth of the type check is pinned: `[tool.mypy]` in pyproject.toml is the
table `.mypy-config.pinned` holds.

The type-check step in .github/workflows/ci.yml counts errors and blocks only a
rise, so a looser configuration lowers the count and passes. This check refuses
that. It compares the two tables as the TOML library parses them (tomllib, which
mypy reads pyproject.toml with), so `[[tool.mypy.overrides]]` and every other
sub-table are part of the comparison. The step then runs
`mypy --config-file pyproject.toml app`, so the table compared is the one read.

Measured on the step this replaces (a regular expression that cut the section
at the first `[`, then `mypy app`; mypy 2.3.1, 126 source files, ceiling 116):

    as committed                                             116 errors  passed
    [[tool.mypy.overrides]] module = ["app.*"],
      ignore_errors = true appended to pyproject.toml          0 errors  passed
    a mypy.ini without strict, pyproject.toml untouched       33 errors  passed

A file that is missing or does not parse raises, and the step fails: a refusal,
never a pass. Outside this pin, as `.mypy-config.pinned` states: a per-file
`# mypy:` line and `# type: ignore` comments, which are in the diff of the file
they loosen.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def table(text: str) -> dict[str, Any] | None:
    """The `[tool.mypy]` table of a TOML document, sub-tables included."""
    return tomllib.loads(text).get("tool", {}).get("mypy")


def problems(root: Path = ROOT) -> list[str]:
    actual = table((root / "pyproject.toml").read_text(encoding="utf-8"))
    pinned = table((root / ".mypy-config.pinned").read_text(encoding="utf-8"))
    if actual is None or actual != pinned:
        return [f"[tool.mypy] in pyproject.toml is {actual!r}; .mypy-config.pinned holds {pinned!r}"]
    return []


def main(root: Path = ROOT) -> int:
    found = problems(root)
    for problem in found:
        print(f"BLOCK mypy depth pin: {problem}", file=sys.stderr)
    if found:
        print("The type-check step counts errors; it cannot see strictness. Re-derive "
              "MYPY_CEILING under the new configuration and update .mypy-config.pinned "
              "in the same commit.", file=sys.stderr)
        return 1
    print("mypy depth pin: [tool.mypy] in pyproject.toml is the pinned table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
