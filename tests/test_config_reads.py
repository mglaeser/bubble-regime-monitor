"""What the configuration declares, something reads (AGENTS.md: delete before
you add - dead code is removed, not maintained).

Sweep of 2026-11-11 over the 63 settings and 6 properties of `Settings`
(attribute reads and `getattr(settings, "name")` in app/, scripts/ and
migrations/): exactly two had no reader.

`Settings.tz`, with its `TZ=UTC` line in the template, read as a knob for the
service's clock. Nothing read it: every scheduler trigger is UTC by
construction (app/scheduler.py) and the image sets TZ itself (Containerfile).

`Settings.llm_configured` read as the gateway's gate, and one line of one test
was its only reader. The gate is `GatewayConfig` at the use site
(app/llm_gateway.py): it refuses a partial or malformed configuration before a
client is built, and tests/test_llm_gateway.py::TestConfiguration pins that.

`Settings.sms_configured` has no attribute read either and stays: the alert
sender reads it through `getattr` (app/alerts/sender.py)."""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

from app.config import Settings, near_miss_env_keys, retired_env_keys

ROOT = Path(__file__).resolve().parents[1]


def test_the_settings_nothing_read_are_gone():
    assert "tz" not in Settings.model_fields
    assert not hasattr(Settings, "llm_configured")


def test_the_template_names_only_settings():
    """A line of the template that no setting reads is a knob that does
    nothing, as `TZ=UTC` was."""
    keys = dotenv_values(ROOT / ".env.example")
    assert keys, "the template has no key line; this test lost its subject"
    assert [key for key in keys if key.lower() not in Settings.model_fields] == []


def test_the_images_own_tz_is_not_named_as_a_stray_key():
    """TZ stays in every container's environment (Containerfile: ENV TZ=UTC),
    and a `TZ=UTC` copied from the old template may stay in a live `.env`. It
    is an operating-system variable, not a retired setting: neither key
    detector names it, so the boot log and health say nothing about it."""
    assert retired_env_keys({"TZ": "UTC"}) == []
    assert near_miss_env_keys({"TZ": "UTC"}) == []
