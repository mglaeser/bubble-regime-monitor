"""AGENTS.md: "the suite is hermetic". What the test process must not take
from the host it runs on."""
from __future__ import annotations


def test_the_suite_reads_no_dotenv_file(tmp_path, monkeypatch):
    """Settings reads `.env` from the working directory (app/config.py), so a
    suite started in a deployed checkout ran on that host's recipient and
    provider keys: with the file below in the working directory, Settings()
    gave iMessage enabled and the file's recipient. tests/conftest.py turns
    the file off for the test process; a test that needs a dotenv file names
    it itself, as the one in tests/test_imessage_digest.py does."""
    from app.config import Settings

    (tmp_path / ".env").write_text(
        "IMESSAGE_ENABLED=true\nIMESSAGE_RECIPIENT=someone@example.com\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IMESSAGE_ENABLED", raising=False)
    monkeypatch.delenv("IMESSAGE_RECIPIENT", raising=False)
    settings = Settings()
    assert (settings.imessage_enabled, settings.imessage_recipient) == (False, "")
