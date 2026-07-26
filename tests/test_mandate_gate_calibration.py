"""Mandate gate: the S12 seeded-defect calibration and the audit surface.

The six calibration classes re-prove that the live gates still catch what
they exist to catch; the surface is the audit denominator every coverage
claim divides by."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.mandate_gate_support import (
    clear_ambient_ci_env,
    mg,
)
from tests.mandate_gate_support import (
    reattest as _reattest,
)
from tests.mandate_gate_support import (
    write_minimal_engagement as _write_minimal_engagement,
)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    clear_ambient_ci_env(monkeypatch)


class TestPanelFindingsRound25:
    """Four scope escapes: three calibration checks that looked at a narrower
    slice of the repository than the claim they re-validate, and an orphan
    prune that never ran on the change that creates the orphan."""

    # --- the acceptance orphan prune ---------------------------------------

    def _accept_fixture(self, monkeypatch, tmp_path, *, accepted, prev_accepted):
        """A02 open blocker; the register carries a record for A-02 while
        `accepted` decides whether A-02 is still in the accepted id list."""
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=True)
        reg = tmp_path / "governance" / "accepted-residuals.json"
        d = json.loads(reg.read_text())
        d["accepted_open_findings"] = accepted
        reg.write_text(json.dumps(d))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        monkeypatch.setattr(
            mg, "previous_version",
            lambda p: ({"accepted_open_findings": prev_accepted,
                        "acceptance_records": [{"finding_id": "A-02"}]}
                       if "accepted-residuals" in p else None))

    def test_orphan_record_blocks_when_no_id_is_newly_accepted(
            self, monkeypatch, tmp_path):
        # THE EXPLOIT: close A-02 and drop it from the accepted list while
        # KEEPING its acceptance record. Nothing is newly accepted, so `newly`
        # was empty and the prune never ran; A-02 is no longer an open blocker,
        # so no later guard has anything to say either. The pre-fix gate went
        # FULLY GREEN here, leaving a record that pre-authorises the next
        # acceptance of A-02 without a fresh operator decision.
        #
        # Verified against the pre-fix gate: the naive form of this test (leave
        # A-02 open) also raises SystemExit there — from the unaccepted-blocker
        # guard, not the prune — so it would have passed for the wrong reason.
        # Hence both the closure and the assertion on WHICH guard fired.
        self._accept_fixture(monkeypatch, tmp_path,
                             accepted=[], prev_accepted=["A-02"])
        fp = tmp_path / "audit" / "03-findings.json"
        fs = json.loads(fp.read_text())
        for f in fs:
            if f["id"] == "A-02":
                f["verdict"] = "PASS"
                f["standing_control"] = {
                    "mechanism": "blocking CI job runs the gate every change",
                    "demonstrated": "observed blocking a seeded defect"}
        fp.write_text(json.dumps(fs))
        _reattest(tmp_path)
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), pytest.raises(SystemExit) as e:
            mg.compute_status()
        assert e.value.code == 1
        assert "acceptance_records exist" in err.getvalue()

    def test_orphan_prune_passes_when_the_record_is_pruned_too(
            self, monkeypatch, tmp_path):
        # the positive path: drop the id AND its record and the register is
        # consistent again (A-02 then fails as an unaccepted open blocker,
        # which is the correct different complaint — not the orphan one)
        self._accept_fixture(monkeypatch, tmp_path,
                             accepted=[], prev_accepted=["A-02"])
        reg = tmp_path / "governance" / "accepted-residuals.json"
        d = json.loads(reg.read_text())
        d["acceptance_records"] = []
        reg.write_text(json.dumps(d))
        _reattest(tmp_path)
        with pytest.raises(SystemExit):
            mg.compute_status()
        # prove WHICH guard fired: the orphan message must be gone
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), pytest.raises(SystemExit):
            mg.compute_status()
        assert "acceptance_records exist" not in err.getvalue()
        assert "NOT in the accepted-residuals register" in err.getvalue()

    def test_a_consistent_register_still_passes(self, monkeypatch, tmp_path):
        # the unconditional prune must not break the ordinary steady state
        self._accept_fixture(monkeypatch, tmp_path,
                             accepted=["A-02"], prev_accepted=["A-02"])
        mg.compute_status()          # does not raise

    def _calib_error(self) -> str:
        """Run cmd_calibrate expecting refusal, and return the message — an
        assertion that it merely exited says nothing about WHICH check caught
        it, and calibrate has many ways to fail."""
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), pytest.raises(SystemExit):
            mg.cmd_calibrate()
        return err.getvalue()

    # --- class 5: tool enablement ------------------------------------------

    def _calibrate_app(self, monkeypatch, tmp_path, files: dict):
        """A root cmd_calibrate can actually run against: an app/ tree, a
        pyproject for the dependency check, and an empty tests/ tree."""
        app = tmp_path / "app"
        app.mkdir(parents=True, exist_ok=True)
        for name, src in files.items():
            (app / name).write_text(src)
        (tmp_path / "tests").mkdir(exist_ok=True)
        (tmp_path / "scripts").mkdir(exist_ok=True)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "t"\nversion = "0"\n'
            'dependencies = ["anthropic"]\n'      # LLM-path fixtures import it
            # class-2 config check (P1.1) reads this: S must stay selected
            '[tool.ruff.lint]\nselect = ["E", "F", "S"]\nignore = ["E501"]\n')
        # a REAL git repo: the credential scan derives its file set from
        # `git ls-files` and fails closed when that cannot run, so a fixture
        # that is not a repo would exercise the error path instead of the
        # check under test
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        monkeypatch.setattr(mg, "ROOT", tmp_path)

    def test_class5_catches_a_subscript_built_tools_payload(
            self, monkeypatch, tmp_path):
        # THE EXPLOIT: no `tools=` kwarg and no `{"tools": ...}` literal, so
        # both original forms saw nothing — yet the request carries tools.
        self._calibrate_app(monkeypatch, tmp_path, {"llm.py": (
            'def call(msgs):\n'
            '    payload = {"model": "m", "messages": msgs}\n'
            '    payload["tools"] = [{"name": "shell"}]\n'
            '    return client.post("/v1/messages", json=payload)\n')})
        assert "class 5 N/A no longer holds" in self._calib_error()

    def test_class5_catches_kwargs_expansion_into_raw_http(
            self, monkeypatch, tmp_path):
        # the SDK-name list did not include the transport underneath it
        self._calibrate_app(monkeypatch, tmp_path, {"llm.py": (
            'def call(msgs, extra):\n'
            '    return client.post("/v1/messages", **extra)\n')})
        assert "expands **kwargs into a model call" in self._calib_error()

    def test_class5_passes_on_a_toolless_explicit_call(
            self, monkeypatch, tmp_path):
        # BOTH SIDES: the shape this repo actually uses must stay green, or
        # the check is just a blanket refusal
        self._calibrate_app(monkeypatch, tmp_path, {"llm.py": (
            'def call(msgs):\n'
            '    return client.messages.create(model="m", max_tokens=8,\n'
            '                                  messages=msgs)\n')})
        mg.cmd_calibrate()           # does not raise

    # --- class 6: tenancy, anywhere in app/ --------------------------------

    def test_class6_catches_a_tenant_model_outside_models_py(
            self, monkeypatch, tmp_path):
        # THE EXPLOIT: the scan read app/models.py only, so the same
        # declaration one file over preserved the NOT-APPLICABLE verdict.
        self._calibrate_app(monkeypatch, tmp_path, {
            "models.py": 'class Snapshot(Base):\n    __tablename__ = "s"\n',
            "auth_models.py": (
                'class TenantMembership(Base):\n'
                '    __tablename__ = "tenant_membership"\n'
                '    user_id = mapped_column(ForeignKey("users.id"))\n')})
        err = self._calib_error()
        assert "class 6 N/A no longer holds" in err
        assert "auth_models.py" in err     # names the file it actually found

    def test_class6_ignores_modules_that_declare_no_orm_structure(
            self, monkeypatch, tmp_path):
        # BOTH SIDES: the claim is about TABLES. A helper that merely mentions
        # a user cannot define one, and flagging it would be a false alarm
        # that trains the operator to ignore the gate.
        self._calibrate_app(monkeypatch, tmp_path, {
            "models.py": 'class Snapshot(Base):\n    __tablename__ = "s"\n',
            "notify.py": ('def message(owner_id, account_id):\n'
                          '    return f"{owner_id}/{account_id}"\n')})
        mg.cmd_calibrate()           # does not raise

    # --- class 1: the tests/ tree ------------------------------------------

    def test_class1_live_scan_covers_the_tests_tree(self):
        # THE EXPLOIT: tests/ has a blanket ruff-S per-file-ignore and
        # detect-secrets' UUID blind spot, so it was the one tree where a
        # pasted credential met no gate at all.
        assert any("tests/" in str(p)
                   for p in ([*mg.ROOT.glob("app/**/*.py")]
                             + [*mg.ROOT.glob("scripts/*.py")]
                             + [*mg.ROOT.glob("tests/**/*.py")]))
        seeded = mg.ROOT / "tests" / "__calib_probe.py"
        try:
            seeded.write_text('sipgate_token = "4ed251b5-8a9c'   # pragma: allowlist secret
                              '-4b3e-9f21-7c6d5e4a3b21"\n')
            assert mg.scan_credential_shapes([seeded])
        finally:
            seeded.unlink()

    def test_the_live_tests_tree_is_clean_under_the_scanner(self):
        # the annotated fixtures must be the ONLY reason it is clean: every
        # exemption is a visible pragma on the offending line, not a tree-wide
        # exclusion
        assert mg.scan_credential_shapes(
            list(Path(mg.ROOT).glob("tests/**/*.py"))) == []


class TestRound25FollowOnSweep:
    """Own sweep for the NEXT variant of each round-25 class, because the
    recurring pattern in this PR has been that a round finds the defect in the
    previous round's fix."""

    def _calibrate_app(self, monkeypatch, tmp_path, files: dict):
        return TestPanelFindingsRound25._calibrate_app(
            TestPanelFindingsRound25(), monkeypatch, tmp_path, files)

    def _calib_error(self):
        return TestPanelFindingsRound25._calib_error(TestPanelFindingsRound25())

    def test_class5_catches_setdefault_which_is_none_of_the_known_forms(
            self, monkeypatch, tmp_path):
        # not a kwarg, not a dict key, not a subscript — the three shapes the
        # check had accumulated. Enumerating forms is how it kept losing.
        self._calibrate_app(monkeypatch, tmp_path, {"llm.py": (
            'def call(msgs, payload):\n'
            '    payload.setdefault("tools", [{"name": "shell"}])\n'
            '    return client.post("/v1/messages", json=payload)\n')})
        assert "class 5 N/A no longer holds" in self._calib_error()

    def test_class5_prose_about_tools_does_not_fire(
            self, monkeypatch, tmp_path):
        # BOTH SIDES: the text-scan ancestor of this check tripped on the
        # repo's own comment describing the invariant. The AST must not.
        self._calibrate_app(monkeypatch, tmp_path, {"llm.py": (
            '"""The judgment path passes no tools to the model."""\n'
            '# tools are never enabled here; see audit/03-findings.json\n'
            'def call(msgs):\n'
            '    return client.messages.create(model="m", messages=msgs)\n')})
        mg.cmd_calibrate()           # does not raise

    def test_class6_catches_a_tenancy_table_created_in_a_migration(
            self, monkeypatch, tmp_path):
        # migrations are where tables are actually created, so this introduces
        # real multi-tenancy without touching app/ at all
        (tmp_path / "migrations" / "versions").mkdir(parents=True)
        (tmp_path / "migrations" / "versions" / "0007_x.py").write_text(
            'def upgrade():\n'
            '    op.create_table("tenants", sa.Column("tenant_id"))\n')
        self._calibrate_app(monkeypatch, tmp_path, {
            "models.py": 'class Snapshot(Base):\n    __tablename__ = "s"\n'})
        err = self._calib_error()
        assert "class 6 N/A no longer holds" in err
        assert "0007_x.py" in err

    def test_class1_scan_set_is_every_tracked_python_file(
            self, monkeypatch, tmp_path):
        # the hand-listed trees missed tests/, migrations/ and docs/harnesses/;
        # deriving from git ls-files retires that whole class
        self._calibrate_app(monkeypatch, tmp_path, {"main.py": "x = 1\n"})
        (tmp_path / "migrations").mkdir(exist_ok=True)
        seeded = tmp_path / "migrations" / "0007_x.py"
        seeded.write_text('sipgate_token = "4ed251b5-8a9c'   # pragma: allowlist secret
                          '-4b3e-9f21-7c6d5e4a3b21"\n')
        import subprocess
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        assert "credential-shape scanner fired" in self._calib_error()

    def test_class1_untracked_file_is_not_scanned_but_tracked_one_is(
            self, monkeypatch, tmp_path):
        # BOTH SIDES: the denominator is the TRACKED set, so an untracked
        # scratch file is out of scope by definition (it ships nowhere)
        self._calibrate_app(monkeypatch, tmp_path, {"main.py": "x = 1\n"})
        scratch = tmp_path / "scratch_probe.py"
        scratch.write_text('sipgate_token = "4ed251b5-8a9c'   # pragma: allowlist secret
                           '-4b3e-9f21-7c6d5e4a3b21"\n')
        mg.cmd_calibrate()           # untracked: does not raise

    def test_class1_fails_closed_when_the_file_set_cannot_be_established(
            self, monkeypatch, tmp_path):
        # an unknown denominator is a FAILED check, never a clean one
        self._calibrate_app(monkeypatch, tmp_path, {"main.py": "x = 1\n"})

        class R:
            returncode = 128
            stdout = ""
            stderr = "fatal: not a git repository"
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: R())
        assert "git ls-files failed" in self._calib_error()

    def test_acceptance_records_must_be_a_list(self, monkeypatch, tmp_path):
        # a MAPPING iterates its keys, so every `isinstance(r, dict)` filter
        # sees zero records and every per-record check passes vacuously
        _write_minimal_engagement(tmp_path, pass_has_control=True,
                                  blocker_accepted=True)
        reg = tmp_path / "governance" / "accepted-residuals.json"
        d = json.loads(reg.read_text())
        d["acceptance_records"] = {"A-02": {"reason": "x" * 30,
                                            "authorised_by": "operator"}}
        reg.write_text(json.dumps(d))
        _reattest(tmp_path)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")
        monkeypatch.setattr(mg, "GOV", tmp_path / "governance")
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), pytest.raises(SystemExit):
            mg.compute_status()
        assert "must be a LIST" in err.getvalue()


