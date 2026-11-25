"""Dependencies are locked (AGENTS.md: a dependency is pinned, and the image
and CI run what was tested).

requirements.lock is what the image and `make install` install;
requirements-dev.lock is what CI installs, the same packages at the same
versions plus the dev tools. `make lock` writes both from pyproject.toml. They replace a CI install list kept equal to
pyproject.toml by hand, whose ceilings still let a release reach CI unasked:
SQLAlchemy 2.1.0 (2026-09-24) and Deprecated 3.0.0 (2026-09-26) each broke
the type-check ratchet, and the Containerfile, which resolved from
pyproject.toml at build time, would have shipped them on the next deploy.

The image's two inputs outside the lock are pinned in the Containerfile the
same way: the base image by digest, and exuber (S4's engine, R) by a dated
CRAN snapshot and a version the build checks. The tests here hold the file to
that form; CI's `image` job builds it from nothing on every pull request, so a
digest or a snapshot that is no longer served, or another exuber, fails there."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from app.references import REGISTRY

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = ROOT / "requirements.lock"
DEV_LOCK = ROOT / "requirements-dev.lock"
LOCKS = [(RUNTIME_LOCK, ()), (DEV_LOCK, ("dev",))]
LOCK_IDS = ["image", "ci"]

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


def test_ci_runs_the_images_versions():
    image, ci = _locked(RUNTIME_LOCK), _locked(DEV_LOCK)
    differ = {name: (version, ci.get(name)) for name, version in image.items()
              if ci.get(name) != version}
    assert not differ, f"requirements-dev.lock departs from requirements.lock: {differ}"


def test_the_makefile_installs_the_same_locks():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    for target, lock in (("install", "requirements.lock"), ("dev", "requirements-dev.lock")):
        start = makefile.index(f"{target}:") + 1
        recipe = []
        for line in makefile[start:]:
            if not line.startswith("\t"):
                break
            recipe.append(line.strip())
        assert any(f"--require-hashes -r {lock}" in line for line in recipe), (target, recipe)


def test_the_image_installs_only_the_runtime_lock():
    installs = _pip_installs((ROOT / "Containerfile").read_text(encoding="utf-8"))
    assert any("--require-hashes" in args and "-r requirements.lock" in args
               for args in installs), installs
    for args in installs:
        assert ("--require-hashes" in args and "-r requirements.lock" in args) or \
            "--no-deps" in args, args


def _instructions(keyword: str) -> list[str]:
    """The arguments of every Containerfile instruction of one kind, with the
    continuation lines joined."""
    text = (ROOT / "Containerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    found = []
    for line in text.splitlines():
        head, _, arguments = line.strip().partition(" ")
        if head.upper() == keyword:
            found.append(arguments.strip())
    return found


def test_the_image_starts_from_one_base_image():
    """A tag names whatever the registry serves on the day a build's cache is
    empty; a digest names one image. The release builds without `--pull`, so
    production's base was the tag as the host once pulled it: the image of
    2026-08-05 (CPython 3.12.13 on Debian trixie), still the one the release
    of 2026-11-01 built on. A pruned cache (SECURITY.md asks for one after a
    disclosure) or a new host would have built on another CPython, and in time
    on another Debian and its R, with no commit and no test to show it."""
    (base,) = _instructions("FROM")
    assert re.fullmatch(r"docker\.io/library/python:3\.12-slim@sha256:[0-9a-f]{64}", base), base


def test_s4s_engine_comes_from_a_dated_snapshot_at_a_checked_version():
    """S4's statistic and its critical values are exuber's (r/gsadf.R), and no
    gate runs it: the suite skips its R tests where R is absent, and the
    release's smoke boot fits nothing. `install.packages` from CRAN takes the
    newest exuber and the newest of the nine packages CRAN adds with it (doRNG
    and rngtools carry the random streams of radf_mc_cv), so the engine was
    whatever CRAN served on the day the R layer's cache was filled: 2026-08-09
    in production, reused by every release since. A dated snapshot serves the
    same ten packages to every build, and the build stops unless exuber is the
    version the methodology registry says S4's critical values were measured
    with."""
    (r_layer,) = [run for run in _instructions("RUN") if "install.packages" in run]
    assert r_layer.count("install.packages(") == 1, r_layer
    repos = re.findall(r"repos='([^']+)'", r_layer)
    assert len(repos) == 1 and re.fullmatch(
        r"https://packagemanager\.posit\.co/cran/\d{4}-\d{2}-\d{2}", repos[0]), repos
    (checked,) = re.findall(
        r"stopifnot\(packageVersion\('exuber'\) == '(\d+\.\d+\.\d+)'\)", r_layer)
    assert f"measured with exuber {checked}" in REGISTRY["s4"].how


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
    the lock; the lock audit sees exactly what the image installs. Dropping
    either narrows the gate (the panel on #133 caught the first)."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = workflow.split("name: Security — dependency audit (BLOCKING)")[1].split("- name:")[0]
    audits = re.findall(r"pip-audit[^\n]*", step)
    assert any(" -r " not in f" {audit} " for audit in audits), (
        f"no audit of the installed environment: {audits}")
    assert any("-r requirements.lock" in audit and "-r requirements-dev.lock" in audit
               for audit in audits), f"no audit of the locks: {audits}"
