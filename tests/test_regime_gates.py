"""The Level 2 regime gates, under pytest.

Each gate carries its own `--selftest` so it can be exercised in CI without a
test runner; this suite runs those selftests AND pins the behaviour that
matters most — that every gate fails CLOSED. A gate that returns 0 when it
cannot evaluate its input is the exact defect the mandate calls decorative.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from starlette.applications import Starlette

from app import security
from scripts.regime import authz_coverage, findings_gate, separation_check

ROOT = Path(__file__).resolve().parents[1]
GATES = ("findings_gate", "separation_check", "authz_coverage")


@pytest.mark.parametrize("gate", GATES)
def test_selftest_passes(gate: str) -> None:
    out = subprocess.run([sys.executable, "-m", f"scripts.regime.{gate}", "--selftest"],
                         capture_output=True, text=True, cwd=ROOT, check=False)
    assert out.returncode == 0, f"{gate} --selftest failed:\n{out.stdout}\n{out.stderr}"


@pytest.mark.parametrize("gate", GATES)
def test_gate_passes_against_the_real_repository(gate: str) -> None:
    """The reporting mode must be green on the tree as committed.

    findings_gate's default mode reports rather than blocks (the repair lane);
    the separation and authz gates block, and must be satisfied here."""
    out = subprocess.run([sys.executable, "-m", f"scripts.regime.{gate}"],
                         capture_output=True, text=True, cwd=ROOT, check=False)
    assert out.returncode == 0, f"{gate} blocked on the committed tree:\n{out.stdout}\n{out.stderr}"


class TestFindingsGateComputesRatherThanAsserts:
    """`production_eligible` may only ever be computed. It was previously a
    hard-coded literal in two files, both deleted."""

    def test_open_blocker_makes_it_false(self):
        t = findings_gate.tally([{"id": "X", "verdict": "FAIL", "band": "BLOCKER-1"}])
        eligible, why = findings_gate.production_eligible(t)
        assert eligible is False and "BLOCKER-1" in why

    def test_escalated_band_overrides_the_base_band(self):
        # A-01 sits at BLOCKER-1 and A-39 at PLAN, but both carry an escalated
        # STOP-SHIP. Reading only `band` reports zero STOP-SHIP while two are open.
        rec = {"id": "A-39", "verdict": "FAIL", "band": "PLAN",
               "escalated_band": "STOP-SHIP (A-01+A-39)"}
        assert findings_gate.effective_band(rec) == "STOP-SHIP"

    def test_unreadable_band_blocks_rather_than_passes(self):
        t = findings_gate.tally([{"id": "X", "verdict": "FAIL", "band": "banana"}])
        eligible, why = findings_gate.production_eligible(t)
        assert eligible is False and "unrecognised" in why

    def test_partial_counts_as_open(self):
        t = findings_gate.tally([{"id": "X", "verdict": "PARTIAL", "band": "STOP-SHIP"}])
        assert t["open_by_band"]["STOP-SHIP"] == 1

    def test_the_real_ledger_is_not_eligible_and_says_why(self):
        t = findings_gate.tally(findings_gate.load_findings())
        eligible, why = findings_gate.production_eligible(t)
        assert eligible is False, "blocking findings are open; this must not compute true"
        assert why, "a refusal must carry its reason"

    def test_admission_refuses_on_the_real_ledger(self):
        out = subprocess.run([sys.executable, "-m", "scripts.regime.findings_gate", "--admission"],
                             capture_output=True, text=True, cwd=ROOT, check=False)
        assert out.returncode == 1
        assert "REFUSE deploy admission" in out.stderr

    def test_an_unreadable_ledger_refuses_rather_than_admits(self, tmp_path, monkeypatch):
        bad = tmp_path / "03-findings.json"
        bad.write_text("{ not json", encoding="utf-8")
        monkeypatch.setattr(findings_gate, "FINDINGS_PATH", bad)
        assert findings_gate.main(["--admission"]) == 1

    def test_a_ledger_that_is_not_a_list_refuses(self, tmp_path, monkeypatch):
        bad = tmp_path / "03-findings.json"
        bad.write_text(json.dumps({"A-01": {}}), encoding="utf-8")
        monkeypatch.setattr(findings_gate, "FINDINGS_PATH", bad)
        assert findings_gate.main(["--admission"]) == 1


class TestSeparationCheck:
    def test_a_pattern_without_an_owner_is_not_coverage(self):
        # CODEOWNERS uses an ownerless pattern to REMOVE ownership; counting it
        # as coverage would invert its meaning.
        assert separation_check.owned_patterns("/scripts/") == []

    def test_prefix_matching_respects_the_separator(self):
        assert not separation_check.covers("/scripts/", "scripts_other/x.py")

    def test_missing_codeowners_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(separation_check, "CODEOWNERS", tmp_path / "nope")
        assert separation_check.main([]) == 1

    def test_every_protected_surface_is_currently_owned(self):
        text = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        assert separation_check.uncovered(text) == []


def _open() -> dict:
    return {}


def _served_application():
    """The application as `create_app` builds it: a copy a test may add routes to."""
    from app.main import create_app

    return create_app()


def _bare_application(*routers: APIRouter) -> FastAPI:
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    for router in routers:
        application.include_router(router)
    return application


class TestAuthzCoverage:
    """The gate's claim (scripts/regime/authz_coverage.py): every route the
    application serves carries an authorisation dependency or is declared
    public, and none becomes public quietly. SECURITY.md: the admin routes and
    every alert route take the key; the other reads are public.

    The gate read the decorators in app/routers/*.py and keyed a route by the
    decorator's path. Executed on 9f79c7a, each of these passed it (exit 0)
    while the route answered 200 without a key: a POST registered on the
    application in app/main.py; a keyless GET "/status" in the admin router,
    served at /api/v1/admin/status and taken for the allowlisted "GET /status";
    and POST /api/v1/admin/send-sms moved to the read dependency, which lets
    everyone in while READ_ENDPOINTS_PUBLIC is true (the default). The first
    three tests below make these three kinds of change to the application the
    gate judges.

    Two tests left with their subject: that a decorator inside a string is no
    route (no source is parsed any more), and that the public list and the
    in-handler list do not overlap (the second list is gone; the reason stands
    in the gate)."""

    def _blocked(self, monkeypatch, capsys, application) -> str:
        """Run the gate on `application` as the served one; it must block."""
        import app.main

        monkeypatch.setattr(app.main, "app", application)
        assert authz_coverage.main([]) == 1, "the gate passed"
        return capsys.readouterr().err

    def test_a_route_registered_on_the_application_is_seen(self, monkeypatch, capsys):
        application = _served_application()
        application.add_api_route("/api/v1/admin/open", _open, methods=["POST"])
        assert "POST /api/v1/admin/open" in self._blocked(monkeypatch, capsys, application)

    def test_a_route_is_judged_at_the_path_it_is_served_at(self, monkeypatch, capsys):
        # "GET /status" is declared public: the status page. The same decorator
        # path under the admin prefix is another route.
        assert "GET /status" in authz_coverage.PUBLIC_ALLOWLIST
        router = APIRouter(prefix="/api/v1/admin")
        router.get("/status")(_open)
        application = _served_application()
        application.include_router(router)
        assert "GET /api/v1/admin/status" in self._blocked(monkeypatch, capsys, application)

    def test_the_read_dependency_does_not_authorise_a_write(self, monkeypatch, capsys):
        application = _served_application()
        application.add_api_route("/api/v1/score/recompute", _open, methods=["POST"],
                                  dependencies=[Depends(security.require_read_access)])
        assert "POST /api/v1/score/recompute" in self._blocked(monkeypatch, capsys, application)

    def test_the_read_dependency_does_not_authorise_an_operator_path(self):
        read = [Depends(security.require_read_access)]
        application = _bare_application()
        application.add_api_route("/api/v1/admin/refresh/status", _open, dependencies=read)
        application.add_api_route("/api/v1/alerts/renders/{render_id}", _open, dependencies=read)
        application.add_api_route("/api/v1/score", _open, dependencies=read)
        application.add_api_route("/api/v1/alerts/health", _open,
                                  dependencies=[Depends(security.require_admin_key)])
        uncovered, under_authorised, _, total = authz_coverage.scan(application, {})
        assert under_authorised == ["GET /api/v1/admin/refresh/status",
                                    "GET /api/v1/alerts/renders/{render_id}"]
        assert uncovered == [] and total == 4

    def test_every_served_route_is_authorised_or_declared(self):
        from app.main import app as application

        uncovered, under_authorised, stale, _ = authz_coverage.scan(application)
        assert uncovered == [], f"served without authorisation and not declared public: {uncovered}"
        assert under_authorised == [], f"only the read dependency on a write or an operator path: {under_authorised}"
        assert stale == [], f"declared public, served nowhere: {stale}"
        # Every operation FastAPI's schema lists must be among the routes
        # judged: what the gate makes of a walked route (its path, its methods)
        # cannot drop one from the judgement unnoticed.
        judged = {key for key, _ in authz_coverage.served(application)}
        in_schema = {f"{method.upper()} {path}"
                     for path, operations in application.openapi()["paths"].items() for method in operations}
        assert in_schema and in_schema <= judged, sorted(in_schema - judged)

    def test_add_api_route_on_a_router_is_seen(self):
        router = APIRouter(prefix="/api/v1/meta")
        router.add_api_route("/dump", _open, methods=["GET"])
        assert authz_coverage.scan(_bare_application(router), {})[0] == ["GET /api/v1/meta/dump"]

    def test_a_mounted_application_is_not_silently_public(self):
        application = _bare_application()
        application.mount("/static", Starlette())
        assert authz_coverage.scan(application, {})[0] == ["* /static"]

    def test_a_dependency_that_is_not_an_authorisation_does_not_count(self):
        def require_admin_key() -> None:
            """Another function under the guard's name: not the guard."""

        application = _bare_application()
        application.add_api_route("/a", _open, dependencies=[Depends(_open)])
        application.add_api_route("/b", _open, dependencies=[Depends(require_admin_key)])
        application.add_api_route("/c", _open, dependencies=[Depends(security.require_admin_key)])
        assert authz_coverage.scan(application, {})[0] == ["GET /a", "GET /b"]

    def test_a_stale_allowlist_entry_blocks(self, monkeypatch, capsys):
        # An entry for a route that is not served is a standing permission the
        # next route at that path inherits.
        monkeypatch.setitem(authz_coverage.PUBLIC_ALLOWLIST, "GET /api/v1/gone",
                            "a route that is not served any more")
        assert "GET /api/v1/gone" in self._blocked(monkeypatch, capsys, _served_application())

    def test_every_declared_exception_carries_a_reason(self):
        for key, reason in authz_coverage.PUBLIC_ALLOWLIST.items():
            assert reason and len(reason) > 20, f"PUBLIC_ALLOWLIST[{key!r}] has no substantive reason"

    def test_the_documentation_routes_are_public_by_decision(self):
        """FastAPI serves the schema and its two viewers on every application
        unless told otherwise. They answered 200 without a key and stood in no
        list; the status page links all three (README.md, "Status & spec UI"),
        so they are declared, each with its reason, not switched off."""
        from app.main import app as application

        documentation = {"GET /openapi.json", "GET /docs", "GET /docs/oauth2-redirect", "GET /redoc"}
        assert documentation <= authz_coverage.PUBLIC_ALLOWLIST.keys(), \
            sorted(documentation - authz_coverage.PUBLIC_ALLOWLIST.keys())
        served = dict(authz_coverage.served(application))
        assert all(served[key] == frozenset() for key in documentation)


class TestStrictModeIsActuallyReachable:
    """`--strict` is the findings gate's END STATE, and until now nothing drove it.

    Mutation against the pre-existing coverage (`--selftest` plus this file):
    deleting the blocking-band clause, the unrecognised-verdict clause, the
    PASS-without-control clause, or the entire `--strict` block, each left
    selftest=0 and 23 tests passing. The mode CI does not run and no test
    exercises is a mode that can be deleted by accident.

    ci.yml deliberately runs the reporting mode, not `--strict`, while 4
    STOP-SHIP and 11 BLOCKER-1 findings are open -- a gate that can never pass
    is a gate someone removes under pressure. That is a scheduling decision;
    it is not a reason for the code path to be untested.
    """

    import json as _json

    @staticmethod
    def _gate_with(tmp_path, monkeypatch, records):
        import json

        from scripts.regime import findings_gate as fg
        path = tmp_path / "03-findings.json"
        path.write_text(json.dumps(records), encoding="utf-8")
        # load_findings binds FINDINGS_PATH as a DEFAULT ARGUMENT, so patching
        # the module attribute does not reach it -- the gate reads the real
        # 119-record ledger and the assertions silently describe production
        # instead of the fixture. Patching load_findings is what redirects it,
        # and that binding is a fair part of why --strict went uncovered.
        monkeypatch.setattr(fg, "FINDINGS_PATH", path)
        monkeypatch.setattr(fg, "load_findings", lambda p=path: json.loads(p.read_text()))
        return fg

    def test_strict_blocks_on_an_open_blocking_band(self, tmp_path, monkeypatch, capsys):
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "FAIL", "band": "STOP-SHIP"}])
        assert fg.main(["--strict"]) == 1
        assert "STOP-SHIP/BLOCKER-1" in capsys.readouterr().err

    def test_strict_blocks_on_an_unrecognised_verdict(self, tmp_path, monkeypatch, capsys):
        # The clause this branch added, and the one that shipped uncovered.
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "FAILED", "band": "MUST-FIX"}])
        assert fg.main(["--strict"]) == 1
        assert "unrecognised verdict" in capsys.readouterr().err

    def test_strict_blocks_on_a_pass_without_a_standing_control(self, tmp_path, monkeypatch, capsys):
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "PASS", "band": "PLAN"}])
        assert fg.main(["--strict"]) == 1
        assert "standing control" in capsys.readouterr().err

    def test_strict_passes_a_clean_ledger(self, tmp_path, monkeypatch):
        # The complement: three blocking clauses that refuse everything would
        # satisfy the tests above and be useless.
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "PASS", "band": "PLAN", "standing_control": "ci"},
            {"id": "A-2", "verdict": "NOT-APPLICABLE", "band": "PLAN", "tripwire": "n/a"}])
        assert fg.main(["--strict"]) == 0

    def test_the_reporting_mode_stays_non_blocking_on_the_same_ledger(self, tmp_path, monkeypatch):
        # The repair lane: honest while findings are still open, exit 0.
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "FAIL", "band": "STOP-SHIP"}])
        assert fg.main([]) == 0

    def test_admission_still_refuses_where_strict_does(self, tmp_path, monkeypatch, capsys):
        fg = self._gate_with(tmp_path, monkeypatch, [
            {"id": "A-1", "verdict": "FAIL", "band": "STOP-SHIP"}])
        assert fg.main(["--admission"]) == 1
        assert "REFUSE deploy admission" in capsys.readouterr().err