class TestSurfaceDriftCheck:
    """The audit denominator is a computed property. Nothing verified the
    committed copy, and the repo carried one reading `files_tracked: 2` —
    fixture content leaked by a test written before the surface tests were
    made hermetic. Stale coverage denominators fail quietly, which is worse
    than failing loudly."""

    def _repo(self, monkeypatch, tmp_path, tracked):
        (tmp_path / "audit").mkdir(exist_ok=True)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")

        class R:
            returncode = 0
            stdout = "\0".join(tracked) + "\0"
            stderr = ""
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: R())

    def test_check_passes_on_a_freshly_generated_surface(self, monkeypatch, tmp_path):
        self._repo(monkeypatch, tmp_path, ["app/a.py", "app/b.py"])
        mg.cmd_surface()
        mg.cmd_surface(check=True)          # does not raise

    def test_check_blocks_a_stale_denominator(self, monkeypatch, tmp_path):
        self._repo(monkeypatch, tmp_path, ["app/a.py", "app/b.py"])
        mg.cmd_surface()
        self._repo(monkeypatch, tmp_path,
                   ["app/a.py", "app/b.py", "app/c.py"])   # repo grew
        with pytest.raises(SystemExit):
            mg.cmd_surface(check=True)

    def test_check_blocks_a_hand_edited_surface(self, monkeypatch, tmp_path):
        self._repo(monkeypatch, tmp_path, ["app/a.py", "app/b.py"])
        mg.cmd_surface()
        out = tmp_path / "audit" / "00-audit-surface.json"
        d = json.loads(out.read_text())
        d["files_tracked"] = 999
        out.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
        with pytest.raises(SystemExit):
            mg.cmd_surface(check=True)

    def test_check_blocks_a_missing_surface(self, monkeypatch, tmp_path):
        self._repo(monkeypatch, tmp_path, ["app/a.py"])
        with pytest.raises(SystemExit):
            mg.cmd_surface(check=True)

    def test_check_never_writes(self, monkeypatch, tmp_path):
        self._repo(monkeypatch, tmp_path, ["app/a.py"])
        out = tmp_path / "audit" / "00-audit-surface.json"
        out.write_text("{}\n")
        with pytest.raises(SystemExit):
            mg.cmd_surface(check=True)
        assert out.read_text() == "{}\n"   # a check that repairs is not a check

    def test_live_surface_matches_the_repository(self):
        # regression: the committed denominator was fixture content
        surface = json.loads(
            (Path(mg.ROOT) / "audit" / "00-audit-surface.json").read_text())
        assert surface["files_tracked"] > 100
        assert "app/main.py" in surface["python_modules"]

