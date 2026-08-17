#!/usr/bin/env python3
"""Authorisation coverage — mandate C-01.

Every HTTP route either carries an authorisation dependency or is named in the
PUBLIC allowlist below. There is no third state, and there is no way to add a
public route quietly: the allowlist is source, it is owned in CODEOWNERS, and
CI fails on a route that is in neither set.

That is the whole point. A route becomes reachable-by-anyone through an
omission — nobody writes `# no auth needed` — so the control has to make the
omission LOUD rather than ask reviewers to notice an absence.

Parsed with `ast`, not grep: a decorator inside a string or a comment is not a
route, and a route split across lines is still a route.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTERS = ROOT / "app" / "routers"

HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})

# Callables that establish authorisation. A route whose signature depends on one
# of these is covered. Extend deliberately — every addition widens what counts
# as "authorised", so it belongs in a reviewed diff.
AUTH_DEPENDENCIES = frozenset({
    "require_admin_key",
    "require_read_access",
    "require_alerts_read",
    "require_alerts_write",
})

# Routes that ARE authorised, but inside the handler rather than by a
# dependency — so the AST cannot see it. Each entry names where the check
# lives, so the claim is verifiable rather than asserted.
#
# Kept separate from PUBLIC_ALLOWLIST on purpose: calling an HMAC-verified
# webhook "public" would be false, and a control that files authorised routes
# under "public" teaches its readers to distrust it.
IN_HANDLER_AUTH: dict[str, str] = {
    "POST /github": (
        "GitHub webhook: constant-time HMAC-SHA256 over the RAW body "
        "(X-Hub-Signature-256), app/routers/webhooks.py:32-37, enforced at :55 "
        "with 401 on mismatch. Fail-closed — :49 refuses outright unless both "
        "GITHUB_WEBHOOK_SECRET and DEPLOY_BRANCH are configured."
    ),
}

# Routes that are PUBLIC BY DECISION, each with the reason it is safe to be.
# Adding a line here is the deliberate act the control exists to force.
#
# The reason matters as much as the entry: "/readyz" is public AND returns
# SourceHealth.note straight from the database, which is how a provider error
# string — and with it an API key carried in a request URL — could reach an
# anonymous caller. Being on this list is a commitment that the handler's
# OUTPUT is safe to publish, not merely that its existence is.
PUBLIC_ALLOWLIST: dict[str, str] = {
    "GET /healthz": "liveness probe: returns a constant, reads nothing",
    "GET /readyz": (
        "readiness probe for the deploy health-poll in deploy.sh, which runs "
        "before the service holds a credentialed session. Its body is a "
        "per-source status matrix; SourceHealth.note MUST stay free of raw "
        "provider error text"
    ),
    "GET /": "static HTML status page (a module-level constant, no data access)",
    "GET /status": "same static HTML status page as GET /",
}


class RouteVisitor(ast.NodeVisitor):
    """Collects (method, path, has_auth) for every decorated route handler."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, bool]] = []

    def _handle(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            method = dec.func.attr.lower()
            if method not in HTTP_METHODS:
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Constant):
                continue
            path = str(dec.args[0].value)
            self.routes.append((method.upper(), path, self._authed(node, dec)))

    def _authed(self, node: ast.FunctionDef | ast.AsyncFunctionDef, dec: ast.Call) -> bool:
        # Two placements count, because both are used in this codebase: a
        # Depends(...) in the handler signature, and a dependencies=[...] list
        # on the decorator itself.
        names = {n.id for n in ast.walk(node.args) if isinstance(n, ast.Name)}
        for kw in dec.keywords:
            if kw.arg == "dependencies":
                names |= {n.id for n in ast.walk(kw.value) if isinstance(n, ast.Name)}
        return bool(names & AUTH_DEPENDENCIES)

    visit_FunctionDef = _handle          # noqa: N815 -- ast.NodeVisitor's naming
    visit_AsyncFunctionDef = _handle     # noqa: N815


