#!/usr/bin/env python3
"""Authorisation coverage — mandate C-01.

Every route the application serves either carries an authorisation dependency
or is named in the PUBLIC allowlist below, and the read dependency alone does
not count for a write or for an operator route. There is no third state: the
allowlist is source, it is owned in CODEOWNERS, and CI fails on a route that
is in neither set.

That is the whole point. A route becomes reachable-by-anyone through an
omission — nobody writes `# no auth needed` — so the control has to make the
omission LOUD rather than ask reviewers to notice an absence.

What is judged is the application the server runs (`app.main:app`), walked
with `fastapi.routing.iter_route_contexts`, the walk FastAPI's own OpenAPI
generator uses. So a route is keyed by its method and the full path it is
served at, however it was registered (a decorator, `add_api_route`, a router
prefix, a mount, the framework's own documentation routes), and a dependency
counts when it IS one of the guards in app/security.py, not when it carries
the name of one.

Why the served application and not the source: the gate read the decorators
in app/routers/*.py and keyed a route by the decorator's path. Executed on
9f79c7a, the application served 45 method+path pairs, the gate counted 40,
and each of these changes passed it (exit 0) while the route answered 200
without a key:
  - `app.add_api_route("/api/v1/admin/open", ..., methods=["POST"])` in
    app/main.py;
  - a keyless `@router.get("/status")` in the admin router, served at
    /api/v1/admin/status and taken for the allowlisted "GET /status";
  - POST /api/v1/admin/send-sms moved to `require_read_access`, which lets
    every caller in while READ_ENDPOINTS_PUBLIC is true (the default): the
    forced digest ran for a caller without a key.
The four documentation routes FastAPI adds answered 200 and stood in no list.

Limits. The contract is what that walk yields and the dependencies a route
declares. A FastAPI release without the walk fails the import, and one whose
walk reaches fewer routes fails the selftest, which registers 18 and expects
each: closed either way. Not seen: a route added while the application runs
(in the lifespan or a handler), a `frontend()` directory (FastAPI keeps those
outside `routes`) and `dependency_overrides` (the walk shows the declared
guard, not its replacement). `app/` uses none of the three: it registers
routes with twelve `include_router` calls in `create_app` and nowhere else.
Each of the three is a line someone writes, not an omission, and
tests/test_api_contract.py::test_every_operator_route_refuses_a_caller_without_the_key
asks the running application for its 401s instead of reading declarations.
"""

from __future__ import annotations

import sys
from typing import Any

from fastapi.routing import iter_route_contexts

from app import security

# The callables that establish authorisation, compared as objects. A route
# whose dependency tree holds one of them is covered. Extend deliberately —
# every addition widens what counts as "authorised", so it belongs in a
# reviewed diff.
AUTH_DEPENDENCIES: dict[Any, str] = {
    security.require_admin_key: "require_admin_key",
    security.require_alerts_write: "require_alerts_write",
    security.require_read_access: "require_read_access",
}

# Reads are public by default (READ_ENDPOINTS_PUBLIC=true), and
# `require_read_access` then returns for every caller. Alone, it therefore
# authorises a GET outside the operator paths and nothing else (SECURITY.md:
# the admin routes and every alert route take the key).
READ_ONLY = frozenset({"require_read_access"})
OPERATOR_PATHS = ("/api/v1/admin", "/api/v1/alerts")