class TestPanelFindingsRound27:
    """Three checks that were narrower than the property they assert: the
    replay window, the tools-constant match, and the route inventory."""

    def _calib(self, monkeypatch, tmp_path, files):
        return TestPanelFindingsRound25._calibrate_app(
            TestPanelFindingsRound25(), monkeypatch, tmp_path, files)

    def _err(self):
        return TestPanelFindingsRound25._calib_error(TestPanelFindingsRound25())

    # --- class 5: folded and unreadable keys -------------------------------

    def test_concatenated_tools_key_is_folded_and_caught(
            self, monkeypatch, tmp_path):
        # THE EXPLOIT: no "tools" constant exists anywhere in the tree
        self._calib(monkeypatch, tmp_path, {"llm.py": (
            'def call(msgs):\n'
            '    payload = {"model": "m", "messages": msgs}\n'
            '    payload["to" + "ols"] = [{"name": "shell"}]\n'
            '    return client.post("/v1/messages", json=payload)\n')})
        assert "class 5 N/A no longer holds" in self._err()

    def test_fstring_composed_tools_key_is_folded_and_caught(
            self, monkeypatch, tmp_path):
        self._calib(monkeypatch, tmp_path, {"llm.py": (
            'def call(payload):\n'
            '    payload[f"to{\'ols\'}"] = []\n')})
        assert "class 5 N/A no longer holds" in self._err()

    def test_dynamic_key_write_in_the_llm_module_is_refused(
            self, monkeypatch, tmp_path):
        # "".join(...) and chr() arithmetic cannot be folded, and enumerating
        # obfuscations is the losing game. In the module that calls a model,
        # a key the AST cannot read is refused outright.
        self._calib(monkeypatch, tmp_path, {"judgment.py": (
            'import anthropic\n'
            'def call(payload, key):\n'
            '    payload["".join(["to", "ols"])] = []\n'
            '    return client.messages.create(model="m", messages=[])\n')})
        assert "builds a mapping key dynamically" in self._err()

    def test_dynamic_key_outside_the_llm_module_is_left_alone(
            self, monkeypatch, tmp_path):
        # BOTH SIDES: dynamic mapping writes are ordinary everywhere else (16
        # live modules do it). A check that fires on all of them is one the
        # operator learns to wave through.
        self._calib(monkeypatch, tmp_path, {
            "compute.py": ('def build(rows, key):\n'
                           '    out = {}\n'
                           '    out[key] = rows\n'
                           '    return out\n')})
        mg.cmd_calibrate()           # does not raise

    def test_llm_module_with_only_literal_keys_passes(
            self, monkeypatch, tmp_path):
        # the shape app/engine/judgment.py actually uses must stay green
        self._calib(monkeypatch, tmp_path, {"judgment.py": (
            'import anthropic\n'
            'def call(msgs):\n'
            '    return client.messages.create(model="m", max_tokens=8,\n'
            '                                  messages=msgs)\n')})
        mg.cmd_calibrate()           # does not raise

    def test_live_judgment_module_has_no_dynamic_mapping_writes(self):
        # the live invariant this rule pins
        mg.cmd_calibrate()           # does not raise on the real repo

    # --- the route inventory ----------------------------------------------

    def test_surface_records_patch_and_non_router_decorators(
            self, monkeypatch, tmp_path):
        # THE EXPLOIT: a PATCH endpoint, or an @app.get in main.py, was live
        # traffic the audit denominator did not know existed.
        (tmp_path / "audit").mkdir()
        (tmp_path / "app" / "routers").mkdir(parents=True)
        (tmp_path / "app" / "main.py").write_text(
            '@app.get("/root-level")\ndef r(): ...\n')
        (tmp_path / "app" / "routers" / "admin.py").write_text(
            '@router.patch("/admin/thing")\ndef p(): ...\n'
            '@router.head("/admin/ping")\ndef h(): ...\n'
            '@router.get("/admin/ok")\ndef g(): ...\n')
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")

        class R:
            returncode = 0
            stdout = "app/main.py\0app/routers/admin.py\0"
            stderr = ""
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: R())
        mg.cmd_surface()
        surface = json.loads(
            (tmp_path / "audit" / "00-audit-surface.json").read_text())
        found = {(r["method"], r["path"]) for r in surface["routes"]}
        assert ("PATCH", "/admin/thing") in found
        assert ("HEAD", "/admin/ping") in found
        assert ("GET", "/root-level") in found       # outside app/routers/
        assert ("GET", "/admin/ok") in found

    def test_live_surface_still_lists_every_route(self):
        surface = json.loads(
            (Path(mg.ROOT) / "audit" / "00-audit-surface.json").read_text())
        assert len(surface["routes"]) >= 19


