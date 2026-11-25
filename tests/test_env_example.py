"""A comment never shares a line with a key: not in the template, and not in
the documents, whose configuration lines an operator pastes into the live file.

In a container - compose's `env_file`, the Quadlet's `EnvironmentFile=` -
podman's `--env-file` reads the `.env`, and that reader takes everything after
the first "=" as the value, an inline comment included.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"

#: A key line, or a commented-out one: `KEY=` from the first column.
_KEY_LINE = re.compile(r"^#?[A-Z][A-Z0-9_]*=")
#: The same line, carrying a comment after its value.
_COMMENT_ON_A_KEY_LINE = re.compile(_KEY_LINE.pattern + r".*\s#")


def test_no_key_line_carries_a_comment_in_the_template_or_the_documents():
    """The README's dotenv blocks carried 13 such lines and
    docs/ALERT_SYSTEM.md's configuration blocks 6: pasted into the live file,
    `SMS_DAILY_HOUR=8   # UTC hour` or `ALERTS_MODE=disabled   # ...` stops the
    boot under the Quadlet unit."""
    documents = [ROOT / "README.md", ROOT / "SECURITY.md", *sorted((ROOT / "docs").glob("*.md"))]
    key_lines: dict[str, int] = {}
    offenders: dict[str, list[str]] = {}
    for path in (EXAMPLE, *documents):
        lines = path.read_text(encoding="utf-8").splitlines()
        key_lines[path.name] = sum(1 for line in lines if _KEY_LINE.match(line))
        found = [line for line in lines if _COMMENT_ON_A_KEY_LINE.match(line)]
        if found:
            offenders[path.name] = found
    # The three files that show configuration lines today still do, so this
    # cannot pass by losing its subject.
    assert all(key_lines[name] for name in (".env.example", "README.md", "ALERT_SYSTEM.md"))
    assert offenders == {}