# There is no list of routes "authorised inside the handler". Its one entry,
# the GitHub webhook, left with the old deploy chain (owner decision D6,
# 2026-10-02) and the list stood empty since: a way to file a route as covered
# without a dependency this gate can resolve, kept for nothing.

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
        "readiness probe for the operator and uptime checks, which carry no "
        "credential. Its body is a "
        "per-source status matrix; SourceHealth.note MUST stay free of raw "
        "provider error text"
    ),
    "GET /": "static HTML status page (a module-level constant, no data access)",
    "GET /status": "same static HTML status page as GET /",
    # FastAPI adds the next four to every application unless it is told not
    # to. They are declared, not switched off, because they are in use: the
    # status page links the schema and both viewers (app/content_registry.py;
    # README.md, "Status & spec UI") and the status document names them
    # (`service.docs`, app/services/status.py). Whether they stay public is the
    # owner's decision; until this entry they were public with no decision
    # recorded anywhere.
    "GET /openapi.json": (
        "the API's schema, generated from the code: paths, parameters, "
        "response models, descriptions and the worked example. It holds no "
        "stored data and no setting other than the service version"
    ),
    "GET /docs": "Swagger viewer of /openapi.json: a constant page, reads nothing",
    "GET /redoc": "ReDoc viewer of /openapi.json: a constant page, reads nothing",
    "GET /docs/oauth2-redirect": "the Swagger viewer's helper page: a constant, reads nothing",
}


def _guards(dependant: Any) -> frozenset[str]:
    """The authorisation dependencies in a dependency tree: those the route
    names and those its dependencies depend on. FastAPI runs them all before
    the handler."""
    names: set[str] = set()
    for sub in dependant.dependencies:
        if sub.call in AUTH_DEPENDENCIES:
            names.add(AUTH_DEPENDENCIES[sub.call])
        names |= _guards(sub)
    return frozenset(names)


def served(application: Any) -> list[tuple[str, frozenset[str]]]:
    """("METHOD /full/path", its authorisation dependencies) for everything
    `application` answers. A list, not a mapping: two routes at one method and
    path are both judged."""
    rows: list[tuple[str, frozenset[str]]] = []
    for context in iter_route_contexts(application.routes):
        # No dependant: a plain Starlette route or a mount. It can carry no
        # guard, so it is covered only by a declaration.
        dependant = getattr(context, "dependant", None)
        guards = _guards(dependant) if dependant is not None else frozenset()
        methods = set(context.methods or ())
        if "GET" in methods:
            methods.discard("HEAD")     # Starlette answers HEAD on a GET route: one handler, one key
        # For a plain route, a websocket or a mount INSIDE an included router
        # the walk gives no served path, no methods and no dependant
        # (fastapi 0.141). It is named by what was declared and, having no
        # guard, is not covered.
        path = context.path or (f"<{type(context.original_route).__name__} "
                                f"{getattr(context.original_route, 'path', '?')}: served path not resolved>")
        # "*": no method list (a mount, a websocket) - whatever arrives there.
        rows += [(f"{method} {path}", guards) for method in sorted(methods) or ["*"]]
    return rows


def scan(application: Any,
         allowlist: dict[str, str] = PUBLIC_ALLOWLIST) -> tuple[list[str], list[str], list[str], int]:
    """(uncovered, under_authorised, stale_allowlist_entries, total_routes)."""
    rows = served(application)
    uncovered = sorted(key for key, guards in rows if not guards and key not in allowlist)
    under_authorised = sorted(
        key for key, guards in rows
        if guards == READ_ONLY
        and (not key.startswith("GET ") or key.split(" ", 1)[1].startswith(OPERATOR_PATHS)))
    # An entry for a route that is not served is a standing permission nobody
    # is watching; the next route to take that path inherits it silently.
    stale = sorted(allowlist.keys() - {key for key, _ in rows})
    return uncovered, under_authorised, stale, len(rows)