class TestPanelFindingsRound29:
    """Four checks that were each one generalisation short: the fix landed on
    the reported instance and the adjacent case stayed open."""

    def _calib(self, monkeypatch, tmp_path, files):
        return TestPanelFindingsRound25._calibrate_app(
            TestPanelFindingsRound25(), monkeypatch, tmp_path, files)

    def _err(self):
        return TestPanelFindingsRound25._calib_error(TestPanelFindingsRound25())

    def test_raw_http_module_is_in_scope_for_the_dynamic_key_ban(
            self, monkeypatch, tmp_path):
        # THE EXPLOIT: no SDK import at all, so the ban did not apply — yet the
        # module addresses the model endpoint directly and can enable tools.
        self._calib(monkeypatch, tmp_path, {"llm_http.py": (
            'import httpx\n'
            'def call(msgs, payload):\n'
            '    payload["".join(["to", "ols"])] = [{"name": "shell"}]\n'
            '    return httpx.post("https://api.anthropic.com/v1/messages",\n'
            '                      json=payload)\n')})
        assert "builds a mapping key dynamically" in self._err()

    def test_module_touching_no_model_endpoint_is_untouched(
            self, monkeypatch, tmp_path):
        # BOTH SIDES: an ordinary HTTP client for a price feed keeps its
        # dynamic keys; widening scope must not become a blanket ban
        self._calib(monkeypatch, tmp_path, {"prices.py": (
            'import httpx\n'
            'def fetch(sym, out, key):\n'
            '    out[key] = httpx.get("https://stooq.com/q/d/l/")\n'
            '    return out\n')})
        mg.cmd_calibrate()           # does not raise

    def test_raw_sql_create_table_is_caught_without_orm_markers(
            self, monkeypatch, tmp_path):
        # THE EXPLOIT: a real per-user table, no ORM marker anywhere, so the
        # marker gate skipped the file and the N/A verdict survived
        self._calib(monkeypatch, tmp_path, {
            "models.py": 'class Snapshot(Base):\n    __tablename__ = "s"\n',
            "bootstrap.py": (
                'def init(conn):\n'
                '    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")\n')})
        err = self._err()
        assert "class 6 N/A no longer holds" in err
        assert "bootstrap.py" in err

    def test_raw_sql_create_table_of_a_normal_table_is_fine(
            self, monkeypatch, tmp_path):
        # BOTH SIDES: CREATE TABLE is not itself the signal — the tenancy
        # entity in its name is
        self._calib(monkeypatch, tmp_path, {
            "models.py": 'class Snapshot(Base):\n    __tablename__ = "s"\n',
            "bootstrap.py": (
                'def init(conn):\n'
                '    conn.execute("CREATE TABLE snapshots (id INTEGER)")\n')})
        mg.cmd_calibrate()           # does not raise

    def test_dotted_decorator_routes_reach_the_denominator(
            self, monkeypatch, tmp_path):
        # THE EXPLOIT: @api.router.get is an ordinary FastAPI form and the
        # bare-name pattern missed it, so the endpoint existed in traffic but
        # not in the audit surface
        (tmp_path / "audit").mkdir()
        (tmp_path / "app").mkdir(parents=True)
        (tmp_path / "app" / "api.py").write_text(
            '@api.router.get("/deep/nested")\ndef a(): ...\n'
            '@v1.router.patch("/deep/patched")\ndef b(): ...\n')
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")

        class R:
            returncode = 0
            stdout = "app/api.py\0"
            stderr = ""
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: R())
        mg.cmd_surface()
        found = {(r["method"], r["path"]) for r in json.loads(
            (tmp_path / "audit" / "00-audit-surface.json").read_text())["routes"]}
        assert ("GET", "/deep/nested") in found
        assert ("PATCH", "/deep/patched") in found


