"""The template is a file an operator copies and boots (README, "Install").

Two readers take a `.env` made from it. In a checkout, pydantic-settings reads
it with python-dotenv. In a container - compose's `env_file`, the Quadlet's
`EnvironmentFile=` - podman's `--env-file` reads it, and that reader takes
everything after the first "=" as the value, an inline comment included
(executed 2026-11-11 on podman 4.9.3: the line `AUDIT_BOOL=false   # note`
reached the container as that whole string). So a comment never shares a line
with a key: not in the template, and not in the documents, whose configuration
lines an operator pastes into the live file.
"""

from __future__ import annotations

import re
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"

#: A key line, or a commented-out one: `KEY=` from the first column.
_KEY_LINE = re.compile(r"^#?[A-Z][A-Z0-9_]*=")
#: The same line, carrying a comment after its value.
_COMMENT_ON_A_KEY_LINE = re.compile(_KEY_LINE.pattern + r".*\s#")


def _env_file_values(text: str) -> dict[str, str]:
    """podman's --env-file: blank lines and lines starting with # are skipped,
    and the value is everything after the first "=", verbatim."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        name, _, value = stripped.partition("=")
        values[name] = value
    return values


def test_both_readers_take_the_same_value_from_every_line():
    """Also refuses a quoted value, which the two readers read differently."""
    by_podman = _env_file_values(EXAMPLE.read_text(encoding="utf-8"))
    by_dotenv = {key: value or "" for key, value in dotenv_values(EXAMPLE).items()}
    assert by_podman == by_dotenv


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


def test_the_copied_template_boots_and_a_blank_key_stays_blank(monkeypatch, tmp_path):
    """`cp .env.example .env` under the Quadlet unit: the settings build, no
    transport and no provider counts as configured, and the admin key is
    still the placeholder the guard refuses (app/security.py) - a comment
    after it would have made a published string the working key."""
    from app.config import Settings
    from app.security import PLACEHOLDER_ADMIN_KEY

    values = _env_file_values(EXAMPLE.read_text(encoding="utf-8"))
    monkeypatch.chdir(tmp_path)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)

    for key, value in values.items():
        if value == "" and key.lower() in Settings.model_fields:
            got = getattr(settings, key.lower())
            assert (got.get_secret_value() if hasattr(got, "get_secret_value") else got) == "", key
    assert settings.admin_api_key == PLACEHOLDER_ADMIN_KEY
    assert settings.imessage_configured is False
    assert settings.sms_configured is False
    assert settings.daily_digest_transport == "none"
    assert settings.alerts_mode == "disabled"


def test_the_template_and_the_readme_name_every_credential_and_switch():
    """A `.env` made from the template can be completed from it: every
    credential and every on/off switch the settings declare has a line. Of
    those 15 the template lacked two: POLYGON_API_KEY - breadth's only
    provider (app/sources/breadth.py), so an install from the documents dropped
    D1, 0.35 of block D, on every recompute - and MESSAGE_ENGINE_ENABLED, which
    the template's own comments name. The README names every credential too:
    it said "two free API keys" and stopped."""
    from app.config import Settings

    wanted = [name for name in Settings.model_fields
              if name.endswith(("_api_key", "_token", "_token_id", "_enabled"))]
    template = {key.lower() for key in dotenv_values(EXAMPLE)}
    assert [name for name in wanted if name not in template] == []
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert [name for name in wanted
            if not name.endswith("_enabled") and name.upper() not in readme] == []
