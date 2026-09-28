"""Dependencies are locked (AGENTS.md: a dependency is pinned, and the image
and CI run what was tested).

requirements.lock is what the image installs; requirements-dev.lock is what
CI installs: the image's packages at the image's versions (pyarrow, the
optional Parquet extra, aside) plus the dev tools. `make lock` writes both
from pyproject.toml. They replace a CI install list kept equal to
pyproject.toml by hand, whose ceilings still let a release reach CI unasked:
SQLAlchemy 2.1.0 (2026-09-24) and Deprecated 3.0.0 (2026-09-26) each broke
the type-check ratchet, and the Containerfile, which resolved from
pyproject.toml at build time, would have shipped them on the next deploy."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = ROOT / "requirements.lock"
DEV_LOCK = ROOT / "requirements-dev.lock"
LOCKS = [(RUNTIME_LOCK, ("parquet",)), (DEV_LOCK, ("dev",))]

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\;]+)")


def _locked(path: Path) -> dict[str, str]:
    """Package name -> version, for every package in a lock."""
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match:
            pins[canonicalize_name(match.group(1))] = match.group(2)
    return pins


def _declared(*extras: str) -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = [Requirement(dep) for dep in project["dependencies"]]
    for extra in extras:
        declared += [Requirement(dep) for dep in project["optional-dependencies"][extra]]
    return declared


def _pip_installs(text: str) -> list[str]:
    """The arguments of every `pip install` in a build or workflow file."""
    return [args.strip() for args in re.findall(r"pip install([^\n&|]*)", text)]


@pytest.mark.parametrize(("lock", "extras"), LOCKS, ids=["image", "ci"])
def test_every_declared_dependency_is_locked_within_its_specifier(lock, extras):
    locked = _locked(lock)
    for requirement in _declared(*extras):
        name = canonicalize_name(requirement.name)
        assert name in locked, f"{requirement.name} is declared but not in {lock.name}"
        assert requirement.specifier.contains(locked[name], prereleases=True), (
            f"{lock.name} holds {requirement.name}=={locked[name]}, "
            f"outside {requirement.specifier}")


@pytest.mark.parametrize(("lock", "_extras"), LOCKS, ids=["image", "ci"])
def test_every_locked_package_carries_a_hash(lock, _extras):
    entries = re.split(r"\n(?=[A-Za-z0-9])", lock.read_text(encoding="utf-8"))
    unhashed = [entry.split()[0] for entry in entries
                if _PIN.match(entry) and "--hash=sha256:" not in entry]
    assert not unhashed, f"{lock.name}: no hash for {unhashed}"


def test_ci_runs_the_images_versions():
    image, ci = _locked(RUNTIME_LOCK), _locked(DEV_LOCK)
    differ = {name: (version, ci[name]) for name, version in image.items()
              if name in ci and ci[name] != version}
    assert not differ, f"CI and the image lock different versions: {differ}"


def test_the_image_installs_only_the_runtime_lock():
    installs = _pip_installs((ROOT / "Containerfile").read_text(encoding="utf-8"))
    assert any("--require-hashes" in args and "-r requirements.lock" in args
               for args in installs), installs
    for args in installs:
        assert ("--require-hashes" in args and "-r requirements.lock" in args) or \
            "--no-deps" in args, args


def test_ci_installs_only_the_dev_lock():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    installs = _pip_installs(workflow)
    assert any("--require-hashes" in args and "-r requirements-dev.lock" in args
               for args in installs), installs
    bootstrap = '--upgrade pip "setuptools>=83"'
    for args in installs:
        assert args.startswith(bootstrap) or (
            "--require-hashes" in args and "-r requirements-dev.lock" in args), args


def test_the_audit_covers_what_ci_runs_and_what_the_image_installs():
    """The environment audit sees pip and setuptools, which CI installs outside
    the lock; the lock audit sees pyarrow, which only the image installs.
    Dropping either narrows the gate (the panel on #133 caught the first)."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = workflow.split("name: Security — dependency audit (BLOCKING)")[1].split("- name:")[0]
    audits = re.findall(r"pip-audit[^\n]*", step)
    assert any(" -r " not in f" {audit} " for audit in audits), (
        f"no audit of the installed environment: {audits}")
    assert any("-r requirements.lock" in audit and "-r requirements-dev.lock" in audit
               for audit in audits), f"no audit of the locks: {audits}"