class TestRoutePathExtraction:
    """The decorator's path may be positional or the `path=` keyword, in any
    argument position, and may be the EMPTY string (a route mounted at the
    router prefix). Both live "" endpoints were missing from the denominator."""

    def _surface(self, monkeypatch, tmp_path, source):
        (tmp_path / "audit").mkdir()
        (tmp_path / "app").mkdir(parents=True)
        (tmp_path / "app" / "r.py").write_text(source)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")

        class R:
            returncode = 0
            stdout = "app/r.py\0"
            stderr = ""
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: R())
        mg.cmd_surface()
        return {(r["method"], r["path"]) for r in json.loads(
            (tmp_path / "audit" / "00-audit-surface.json").read_text())["routes"]}

    def test_every_path_form_reaches_the_denominator(self, monkeypatch, tmp_path):
        found = self._surface(monkeypatch, tmp_path, (
            '@router.get("/positional")\ndef a(): ...\n'
            '@router.get(path="/keyword")\ndef b(): ...\n'
            '@router.post(response_model=X, path="/late-keyword")\ndef c(): ...\n'
            '@router.patch("/with-kwargs", tags=["x"])\ndef d(): ...\n'
            '@api.router.put(path = "/spaced")\ndef e(): ...\n'))
        assert ("GET", "/positional") in found
        assert ("GET", "/keyword") in found
        assert ("POST", "/late-keyword") in found
        assert ("PATCH", "/with-kwargs") in found
        assert ("PUT", "/spaced") in found

    def test_an_empty_path_is_a_real_route_not_a_parse_failure(
            self, monkeypatch, tmp_path):
        found = self._surface(monkeypatch, tmp_path, (
            '@router.get(\n    "",\n    summary="Headline",\n)\ndef a(): ...\n'))
        assert ("GET", "") in found
        assert not any("summary" in p for _, p in found)   # no garbage capture

    def test_the_live_surface_carries_both_empty_path_routes(self):
        surface = json.loads(
            (Path(mg.ROOT) / "audit" / "00-audit-surface.json").read_text())
        empties = [r for r in surface["routes"] if r["path"] == ""]
        assert len(empties) == 2, empties
        assert len(surface["routes"]) >= 21