def routes_in(source: str) -> list[tuple[str, str, bool]]:
    v = RouteVisitor()
    v.visit(ast.parse(source))
    return v.routes


def scan(directory: Path = ROUTERS) -> tuple[list[str], list[str], int]:
    """(uncovered, stale_allowlist_entries, total_routes)."""
    declared = PUBLIC_ALLOWLIST.keys() | IN_HANDLER_AUTH.keys()
    seen: set[str] = set()
    uncovered: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        for method, route, authed in routes_in(path.read_text(encoding="utf-8")):
            key = f"{method} {route}"
            seen.add(key)
            if not authed and key not in declared:
                uncovered.append(f"{key}    ({path.relative_to(ROOT)})")
    # An entry for a route that no longer exists is a standing permission
    # nobody is watching; the next route to take that path inherits it
    # silently. Checked for both lists.
    stale = sorted(k for k in declared if k not in seen)
    return uncovered, stale, len(seen)


def selftest() -> int:
    def expect(cond: bool, msg: str) -> None:
        if not cond:
            print(f"FAIL selftest: {msg}", file=sys.stderr)
            sys.exit(1)

    expect(routes_in("@router.get('/a')\ndef h(): pass\n") == [("GET", "/a", False)],
           "a bare route is detected and reported unauthorised")
    expect(routes_in("@router.get('/a')\ndef h(x=Depends(require_admin_key)): pass\n")
           == [("GET", "/a", True)], "Depends in the signature counts as auth")
    expect(routes_in("@router.get('/a', dependencies=[Depends(require_read_access)])\n"
                     "def h(): pass\n") == [("GET", "/a", True)],
           "dependencies=[...] on the decorator counts as auth")
    expect(routes_in("@router.get('/a')\nasync def h(x=Depends(require_alerts_read)): pass\n")
           == [("GET", "/a", True)], "async handlers are handled")
    expect(routes_in("@router.get('/a')\ndef h(x=Depends(some_other_thing)): pass\n")
           == [("GET", "/a", False)],
           "a non-auth Depends must NOT count — that is the whole failure mode")
    expect(routes_in("# @router.get('/a')\ndef h(): pass\n") == [],
           "a commented-out decorator is not a route")
    expect(routes_in("s = '@router.get(\"/a\")'\n") == [],
           "a decorator inside a string is not a route")
    expect(routes_in("@router.on_event('startup')\ndef h(): pass\n") == [],
           "a non-HTTP decorator is not a route")

    print("   OK selftest: routes_in (signature, decorator, async, non-auth, comment, string)")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if not ROUTERS.is_dir():
        print(f"BLOCK authz coverage: {ROUTERS} does not exist.", file=sys.stderr)
        return 1
    uncovered, stale, total = scan()
    rc = 0
    if uncovered:
        print("BLOCK authz coverage (C-01): routes with no authorisation dependency "
              "and no PUBLIC_ALLOWLIST entry:", file=sys.stderr)
        for line in uncovered:
            print(f"  {line}", file=sys.stderr)
        print("  Add the dependency, or add the route to PUBLIC_ALLOWLIST with the "
              "reason its OUTPUT is safe to publish.", file=sys.stderr)
        rc = 1
    if stale:
        print("BLOCK authz coverage: PUBLIC_ALLOWLIST names routes that no longer "
              "exist — a standing permission the next route on that path inherits:",
              file=sys.stderr)
        for key in stale:
            print(f"  {key}", file=sys.stderr)
        rc = 1
    if rc == 0:
        print(f"Authz coverage: {total} routes — "
              f"{total - len(PUBLIC_ALLOWLIST) - len(IN_HANDLER_AUTH)} by dependency, "
              f"{len(IN_HANDLER_AUTH)} verified in-handler, "
              f"{len(PUBLIC_ALLOWLIST)} public by decision.")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
