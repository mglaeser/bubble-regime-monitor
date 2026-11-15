"""AGENTS.md is what a cold-start agent reads before it changes anything, and
it says of itself "Keep it true". Without a test that reads the file, its gate
list, its paths, its test ids and its one number go stale with every check
green. These tests hold the parts of it a machine can check - the gate list is
CI's, what it cites exists, its number is CI's - and one row of the map it
sends that reader to (audit/00-system-map.md)."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
WORKFLOWS = ROOT / ".github" / "workflows"
CI = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
#: "How to make a change (the gate)", up to the table of change classes.
GATE = AGENTS[AGENTS.index("## How to make a change"):AGENTS.index("### Verification tier")]
#: What CI's blocking steps run that is not `python -m <module>`.
TOOLS = ("ruff", "pip-audit", "detect-secrets", "pytest")


def _named_as(run: str) -> set[str]:
    """What the gate list calls a step: each module the step runs with
    `python -m`, or else the tool."""
    modules = set(re.findall(r"python -m ([\w.]+)", run))
    return modules or {tool for tool in TOOLS if tool in run}


def test_agents_md_names_every_blocking_step_of_the_gate():
    """"Every change must pass ci.yml, which is blocking": the list under that
    sentence names each step ci.yml calls BLOCKING by what it runs, the `image`
    job, and every workflow file. A step the list leaves out is one an agent
    learns of from a red build - `scripts.export_alert_openapi`, after it
    changed an alert route. A workflow it leaves out is the review panel,
    which is given the diff and nothing else: an agent that does not know
    that writes its reasons where no reviewer reads them."""
    blocking = [step for job in CI["jobs"].values() for step in job.get("steps", [])
                if "BLOCKING" in step.get("name", "")]
    assert blocking, "ci.yml calls no step BLOCKING any more: this test reads that word"
    missing = []
    for step in blocking:
        names = _named_as(step["run"])
        assert names, (f"the blocking step {step['name']!r} runs neither a module nor one of "
                       f"{TOOLS}: name its tool there and in the gate list of AGENTS.md")
        missing += [(step["name"], name) for name in sorted(names) if name not in GATE]
    assert "image" in CI["jobs"], "ci.yml has no `image` job any more"
    if "`image` job" not in GATE:
        missing.append(("job", "image"))
    for workflow in sorted(WORKFLOWS.iterdir()):
        if workflow.relative_to(ROOT).as_posix() not in GATE:
            missing.append(("workflow", workflow.name))
    assert not missing, f"the gate list of AGENTS.md does not name: {missing}"


def _cited() -> list[tuple[str, list[str]]]:
    """(path, names) for every backticked token of AGENTS.md that cites this
    repository: `path`, `path:symbol`, `path::test id`, and a bare `::test id`,
    which continues the file cited before it."""
    cited: list[tuple[str, list[str]]] = []
    for token in re.findall(r"`([^`\n]+)`", AGENTS):
        if " " in token or token.startswith(("http", "/")):
            continue
        if token.startswith("::") and cited:
            cited.append((cited[-1][0], token[2:].split("::")))
        elif "/" in token:
            path, _, test_id = token.partition("::")
            path, _, symbol = path.partition(":")
            cited.append((path, [symbol] if symbol else test_id.split("::") if test_id else []))
    return cited


def _binds(node: ast.stmt) -> set[str]:
    """The names a statement defines: a function, a class, an assignment's targets."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _defines(path: Path, names: list[str]) -> bool:
    """`names` is a module-level name of the file, or a class and a name in
    its body. Read with `ast`: a name left behind in a comment or a string is
    no definition."""
    scope: ast.AST = ast.parse(path.read_text(encoding="utf-8"))
    for name in names:
        found = next((node for node in getattr(scope, "body", []) if name in _binds(node)), None)
        if found is None:
            return False
        scope = found
    return True


def test_every_path_and_test_id_agents_md_cites_exists():
    """A renamed test or a moved module leaves the constitution citing what is
    not there - and the regression test it names as the enforcement of a
    frozen invariant is then one nobody can find."""
    cited = _cited()
    assert any(names for _, names in cited) and any(not names for _, names in cited), (
        "AGENTS.md cites paths, symbols and test ids in backticks; this test read none")
    missing = [(path, names) for path, names in cited
               if not (ROOT / path).exists() or (names and not _defines(ROOT / path, names))]
    assert not missing, f"AGENTS.md cites what does not exist: {missing}"


def test_the_mypy_ceiling_agents_md_states_is_the_one_ci_enforces():
    """"Lower the ceiling in the same commit that lowers the count": the number
    AGENTS.md prints is the one ci.yml holds, not last month's."""
    stated = re.search(r"`MYPY_CEILING`[^*]*\(\*\*(\d+)\*\* today", GATE)
    assert stated, "AGENTS.md no longer states the mypy ceiling as (**<n>** today"
    enforced = {step["env"]["MYPY_CEILING"] for job in CI["jobs"].values()
                for step in job.get("steps", []) if "MYPY_CEILING" in step.get("env", {})}
    assert enforced == {stated.group(1)}, (
        f"ci.yml enforces {sorted(enforced)}, AGENTS.md states {stated.group(1)}")


def test_the_system_map_names_the_origin_the_app_allows():
    """AGENTS.md sends the reader to audit/00-system-map.md "for the full
    map". The map's reverse-proxy row names the CORS allow-origins: every
    origin the application allows, and not crash.klee.me. That one left the
    allowlist because the host no longer resolves and a lapsed name can be
    registered by anyone (tests/test_cors.py); a map that lists it tells the
    reader the API trusts that host."""
    from fastapi.middleware.cors import CORSMiddleware

    from app.main import create_app

    system_map = (ROOT / "audit" / "00-system-map.md").read_text(encoding="utf-8")
    cors = next(middleware for middleware in create_app().user_middleware
                if middleware.cls is CORSMiddleware)
    hosts = {urlsplit(origin).hostname for origin in cors.kwargs["allow_origins"]}
    assert hosts, "the application allows no origin any more: the map's row is then wrong too"
    row = next(line for line in system_map.splitlines() if "CORS allow-origin" in line)
    assert all(f"`{host}`" in row for host in hosts), (
        f"the application allows {sorted(hosts)}; the map's row: {row}")
    dropped = [line for line in system_map.splitlines() if "crash.klee.me" in line]
    assert not dropped, f"the map names an origin the application dropped: {dropped}"
