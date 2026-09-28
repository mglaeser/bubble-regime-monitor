"""Dependencies are locked (AGENTS.md: a dependency is pinned, and the image
and CI run what was tested).

requirements.lock is the plain install (`make install`); requirements-image.lock
is what the image installs, the same plus the Parquet extra (pyarrow), behind
the Containerfile's CPU probe; requirements-dev.lock is what CI installs, the
plain lock plus the dev tools. `make lock` writes all three from pyproject.toml. They replace a CI install list kept equal to
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
IMAGE_LOCK = ROOT / "requirements-image.lock"
DEV_LOCK = ROOT / "requirements-dev.lock"
LOCKS = [(RUNTIME_LOCK, ()), (IMAGE_LOCK, ("parquet",)), (DEV_LOCK, ("dev",))]
LOCK_IDS = ["plain", "image", "ci"]

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


@pytest.mark.parametrize(("lock", "extras"), LOCKS, ids=LOCK_IDS)
def test_every_declared_dependency_is_locked_within_its_specifier(lock, extras):
    locked = _locked(lock)
    for requirement in _declared(*extras):
        name = canonicalize_name(requirement.name)
        assert name in locked, f"{requirement.name} is declared but not in {lock.name}"
        assert requirement.specifier.contains(locked[name], prereleases=True), (
            f"{lock.name} holds {requirement.name}=={locked[name]}, "
            f"outside {requirement.specifier}")


@pytest.mark.parametrize(("lock", "_extras"), LOCKS, ids=LOCK_IDS)
def test_every_locked_package_carries_a_hash(lock, _extras):
    entries = re.split(r"\n(?=[A-Za-z0-9])", lock.read_text(encoding="utf-8"))
    unhashed = [entry.split()[0] for entry in entries
                if _PIN.match(entry) and "--hash=sha256:" not in entry]
    assert not unhashed, f"{lock.name}: no hash for {unhashed}"


@pytest.mark.parametrize("other", [IMAGE_LOCK, DEV_LOCK], ids=["image", "ci"])
def test_every_lock_runs_the_plain_locks_versions(other):
    plain, locked = _locked(RUNTIME_LOCK), _locked(other)
    differ = {name: (version, locked[name]) for name, version in plain.items()
              if locked.get(name) != version}
    assert not differ, f"{other.name} departs from requirements.lock: {differ}"


def test_only_the_image_installs_pyarrow():
    """pyarrow (the Parquet extra) dies with SIGILL at pandas import on CPUs
    without SSE4.2, so the plain install must not carry it; the image installs
    it behind the Containerfile's probe (the panel on #133 caught `make
    install` pulling it in)."""
    assert "pyarrow" not in _locked(RUNTIME_LOCK)
    assert "pyarrow" not in _locked(DEV_LOCK)
    assert "pyarrow" in _locked(IMAGE_LOCK)
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    for target, lock in (("install", "requirements.lock"), ("dev", "requirements-dev.lock")):
        start = makefile.index(f"{target}:") + 1
        recipe = []
        for line in makefile[start:]:
            if not line.startswith("\t"):
                break
            recipe.append(line.strip())
        assert any(f"-r {lock}" in line for line in recipe), (target, recipe)
        assert not any("requirements-image.lock" in line for line in recipe), (target, recipe)


def test_the_image_installs_only_the_image_lock():
    installs = _pip_installs((ROOT / "Containerfile").read_text(encoding="utf-8"))
    assert any("--require-hashes" in args and "-r requirements-image.lock" in args
               for args in installs), installs
    for args in installs:
        assert ("--require-hashes" in args and "-r requirements-image.lock" in args) or \
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
    assert any("-r requirements-image.lock" in audit and "-r requirements-dev.lock" in audit
               for audit in audits), f"no audit of the locks: {audits}"
