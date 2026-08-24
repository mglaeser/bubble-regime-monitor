"""Fail closed when assembling Docker and Podman build contexts.

The deploy host keeps live credentials in ``.env`` while ``Containerfile``
copies the repository context wholesale.  These tests pin one portable ignore
policy for both supported engines so host-only state cannot enter an image.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE_POLICIES = (
    pytest.param("Docker", ROOT / ".dockerignore", id="docker"),
    pytest.param("Podman", ROOT / ".containerignore", id="podman"),
)

HOST_ONLY_PATHS = (
    ".env",
    ".env.local",
    "ops/.env",
    "ops/.env.production",
    "secrets/gateway-token",
    ".secrets/signing-token",
    "tls/private.key",
    "tls/client.pem",
    "artifacts/verifier/mc3/local-skeleton.json",
    "artifacts/verifier/mc3/local-plan.json",
    "vendor/unreviewed.whl",
    "vendor/unreviewed.tar.gz",
    ".git/config",
    ".venv/bin/python",
    "tools/venv/bin/python",
    "app/__pycache__/main.pyc",
    ".pytest_cache/v/cache/nodeids",
    ".mypy_cache/3.11/meta.json",
    ".ruff_cache/content",
    ".cache/pip/wheels/example.whl",
    "data/bubblegauge.db",
    "data/snapshots/latest.json",
)

REQUIRED_BUILD_INPUTS = (
    ".env.example",
    "Containerfile",
    "pyproject.toml",
    "alembic.ini",
    "app/main.py",
    "config/alert_rules.v3.2.yaml",
    "migrations/env.py",
    "r/gsadf.R",
    "frozen_methodology.json",
    "deploy.sh",
    "deploy/deploy-watch.sh",
    "deploy/systemd/bubblegauge-deploy.service",
    "compose.yml",
)


def _rules(policy: Path) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in policy.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _matches(path: str, pattern: str) -> bool:
    """Match the portable subset used by both container ignore files."""
    if "/" in pattern:
        return fnmatch.fnmatchcase(path, pattern)
    return any(fnmatch.fnmatchcase(part, pattern) for part in path.split("/"))


def _is_excluded(path: str, rules: tuple[str, ...]) -> bool:
    excluded = False
    for rule in rules:
        negate = rule.startswith("!")
        pattern = rule[1:] if negate else rule
        if _matches(path, pattern):
            excluded = not negate
    return excluded


def test_docker_and_podman_use_the_same_build_context_policy():
    assert (ROOT / ".dockerignore").read_bytes() == (
        ROOT / ".containerignore"
    ).read_bytes(), "Docker and Podman must not have different secret-exposure rules"


@pytest.mark.parametrize(("engine", "policy"), ENGINE_POLICIES)
def test_host_secrets_local_state_and_runtime_data_are_excluded(engine, policy):
    rules = _rules(policy)
    leaked = [path for path in HOST_ONLY_PATHS if not _is_excluded(path, rules)]
    assert not leaked, f"{engine} build context would include host-only files: {leaked}"


@pytest.mark.parametrize(("engine", "policy"), ENGINE_POLICIES)
def test_required_source_and_deploy_inputs_remain_in_context(engine, policy):
    rules = _rules(policy)
    absent = [path for path in REQUIRED_BUILD_INPUTS if not (ROOT / path).exists()]
    assert not absent, f"required build-input fixture paths do not exist: {absent}"
    missing = [path for path in REQUIRED_BUILD_INPUTS if _is_excluded(path, rules)]
    assert not missing, f"{engine} build context would omit required inputs: {missing}"


@pytest.mark.parametrize(("_engine", "policy"), ENGINE_POLICIES)
def test_ignore_policy_uses_portable_docker_and_podman_syntax(_engine, policy):
    for rule in _rules(policy):
        pattern = rule.removeprefix("!")
        assert pattern
        assert not pattern.startswith("/")
        assert not pattern.endswith("/")
        assert "\\" not in pattern
        assert "[" not in pattern and "]" not in pattern