def selftest() -> int:
    from fastapi import APIRouter, Depends, FastAPI
    from starlette.applications import Starlette

    def expect(cond: bool, msg: str) -> None:
        if not cond:
            print(f"FAIL selftest: {msg}", file=sys.stderr)
            sys.exit(1)

    def handler() -> dict[str, Any]:
        return {}

    def require_admin_key() -> None:     # the guard's name on another function
        return None

    def through(_: None = Depends(security.require_admin_key)) -> None:
        return None

    admin = [Depends(security.require_admin_key)]
    read = [Depends(security.require_read_access)]

    router = APIRouter(prefix="/api/v1/admin")
    router.get("/status")(handler)                                  # bare, under a prefix
    router.add_api_route("/added", handler, methods=["POST"])       # bare, no decorator
    router.get("/namesake", dependencies=[Depends(require_admin_key)])(handler)     # not the guard
    router.add_route("/plain", handler)                             # a Starlette route in a router
    router.post("/decorator", dependencies=admin)(handler)
    router.post("/nested", dependencies=[Depends(through)])(handler)

    @router.post("/signature")
    def in_signature(_: None = Depends(security.require_admin_key)) -> dict[str, Any]:
        return {}

    keyed = APIRouter(prefix="/keyed", dependencies=admin)          # the guard on the router
    keyed.put("/x")(handler)
    reads = APIRouter(prefix="/api/v1")
    reads.get("/score", dependencies=read)(handler)                 # what the read guard is for
    reads.post("/score", dependencies=read)(handler)                # the read guard on a write
    reads.get("/alerts/health", dependencies=read)(handler)         # the read guard on an operator path

    toy = FastAPI()                                                 # with FastAPI's documentation routes
    for part in (router, keyed, reads):
        toy.include_router(part)
    toy.add_api_route("/open", handler, methods=["GET", "DELETE"])  # registered on the application
    toy.mount("/static", Starlette())

    uncovered, under_authorised, stale, total = scan(
        toy, {"GET /status": "the decorator's path, not the served one", "GET /docs": "declared"})
    expect(uncovered == sorted([
        "GET /api/v1/admin/status", "POST /api/v1/admin/added", "GET /api/v1/admin/namesake",
        "* <Route /plain: served path not resolved>", "GET /open", "DELETE /open", "* /static",
        "GET /openapi.json", "GET /docs/oauth2-redirect", "GET /redoc",
    ]), f"every route without a guard is reported at the path it is served at, got {uncovered}")
    expect(under_authorised == ["GET /api/v1/alerts/health", "POST /api/v1/score"],
           f"the read guard alone covers a GET outside the operator paths only, got {under_authorised}")
    expect(stale == ["GET /status"], f"an entry for a path that is not served is stale, got {stale}")
    expect(total == 18, f"18 method+path pairs are served, {total} were walked")

    print("   OK selftest: served routes (prefix, add_api_route, application, mount, documentation), "
          "guards (signature, decorator, router, nested, not by name), read guard, stale entry")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    # The object the server runs (`uvicorn app.main:app`). Importing it builds
    # the application and runs no lifespan: no migration, scheduler or network.
    from app.main import app as application

    uncovered, under_authorised, stale, total = scan(application)
    rc = 0
    if uncovered:
        print("BLOCK authz coverage (C-01): served with no authorisation dependency "
              "and no PUBLIC_ALLOWLIST entry:", file=sys.stderr)
        for key in uncovered:
            print(f"  {key}", file=sys.stderr)
        print("  Add the dependency, or add the route to PUBLIC_ALLOWLIST with the "
              "reason its OUTPUT is safe to publish.", file=sys.stderr)
        rc = 1
    if under_authorised:
        print("BLOCK authz coverage: only require_read_access, which lets every caller in "
              "while reads are public, on a route that is not a public read:", file=sys.stderr)
        for key in under_authorised:
            print(f"  {key}", file=sys.stderr)
        print("  A write and every route under /api/v1/admin or /api/v1/alerts takes "
              "require_admin_key or require_alerts_write.", file=sys.stderr)
        rc = 1
    if stale:
        print("BLOCK authz coverage: PUBLIC_ALLOWLIST names routes that are not "
              "served — a standing permission the next route on that path inherits:",
              file=sys.stderr)
        for key in stale:
            print(f"  {key}", file=sys.stderr)
        rc = 1
    if rc == 0:
        print(f"Authz coverage: {total} routes — "
              f"{total - len(PUBLIC_ALLOWLIST)} by dependency, "
              f"{len(PUBLIC_ALLOWLIST)} public by decision.")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