class TestPanelFindingsRound33:
    """Three fidelity/scope gaps the panel found in the round-32 close-out.
    Each is verified against the pre-fix gate (see the commit) as either a
    false block or a silently-omitted endpoint."""

    def _calibrate_app(self, monkeypatch, tmp_path, files):
        return TestPanelFindingsRound25._calibrate_app(
            TestPanelFindingsRound25(), monkeypatch, tmp_path, files)

    def _calib_error(self):
        return TestPanelFindingsRound25._calib_error(TestPanelFindingsRound25())

    def _surface(self, monkeypatch, tmp_path, source):
        return TestRoutePathExtraction._surface(
            TestRoutePathExtraction(), monkeypatch, tmp_path, source)

    # --- finding 1: the **kwargs ban was not scoped to model-reachers -------

    def test_ordinary_http_client_kwargs_do_not_freeze_releases(
            self, monkeypatch, tmp_path):
        # THE BUG: the **kwargs ban keyed on method NAMES (post/put/...), so an
        # ordinary price-feed `httpx.post(url, **opts)` in a non-model module
        # falsely blocked. It reaches no model and must pass.
        self._calibrate_app(monkeypatch, tmp_path, {"prices.py": (
            'import httpx\n'
            'def fetch(url, opts):\n'
            '    return httpx.post(url, **opts)\n')})
        mg.cmd_calibrate()           # does not raise

    def test_kwargs_into_a_model_reaching_call_still_blocks(
            self, monkeypatch, tmp_path):
        # BOTH SIDES: the same expansion in a module that DOES reach a model is
        # still forbidden (the endpoint literal makes it model-reaching).
        self._calibrate_app(monkeypatch, tmp_path, {"llm.py": (
            'import httpx\n'
            'def call(opts):\n'
            '    return httpx.post("https://api.anthropic.com/v1/messages",\n'
            '                      **opts)\n')})
        assert "expands **kwargs into a model call" in self._calib_error()

    def test_kwargs_into_an_sdk_call_still_blocks(self, monkeypatch, tmp_path):
        self._calibrate_app(monkeypatch, tmp_path, {"j.py": (
            'import anthropic\n'
            'def call(opts):\n'
            '    return client.messages.create(**opts)\n')})
        assert "expands **kwargs into a model call" in self._calib_error()

    # --- finding 2: a non-literal route path was silently dropped -----------

    def test_composed_literal_path_is_folded_into_the_surface(
            self, monkeypatch, tmp_path):
        found = self._surface(monkeypatch, tmp_path,
                              '@router.get("/api" + "/x")\ndef a(): ...\n')
        assert ("GET", "/api/x") in found

    def test_runtime_computed_path_fails_closed_not_silently_dropped(
            self, monkeypatch, tmp_path):
        # THE BUG: `@router.get(PREFIX + "/x")` with PREFIX a Name is a live
        # endpoint the scan cannot read. The pre-fix code set route=None and
        # `continue`d, so it vanished from the denominator while surface --check
        # greened. It must fail instead.
        (tmp_path / "audit").mkdir()
        (tmp_path / "app").mkdir(parents=True)
        (tmp_path / "app" / "r.py").write_text(
            'PREFIX = "/api"\n@router.get(PREFIX + "/x")\ndef a(): ...\n')
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")

        class R:
            returncode = 0
            stdout = "app/r.py\0"
            stderr = ""
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: R())
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), pytest.raises(SystemExit):
            mg.cmd_surface()
        assert "not a readable literal" in err.getvalue()

    # --- finding 3: api_route collapsed methods to a single ANY entry -------

    def test_api_route_emits_one_entry_per_declared_method(
            self, monkeypatch, tmp_path):
        found = self._surface(monkeypatch, tmp_path,
                              '@router.api_route("/x", methods=["GET", "POST"])\n'
                              'def a(): ...\n')
        assert ("GET", "/x") in found
        assert ("POST", "/x") in found
        assert ("ANY", "/x") not in found

    def test_api_route_without_methods_defaults_to_get(
            self, monkeypatch, tmp_path):
        found = self._surface(monkeypatch, tmp_path,
                              '@router.api_route("/y")\ndef a(): ...\n')
        assert ("GET", "/y") in found

    def test_api_route_with_non_literal_methods_fails_closed(
            self, monkeypatch, tmp_path):
        (tmp_path / "audit").mkdir()
        (tmp_path / "app").mkdir(parents=True)
        (tmp_path / "app" / "r.py").write_text(
            'M = ["GET"]\n@router.api_route("/z", methods=M)\ndef a(): ...\n')
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")

        class R:
            returncode = 0
            stdout = "app/r.py\0"
            stderr = ""
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: R())
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), pytest.raises(SystemExit):
            mg.cmd_surface()
        assert "methods=" in err.getvalue()

    def test_const_str_is_the_one_shared_folder(self):
        # the drift that produced findings 27/32/33 was two folding copies;
        # pin that both call sites use the module-level const_str
        assert mg.const_str(mg.ast.parse('"a" + "b"', mode="eval").body) == "ab"
        assert mg.const_str(mg.ast.parse('x + "b"', mode="eval").body) is None


