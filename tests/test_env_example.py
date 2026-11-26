"""A comment never shares a line with a key: not in the template, and not in
the README or docs/ALERT_SYSTEM.md, whose configuration lines an operator
pastes into the live file.

In a container - compose's `env_file`, the Quadlet's `EnvironmentFile=` -
podman's `--env-file` reads the `.env`, and that reader takes everything after
the first "=" as the value, an inline comment included.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / ".env.example", ROOT / "README.md", ROOT / "docs" / "ALERT_SYSTEM.md"]

#: A key line: after leading whitespace, one optional Markdown blockquote or
#: list marker, an optional "#" and an optional "export ", `NAME=`.
_KEY_LINE = re.compile(r"^\s*(?:>\s*|(?:[-*+]|\d+[.)])\s+)?#?(?:export\s+)?[A-Z][A-Z0-9_]*=")
#: The same line, carrying a comment after its value.
_COMMENT_ON_A_KEY_LINE = re.compile(_KEY_LINE.pattern + r".*\s#")


def test_no_key_line_carries_a_comment_in_the_template_or_the_documents():
    """Every line of the three files that, after leading whitespace, one
    optional Markdown blockquote or list marker, an optional "#" and an
    optional "export ", starts with `NAME=` (NAME as the settings name their
    keys, `[A-Z][A-Z0-9_]*`) carries no "#" after whitespace past its "=".
    The README's dotenv blocks carried 13 such lines and docs/ALERT_SYSTEM.md's
    configuration blocks 6: pasted into the live file,
    `SMS_DAILY_HOUR=8   # UTC hour` or `ALERTS_MODE=disabled   # ...` stops the
    boot under the Quadlet unit."""
    key_lines: dict[str, int] = {}
    offenders: dict[str, list[str]] = {}
    for path in FILES:
        lines = path.read_text(encoding="utf-8").splitlines()
        key_lines[path.name] = sum(1 for line in lines if _KEY_LINE.match(line))
        found = [line for line in lines if _COMMENT_ON_A_KEY_LINE.match(line)]
        if found:
            offenders[path.name] = found
    # Each of the three files shows configuration lines, so this cannot pass
    # by losing its subject.
    assert all(key_lines.values()), key_lines
    assert offenders == {}


@pytest.mark.parametrize("line", [
    pytest.param("   SMS_ENABLED=true   # the fallback", id="indented-fence"),   # a fence inside a list item
    pytest.param("export SMS_ENABLED=true  # the fallback", id="export"),
    pytest.param("- SMS_ENABLED=true  # the fallback", id="list-item"),
])
def test_a_key_line_an_operator_can_copy_is_held_to_the_rule(line):
    """The comment is what is refused: the same line without it is a key line
    that passes."""
    bare = line[:line.index("#")].rstrip()
    assert _COMMENT_ON_A_KEY_LINE.match(line)
    assert _KEY_LINE.match(bare) and not _COMMENT_ON_A_KEY_LINE.match(bare)
