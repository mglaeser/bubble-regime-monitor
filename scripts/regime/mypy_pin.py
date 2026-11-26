#!/usr/bin/env python3
"""The depth of the type check is pinned: `[tool.mypy]` in pyproject.toml is the
table `.mypy-config.pinned` holds.

The type-check step in .github/workflows/ci.yml counts errors and blocks only a
rise, so a looser configuration lowers the count and passes. This check refuses
that. It compares the two tables as the TOML library parses them (tomllib, which
mypy reads pyproject.toml with), so `[[tool.mypy.overrides]]` and every other
sub-table are part of the comparison. The step then runs
`mypy --config-file pyproject.toml app`, so the table compared is the one read.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = ("pyproject.toml", ".mypy-config.pinned")


def problems(root: Path = ROOT) -> list[str]:
    """The pin passes (an empty list) in exactly one case: both files open and
    parse as TOML, `tool.mypy` is a table in both, and the two tables are equal
    with the order of their keys and the type of each value. Everything else,
    every exception included, is the refusal: one line that names the file,
    which `main` prints as its BLOCK line before it returns exit status 1."""
    name = FILES[0]
    try:
        printed = []
        for name in FILES:
            # Opened in binary and read by `tomllib.load`, as mypy 2.3.1 reads
            # pyproject.toml: the pin decodes no text and translates no line
            # end itself.
            with (root / name).open("rb") as file:
                found = tomllib.load(file)["tool"]["mypy"]
            if not isinstance(found, dict):
                return [f"{name}: tool.mypy is {found!r}, which is no table"]
            printed.append(repr(found))
        actual, pinned = printed
        # The two are compared as printed, so the order of the keys and the
        # type of each value count, in every sub-table. Measured with mypy 2.3.1:
        # the later of two spellings of one option wins (`ignore_errors`,
        # `no_ignore_errors`), and `strict = 1.0`, which Python calls equal to
        # `true`, is "Not a boolean" and leaves strict off.
        if actual != pinned:
            return [f"[tool.mypy] in pyproject.toml is {actual}; .mypy-config.pinned holds {pinned}"]
        return []
    except Exception as exc:  # whatever opening, parsing, extracting or printing raises
        return [f"{name} holds no [tool.mypy] table the pin can compare: {type(exc).__name__}: {exc}"]


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