class TestBranchReviewFixes:
    """Regression tests for the 2026-07-26 branch-wide review (audit/11).
    Each pins the exact pre-fix failure and exercises both sides."""

    # --- P1.1: calibrate must check the REPO's ruff config, not --isolated ---

    def test_p1_1_ruff_config_check_both_ways(self):
        assert mg._ruff_selects_s110()               # the live repo selects S

    def test_p1_1_deselecting_S_is_caught(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.ruff.lint]\nselect = ["E", "F"]\nignore = []\n')
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        assert not mg._ruff_selects_s110()

    def test_p1_1_ignoring_S110_is_caught(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.ruff.lint]\nselect = ["S"]\nignore = ["S110"]\n')
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        assert not mg._ruff_selects_s110()

    def test_p1_1_absent_config_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mg, "ROOT", tmp_path)     # no pyproject.toml
        assert not mg._ruff_selects_s110()

    # --- P1.2: AST credential scan catches annotated assignments -------------

    def test_p1_2_annotated_assignment_is_caught(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text('api_key: str = "sk-verylongsecretvalue1234567890"\n')
        assert mg.scan_credential_shapes([f])         # the exact review fixture

    def test_p1_2_empty_and_placeholder_defaults_stay_clean(self, tmp_path):
        f = tmp_path / "c.py"
        f.write_text('anthropic_api_key: str = ""\n'
                     'x: str = ""\n')
        assert mg.scan_credential_shapes([f]) == []

    def test_p1_2_unparseable_fails_closed(self, tmp_path):
        f = tmp_path / "broken.py"
        f.write_text("def f(:\n")
        hits = mg.scan_credential_shapes([f])
        assert hits and "cannot parse" in hits[0]

    def test_p1_2_annotated_uuid_still_caught(self, tmp_path):
        f = tmp_path / "u.py"
        f.write_text('sipgate_token: str = "4ed251b5-8a9c-4b3e-9f21-7c6d5e4a3b21"\n')  # pragma: allowlist secret
        assert mg.scan_credential_shapes([f])

    # --- P2.7: surface fails on a path that does not exist -------------------

    def test_p2_7_nonexistent_prompt_path_fails_closed(self, monkeypatch, tmp_path):
        # THE BUG: the surface named app/llm.py, a file that does not exist,
        # and drift-check (JSON==generator) never noticed. Build a real-enough
        # ROOT (contains the gate script so validation runs) but WITHOUT the
        # claimed prompt file, and prove it fails.
        (tmp_path / "audit").mkdir()
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "mandate_gate.py").write_text("# gate\n")
        (tmp_path / "app" / "routers").mkdir(parents=True)
        monkeypatch.setattr(mg, "ROOT", tmp_path)
        monkeypatch.setattr(mg, "AUDIT", tmp_path / "audit")

        class R:
            returncode = 0
            stdout = "scripts/mandate_gate.py\0"
            stderr = ""
        monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: R())
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), pytest.raises(SystemExit):
            mg.cmd_surface()
        assert "do not exist" in err.getvalue()

    def test_p2_7_live_prompt_path_exists(self):
        assert (Path(mg.ROOT) / "app" / "engine" / "judgment.py").exists()
        surface = json.loads(
            (Path(mg.ROOT) / "audit" / "00-audit-surface.json").read_text())
        assert any("judgment.py" in p for p in surface["prompts"])
        assert not any("app/llm.py" in p for p in surface["prompts"])
