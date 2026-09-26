"""The SQLAlchemy ceiling (AGENTS.md: a dependency is pinned). SQLAlchemy
2.1.0 was released on 2026-09-24 and CI installed it unasked: the type-check
ratchet rose from 158 to 161 (three new errors in app/alerts/cli.py and
app/alerts/health.py), and the Containerfile, which installs from
pyproject.toml, would have shipped it on the next deploy. Production runs
2.0; moving to 2.1 is an upgrade of its own, made on purpose."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CEILING = "SQLAlchemy>=2.0,<2.1"


def _pyproject_spec(name: str) -> str:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    (spec,) = [dep for dep in deps if re.match(rf"{name}\b", dep, re.IGNORECASE)]
    return spec


def _ci_spec(name: str) -> str:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = workflow.split("Install Python deps (aligned to pyproject)")[1].split("- name:")[0]
    (spec,) = re.findall(rf'"?({name}[^"\s]*)"?', step, re.IGNORECASE)
    return spec


def test_the_image_installs_sqlalchemy_below_2_1():
    assert _pyproject_spec("SQLAlchemy") == CEILING


def test_ci_installs_the_same_sqlalchemy_as_the_image():
    assert _ci_spec("SQLAlchemy") == CEILING


#: The message engine's link detection (the owner, 2026-09-26: a common
#: problem goes to a well-maintained library): linkify-it-py for links,
#: libphonenumber for numbers a phone dials. An upgrade changes what counts
#: as a link, so each version is exact and moved on purpose.
DETECTORS = {"linkify-it-py": "linkify-it-py==2.2.0", "phonenumberslite": "phonenumberslite==9.0.40"}


@pytest.mark.parametrize("name", sorted(DETECTORS))
def test_the_image_pins_each_detector(name):
    assert _pyproject_spec(name) == DETECTORS[name]


@pytest.mark.parametrize("name", sorted(DETECTORS))
def test_ci_installs_the_same_detector_as_the_image(name):
    assert _ci_spec(name) == DETECTORS[name]
