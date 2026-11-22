"""The security policy and the residual-risk register name the controls that
exist (SECURITY.md: "This describes controls that exist in code, not
aspirations").

Each test reads the control where it lives - a workflow, a unit file, a module,
AGENTS.md - and then the sentence that describes it. What GitHub's settings
hold (the ruleset on main) cannot be read from the repository and is not
tested here.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "SECURITY.md"
REGISTER = ROOT / "audit" / "06-residual-risk-register.md"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _section(heading: str) -> str:
    """The policy's section whose heading begins with `heading`."""
    text = POLICY.read_text(encoding="utf-8")
    start = text.index(f"## {heading}")
    end = text.find("\n## ", start)
    return text[start:] if end == -1 else text[start:end]


def _rows() -> dict[str, list[str]]:
    """The register's table: a row's id -> its six cells (id, residual risk,
    band, compensating control, tripwire, target)."""
    rows = {}
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        if line.startswith("| **"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            assert len(cells) == 6, line
            rows[cells[0].strip("*")] = cells
    return rows


def test_no_security_document_names_a_control_that_is_gone():
    # the release rolls nothing back, and deploy.sh, whose health-check
    # auto-rollback the register named as the compensating control of rows
    # A-01/B-35, A-24/B-19/B-31 and B-03/B-28, went with the webhook chain on
    # 2026-10-02
    assert not (ROOT / "deploy.sh").exists() and not list((ROOT / "deploy").rglob("deploy.sh"))
    assert "Nothing rolls back" in _read("docs/AUTO_DEPLOY.md")
    gone = ("deploy.sh", "auto-rollback")
    policy = POLICY.read_text(encoding="utf-8")
    assert not [word for word in gone if word in policy]
    # every row of the register is read, the three that named it among them
    rows = _rows()
    assert {"A-01 / B-35", "A-24 / B-19 / B-31", "B-03 / B-28"} <= set(rows)
    named = {row: [word for word in gone if word in " ".join(cells)] for row, cells in rows.items()}
    assert not {row: words for row, words in named.items() if words}


def test_the_security_policy_names_the_deploy_note():
    # AGENTS.md rule 1: a runtime prompt carries computed numbers and enums,
    # with one exception - the deploy note, whose prompt carries this
    # repository's commit texts. The statement on what reaches a model names
    # that prompt beside the two it listed.
    from app.services import deploy_note

    assert "The one exception is the deploy note (`app/services/deploy_note.py`" in _read("AGENTS.md")
    assert callable(deploy_note.prompt)
    section = _section("AI / model data-use")
    for module in ("app/engine/judgment.py", "app/message_engine/composer.py", "app/services/deploy_note.py"):
        assert f"`{module}`" in section, module


def test_the_security_policy_states_the_hash_locks():
    # the image and CI install from the locks, hash-checked
    assert "pip install --no-cache-dir --require-hashes -r requirements.lock" in _read("Containerfile")
    assert "pip install --require-hashes -r requirements-dev.lock" in _read(".github/workflows/ci.yml")
    section = _section("Supply chain")
    assert "`--require-hashes`" in section
    assert "`requirements.lock`" in section and "`requirements-dev.lock`" in section
    assert "tracked improvement" not in section
    # the register's row for the missing lock is closed, and says by what
    assert "B-04 / C-03" not in _rows()
    closed = REGISTER.read_text(encoding="utf-8").split("## Changed since the audit")[1]
    assert "**B-04 / C-03**" in closed and "`--require-hashes`" in closed


def test_the_personal_data_section_names_both_recipients():
    # two transports, each with a recipient of the operator's own, each masked
    # where it is logged
    from app.config import Settings
    from app.notify import imessage, sipgate

    assert {"sipgate_recipient", "imessage_recipient"} <= set(Settings.model_fields)
    section = _section("Personal data")
    for name, module in (("sipgate", sipgate), ("imessage", imessage)):
        assert module._mask_recipient("the-operators-handle") == "…dle"
        assert f"`app/notify/{name}.py:_mask_recipient`" in section, name


def test_the_code_owner_residual_codeowners_cites_is_in_the_register():
    # CODEOWNERS and the separation check both send the reader to the register
    # for why Code Owner review is not enforced and for what compensates
    for citing in (".github/CODEOWNERS", "scripts/regime/separation_check.py"):
        assert "audit/06-residual-risk-register.md" in _read(citing), citing
    _, residual, _, control, _, _ = _rows()["A-01 / B-35"]
    assert "Code Owner review" in residual
    assert "scripts.regime.separation_check" in " ".join(_rows()["A-01 / B-35"])
    assert control


def test_the_register_keeps_no_count_that_moves_with_every_change():
    # the mypy ceiling stands in ci.yml and AGENTS.md and falls with every
    # commit that lowers the count: the register said 197 at a ceiling of 116,
    # and counted the tests of 2026-07
    assert re.search(r'MYPY_CEILING: "\d+"', _read(".github/workflows/ci.yml"))
    register = REGISTER.read_text(encoding="utf-8")
    assert "`MYPY_CEILING`" in register
    assert not re.findall(r"\d+ mypy errors", register)
    assert not re.findall(r"\d+ passing tests", register)
