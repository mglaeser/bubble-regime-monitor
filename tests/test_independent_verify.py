"""The independent cross-vendor review panel's decision logic, under pytest.

The pure gate functions (decide / model_matches / require_approvals /
attest_reasons / attest_proof) are the panel's entire merge-blocking logic;
this suite pins their fail-closed semantics so a future edit cannot silently
soften them. The script's own --selftest covers the identical cases at CI
runtime; here the same guarantees ride the normal pytest suite.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest
import yaml

_SPEC = importlib.util.spec_from_file_location(
    "independent_verify", Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
iv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(iv)
TEST_BASE_URL = "https://verifier.example.test/v1"
# Network-shape tests use a visibly inert, explicit endpoint.  Missing-config
# tests below import a fresh module or override these values to exercise refusal.
iv.BASE = TEST_BASE_URL
iv.KEY = "test-verifier-key"  # pragma: allowlist secret

MDL = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
A = {"ok": True, "v": {"refuted": False, "reason": "reason long enough a"}}
A2 = {"ok": True, "v": {"refuted": False, "reason": "reason long enough b"}}
RF = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "real bug"}}
ERR = {"ok": False, "reason": "API 500"}

_VERIFIER_ENV = (
    "GITHUB_BASE_REF",
    "GITHUB_STEP_SUMMARY",
    "OPENAI_API_KEY",
    "SECOND_VENDOR_API_KEY",
    "VERIFIER_AUTH_HEADER",
    "VERIFIER_BASE_BRANCH",
    "VERIFIER_BASE_URL",
    "VERIFIER_HEAD_SHA",
    "VERIFIER_MIN_OTHER_APPROVERS",
    "VERIFIER_MODEL",
    "VERIFIER_PANEL",
    "VERIFIER_PANEL_MODELS",
    "VERIFIER_REQUIRED_APPROVER",
    "VERIFIER_REQUIRE_DEFECT_LIST",
    "VERIFIER_REQUIRE_KEY",
    "VERIFIER_STRICT_ANY_REFUTATION",
)


@pytest.fixture(autouse=True)
def _isolate_verifier_environment(monkeypatch):
    """Unit verdicts must not inherit an operator's live panel settings."""
    for name in _VERIFIER_ENV:
        monkeypatch.delenv(name, raising=False)


def _sse_events(events: list[dict]) -> list[bytes]:
    """Encode payloads in the gateway's observed SSE framing: an ``event:``
    line, a ``data:`` line and a blank delimiter per event block."""
    lines: list[bytes] = []
    for e in events:
        lines.append(f"event: {e.get('type', 'message')}\n".encode())
        lines.append(f"data: {json.dumps(e)}\n".encode())
        lines.append(b"\n")
    return lines


class _FakeWire:
    """A urlopen return value: iterable line by line like the real socket-backed
    response (the SSE path reads it progressively) and ``.read()``-able whole
    (the chat wire's JSON body)."""

    def __init__(self, lines: list[bytes]):
        self.status = 200
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return b"".join(self._lines)


class TestDecide:
    def test_fail_closed_on_unparsable(self):
        assert iv.decide(None)["block"] is True
        assert iv.decide({})["block"] is True
        assert iv.decide({"refuted": "yes"})["block"] is True   # non-bool

    def test_confidence_thresholds(self):
        assert iv.decide({"refuted": True, "confidence": "high"})["block"] is True
        assert iv.decide({"refuted": True, "confidence": "medium"})["block"] is True
        assert iv.decide({"refuted": True, "confidence": "low"})["block"] is False
        assert iv.decide({"refuted": False})["block"] is False


class TestRequiredApproverRole:
    def test_sol_veto_any_confidence(self):
        low = {"ok": True, "v": {"refuted": True, "confidence": "low", "reason": "small doubt"}}
        assert iv.require_approvals([A, low, A], MDL, "gpt-5.6-sol", 1)["block"] is True
        assert iv.require_approvals([A, RF, A], MDL, "gpt-5.6-sol", 1)["block"] is True

    def test_sol_missing_or_fallback_blocks(self):
        no_sol = ["gpt-5.3-codex", "gpt-5.6", "gpt-4.1-mini"]
        assert iv.require_approvals([A, A2, A], no_sol, "gpt-5.6-sol", 1)["block"] is True

    def test_dated_snapshot_counts_variant_does_not(self):
        assert iv.model_matches("gpt-5.6-sol-2026-07-01", "gpt-5.6-sol") is True
        for variant in ("gpt-5.6-sol-mini", "gpt-5.6-sol-codex", "gpt-5.6-sol-preview",
                        "gpt-5.6-solaris", "gpt-5.6"):
            assert iv.model_matches(variant, "gpt-5.6-sol") is False

    def test_independent_corroboration_distinct_models(self):
        assert iv.require_approvals([A, A2, A], MDL, "gpt-5.6-sol", 1)["block"] is False
        assert iv.require_approvals([RF, A, RF], MDL, "gpt-5.6-sol", 1)["block"] is True
        dup = ["gpt-4.1-mini", "gpt-5.6-sol", "gpt-4.1-mini"]
        assert iv.require_approvals([A, A2, A], dup, "gpt-5.6-sol", 2)["block"] is True
        solo = ["gpt-5.6-sol"] * 3
        assert iv.require_approvals([A, A2, A], solo, "gpt-5.6-sol", 1)["block"] is True

    def test_nan_min_others_never_fail_open(self):
        assert iv.require_approvals([RF, A2, RF], MDL, "gpt-5.6-sol", float("nan"))["block"] is True
        assert iv.require_approvals([A, A2, RF], MDL, "gpt-5.6-sol", float("nan"))["block"] is False

    def test_sol_approval_must_be_proven(self):
        ch = "selftest-challenge"
        empty = {"ok": True, "v": {"refuted": False, "reason": "", "proof": f"{ch}-7"}}
        noproof = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol"}}
        good = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol",
                                  "proof": f"{ch}-7"}}
        assert iv.require_approvals([A, empty, A], MDL, "gpt-5.6-sol", 1, ch)["block"] is True
        assert iv.require_approvals([A, noproof, A], MDL, "gpt-5.6-sol", 1, ch)["block"] is True
        assert iv.require_approvals([A, good, A], MDL, "gpt-5.6-sol", 1, ch)["block"] is False


class TestIntegrityGates:
    def test_canned_green_blocks(self):
        r = lambda s, refuted=False: {"ok": True, "v": {"refuted": refuted, "reason": s}}  # noqa: E731
        same = "reason one aaaa"
        assert iv.attest_reasons([r(same), r(same), r(same)], 3)["block"] is True
        assert iv.attest_reasons([r(same), r(same), r("real bug here", True)], 3)["block"] is True
        assert iv.attest_reasons([r("reason a x1"), r("reason b x2"), r("reason c x3")], 3)["block"] is False

    def test_proof_of_check_bounds(self):
        ch = "selftest-challenge"
        pr = lambda p: {"ok": True, "v": {"refuted": False, "reason": "reason long enough", "proof": p}}  # noqa: E731
        assert iv.attest_proof([pr(f"{ch}-1"), pr(f"{ch}-9999"), pr(f"{ch}-500")], ch, 3)["block"] is False
        assert iv.attest_proof([pr(f"{ch}-0"), pr(f"{ch}-0"), pr(f"{ch}-0")], ch, 3)["block"] is True
        assert iv.attest_proof([pr(f"{ch}-10000")] * 3, ch, 3)["block"] is True
        assert iv.attest_proof([pr("wrong-1")] * 3, ch, 3)["block"] is True
        assert iv.attest_proof([pr(f"{ch}-7")] * 3, "", 3)["block"] is True


class TestNoKeyResidualMode:
    def test_selftest_passes_and_no_key_is_green_and_visible(self, monkeypatch):
        script = str(Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
        out = subprocess.run([sys.executable, script, "--selftest"],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and "selftest" in out.stdout
        env = {"PATH": "/usr/bin:/bin"}   # no keys at all
        out2 = subprocess.run([sys.executable, script], capture_output=True, text=True,
                              timeout=60, env=env)
        assert out2.returncode == 0                 # never fake-blocks
        assert "RESIDUAL" in out2.stdout            # never fake-green either: loudly inactive


class TestEmptyEnvVarsAreAbsent:
    def test_suite_ignores_a_hostile_ambient_auth_header(self):
        target = (
            f"{Path(__file__).resolve()}::TestVerifierDiagnosticsAreSecretSafe"
            "::test_peer_error_bodies_cannot_echo_key_or_endpoint"
        )
        env = os.environ.copy()
        env["VERIFIER_AUTH_HEADER"] = "Bad Header"

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", target],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )

        assert result.returncode == 0, result.stdout + result.stderr

    def test_empty_base_url_has_no_implicit_host(self, monkeypatch):
        # This key belongs to one operator-pinned endpoint.  An empty Actions
        # secret must never select a different host on the key's behalf.
        monkeypatch.setenv("VERIFIER_BASE_URL", "")
        spec = importlib.util.spec_from_file_location(
            "independent_verify_emptyenv",
            Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.BASE == ""

    def test_credentialed_main_refuses_a_blank_endpoint_before_diff_io(
            self, monkeypatch, capsys):
        credential = "verifier-credential-must-not-leak"  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", "")
        monkeypatch.setattr(
            iv, "build_diff",
            lambda: pytest.fail("credentialed verifier read the diff without an endpoint"),
        )
        monkeypatch.setattr(iv.sys, "argv", ["independent_verify.py"])

        assert iv.main() == 1
        captured = capsys.readouterr()
        assert "VERIFIER_BASE_URL" in captured.err
        assert credential not in captured.out + captured.err

    @pytest.mark.parametrize("base", [
        "",
        "https://münchen.example/v1",
        "https://xn--mnchen-3ya.example/v1",
        "https://secret.example./v1",
        "https://[fe80::1%25eth0]/v1",
        "https://[0:0:0:0:0:0:0:1]/v1",
        "https://0177.0.0.1/v1",
        "https://127.1/v1",
        "https://0x7f000001/v1",
        "https://2130706433/v1",
        "https://127.0.0.0x1/v1",
        "https://0x7f.0.0.0x1/v1",
        "https://gateway/v1",
    ])
    def test_unsafe_endpoint_is_guarded_at_both_network_sinks(self, monkeypatch, base):
        monkeypatch.setattr(iv, "BASE", base)
        opened = False

        def forbidden_urlopen(*args, **kwargs):
            nonlocal opened
            opened = True
            raise AssertionError("verifier opened a network sink without its endpoint")

        monkeypatch.setattr(iv, "_urlopen", forbidden_urlopen)
        with pytest.raises(iv.ProviderConfigError, match="VERIFIER_BASE_URL"):
            iv._http_json("/models")
        with pytest.raises(iv.ProviderConfigError, match="VERIFIER_BASE_URL"):
            iv._responses_attempt("model", "system", "user")

        assert opened is False

    @pytest.mark.parametrize("base", [
        "http://verifier.example.test/v1",
        "https://user:password@verifier.example.test/v1",  # pragma: allowlist secret
        "https://verifier.example.test/v1?route=other",
        "https://verifier.example.test/v1?",
        "https://verifier.example.test/v1#fragment",
        "https://verifier.example.test/v1#",
        "https://münchen.example/v1",
        "https://xn--mnchen-3ya.example/v1",
        "https://secret.example./v1",
        "https://[fe80::1%25eth0]/v1",
        "https://[0:0:0:0:0:0:0:1]/v1",
        "https://0177.0.0.1/v1",
        "https://127.1/v1",
        "https://0x7f000001/v1",
        "https://2130706433/v1",
        "https://127.0.0.0x1/v1",
        "https://0x7f.0.0.0x1/v1",
        "https://gateway/v1",
        "https://verifier.example.test/not-v1",
        "https://verifier.example.test:not-a-port/v1",
    ])
    def test_unsafe_endpoint_shapes_are_rejected(self, monkeypatch, base):
        monkeypatch.setattr(iv, "BASE", base)
        with pytest.raises(iv.ProviderConfigError, match="VERIFIER_BASE_URL"):
            iv._api_url("/responses")

    def test_endpoint_builder_accepts_only_internal_relative_paths(self, monkeypatch):
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        assert iv._api_url("/responses") == TEST_BASE_URL + "/responses"
        with pytest.raises(ValueError):
            iv._api_url("https://wrong.example/v1/responses")

    def test_verifier_transport_disables_redirects_and_ambient_proxies(self):
        assert any(
            isinstance(handler, iv._NoRedirectHandler)
            for handler in iv._NO_REDIRECT_OPENER.handlers
        )
        # Supplying ProxyHandler({}) suppresses build_opener's ambient default;
        # the empty handler itself has no protocol methods and is not retained.
        assert not any(
            isinstance(handler, iv.urllib.request.ProxyHandler)
            for handler in iv._NO_REDIRECT_OPENER.handlers
        )
        redirect = iv._NoRedirectHandler()
        assert redirect.redirect_request(
            None, None, 302, "Found", {}, "https://wrong.example/v1"
        ) is None

    def test_production_opener_suppresses_ambient_https_proxy_at_import(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py"
        proxy_url = "http://proxy.invalid:3128"
        probe = (
            "import importlib.util\n"
            "import sys\n"
            "import urllib.request\n"
            "ambient = urllib.request.build_opener()\n"
            "ambient_https = [handler for handler in ambient.handlers "
            "if isinstance(handler, urllib.request.ProxyHandler) "
            "and handler.proxies.get('https') == sys.argv[2]]\n"
            "if not ambient_https:\n"
            "    raise SystemExit('ambient HTTPS proxy precondition was not established')\n"
            "spec = importlib.util.spec_from_file_location('verifier_proxy_probe', sys.argv[1])\n"
            "if spec is None or spec.loader is None:\n"
            "    raise SystemExit('could not load production verifier')\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "retained = [handler for handler in module._NO_REDIRECT_OPENER.handlers "
            "if isinstance(handler, urllib.request.ProxyHandler) and handler.proxies]\n"
            "if retained:\n"
            "    raise SystemExit('production verifier retained an ambient proxy handler')\n"
        )
        env = {
            "HTTPS_PROXY": proxy_url,
            "PATH": os.environ.get("PATH", ""),
        }

        result = subprocess.run(
            [sys.executable, "-c", probe, str(script), proxy_url],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

        assert result.returncode == 0, result.stdout + result.stderr


class TestTrustedWorkflowEndpointPreflight:
    @staticmethod
    def _mask_step() -> dict:
        workflow = yaml.safe_load(
            (Path(__file__).resolve().parents[1]
             / ".github/workflows/independent-verify.yml").read_text()
        )
        steps = workflow["jobs"]["panel"]["steps"]
        return next(
            step for step in steps
            if step.get("name") == "Mask the verifier endpoint in the log"
        )

    @staticmethod
    def _publisher_step() -> dict:
        workflow = yaml.safe_load(
            (Path(__file__).resolve().parents[1]
             / ".github/workflows/independent-verify.yml").read_text()
        )
        return next(
            step for step in workflow["jobs"]["panel"]["steps"]
            if step.get("name") == "Publish the verdict onto the candidate head"
        )

    def _run_mask_step(
            self, tmp_path, *, base_url: str, key_configured: str,
            require_key_setting: str = "", is_fork: str = "false",
            is_dependabot_pr: str = "false") -> tuple[subprocess.CompletedProcess, dict[str, str]]:
        github_output = tmp_path / "verifier-config-output"
        env = os.environ.copy()
        env["VERIFIER_BASE_URL"] = base_url
        env["SECOND_VENDOR_API_KEY_CONFIGURED"] = key_configured
        env["VERIFIER_REQUIRE_KEY_SETTING"] = require_key_setting
        env["IS_FORK"] = is_fork
        env["IS_DEPENDABOT_PR"] = is_dependabot_pr
        env["GITHUB_OUTPUT"] = str(github_output)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(
            ["bash", "-c", self._mask_step()["run"]],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        outputs = {}
        if github_output.exists():
            outputs = dict(
                line.split("=", 1)
                for line in github_output.read_text().splitlines()
            )
        return result, outputs

    def test_workflow_binds_presence_and_runtime_key_to_the_same_secret(self):
        mask_step = self._mask_step()
        workflow = yaml.safe_load(
            (Path(__file__).resolve().parents[1]
             / ".github/workflows/independent-verify.yml").read_text()
        )
        panel_step = next(
            step for step in workflow["jobs"]["panel"]["steps"]
            if step.get("name") == "Cross-vendor review panel"
        )
        assert mask_step["env"]["SECOND_VENDOR_API_KEY_CONFIGURED"] == (
            "${{ secrets.TRUSTED_VERIFIER_API_KEY != '' }}"
        )
        assert panel_step["env"]["SECOND_VENDOR_API_KEY"] == (
            "${{ secrets.TRUSTED_VERIFIER_API_KEY }}"
        )
        assert panel_step["env"]["OPENAI_API_KEY"] == ""
        assert "SECOND_VENDOR_API_KEY" not in mask_step["env"]

    def test_workflow_derives_one_config_from_raw_setting_and_pr_identity(self):
        workflow = yaml.safe_load(
            (Path(__file__).resolve().parents[1]
             / ".github/workflows/independent-verify.yml").read_text()
        )
        panel_step = next(
            step for step in workflow["jobs"]["panel"]["steps"]
            if step.get("name") == "Cross-vendor review panel"
        )
        mask_step = self._mask_step()
        assert mask_step["id"] == "verifier_config"
        assert mask_step["env"]["VERIFIER_REQUIRE_KEY_SETTING"] == (
            "${{ vars.VERIFIER_REQUIRE_KEY }}"
        )
        assert mask_step["env"]["IS_FORK"] == (
            "${{ github.event.pull_request.head.repo.full_name != github.repository }}"
        )
        assert mask_step["env"]["IS_DEPENDABOT_PR"] == (
            "${{ startsWith(github.event.pull_request.user.login, 'dependabot') }}"
        )
        assert panel_step["env"]["VERIFIER_REQUIRE_KEY"] == (
            "${{ steps.verifier_config.outputs.require_key }}"
        )

    def test_publisher_derives_dormancy_from_the_same_explicit_opt_out(self):
        assert self._publisher_step()["env"]["PANEL_DORMANT"] == (
            "${{ steps.verifier_config.outputs.panel_dormant }}"
        )

    @pytest.mark.parametrize(
        ("setting", "expected_require_key", "expected_dormant", "expected_returncode"),
        [
            ("", "true", "false", 1),
            ("true", "true", "false", 1),
            ("FALSE", "true", "false", 1),
            ("False", "true", "false", 1),
            ("garbage", "true", "false", 1),
            ("false", "false", "true", 0),
        ],
    )
    def test_only_exact_lowercase_false_selects_dormant_mode(
            self, tmp_path, setting, expected_require_key,
            expected_dormant, expected_returncode):
        result, outputs = self._run_mask_step(
            tmp_path,
            base_url="",
            key_configured="false",
            require_key_setting=setting,
        )
        assert result.returncode == expected_returncode
        assert outputs == {
            "require_key": expected_require_key,
            "panel_dormant": expected_dormant,
        }

    @pytest.mark.parametrize("origin", ["fork", "dependabot"])
    def test_untrusted_origin_cannot_select_dormant_mode(self, tmp_path, origin):
        result, outputs = self._run_mask_step(
            tmp_path,
            base_url="",
            key_configured="false",
            require_key_setting="false",
            is_fork="true" if origin == "fork" else "false",
            is_dependabot_pr="true" if origin == "dependabot" else "false",
        )
        assert result.returncode != 0
        assert outputs == {"require_key": "true", "panel_dormant": "false"}

    @pytest.mark.parametrize(
        ("is_fork", "panel_dormant", "outcome", "expected_returncode",
         "expected_state", "expected_description"),
        [
            ("false", "true", "success", 0, "success",
             "panel dormant — deterministic CI only"),
            ("false", "true", "failure", 1, "failure",
             "cross-vendor panel refused — see job log"),
            ("false", "true", "skipped", 1, "failure",
             "cross-vendor panel refused — see job log"),
            ("false", "false", "success", 0, "success",
             "cross-vendor panel approved"),
            ("false", "false", "failure", 1, "failure",
             "cross-vendor panel refused — see job log"),
            ("false", "false", "skipped", 1, "failure",
             "cross-vendor panel refused — see job log"),
            ("false", "", "skipped", 1, "failure",
             "cross-vendor panel refused — see job log"),
            ("true", "true", "success", 1, "failure",
             "fork PR: panel not run — maintainer review required"),
        ],
    )
    def test_publisher_state_table_never_hides_a_skipped_or_failed_panel(
            self, tmp_path, is_fork, panel_dormant, outcome,
            expected_returncode, expected_state, expected_description):
        publisher = self._publisher_step()
        assert publisher["if"] == "always()"
        capture = tmp_path / "gh-args"
        fake_gh = tmp_path / "gh"
        fake_gh.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CAPTURE\"\n")
        fake_gh.chmod(0o700)
        env = os.environ.copy()
        env.update({
            "CAPTURE": str(capture),
            "PATH": str(tmp_path) + os.pathsep + env.get("PATH", ""),
            "HEAD_SHA": "a" * 40,
            "IS_FORK": is_fork,
            "OUTCOME": outcome,
            "PANEL_DORMANT": panel_dormant,
            "GITHUB_REPOSITORY": "owner/repository",
            "GITHUB_RUN_ID": "123",
            "GITHUB_SERVER_URL": "https://example.test",
        })
        result = subprocess.run(
            ["bash", "-c", publisher["run"]],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == expected_returncode
        published = capture.read_text()
        assert f"state={expected_state}" in published
        assert f"description={expected_description}" in published

    @pytest.mark.parametrize("setting", ["", "false"])
    def test_blank_endpoint_refuses_before_the_credentialed_panel(
            self, tmp_path, setting):
        result, _ = self._run_mask_step(
            tmp_path,
            base_url="",
            key_configured="true",
            require_key_setting=setting,
        )
        assert result.returncode != 0
        assert "refus" in (result.stdout + result.stderr).casefold()

    @pytest.mark.parametrize("base_url", ["", TEST_BASE_URL, "not-an-endpoint"])
    def test_required_panel_refuses_a_missing_key(self, tmp_path, base_url):
        result, _ = self._run_mask_step(
            tmp_path,
            base_url=base_url,
            key_configured="false",
        )
        assert result.returncode != 0
        assert "refus" in (result.stdout + result.stderr).casefold()

    @pytest.mark.parametrize("base_url", ["", "not-an-endpoint"])
    def test_explicit_dormant_mode_skips_endpoint_preflight(self, tmp_path, base_url):
        result, outputs = self._run_mask_step(
            tmp_path,
            base_url=base_url,
            key_configured="false",
            require_key_setting="false",
        )
        assert result.returncode == 0
        assert outputs == {"require_key": "false", "panel_dormant": "true"}
        assert "error" not in (result.stdout + result.stderr).casefold()


class TestVerifierDiagnosticsAreSecretSafe:
    ESCAPED_KEY = "secret-key-123"  # pragma: allowlist secret
    ESCAPED_VERDICT = (
        r'{"refuted":false,"confidence":"high","reason":"approved secret-key-\u0031\u0032\u0033",'
        r'"defects":[],"proof":"challenge-1"}'
    )

    def test_redaction_marker_shaped_key_is_still_detected_and_removed(self, monkeypatch):
        credential = "<redacted>"  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        rendered = f"transport exposed {credential}"

        assert iv._contains_protected_text(rendered) is True
        assert credential not in iv._safe_diag(rendered, limit=None)

    def test_peer_error_bodies_cannot_echo_key_or_endpoint(self, monkeypatch):
        credential = "peer-echo-verifier-credential"  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)

        def rejected(req, timeout=None):
            body = f"bad credential {credential} at {TEST_BASE_URL}".encode()
            raise urllib.error.HTTPError(
                req.full_url, 401, "unauthorized", None, io.BytesIO(body)
            )

        monkeypatch.setattr(iv, "_urlopen", rejected)
        json_failure = iv._http_json("/models")
        stream_failure = iv._responses_attempt("model", "system", "user")
        rendered = repr((json_failure, stream_failure))

        assert json_failure[0] == stream_failure[0] == 401
        assert credential not in rendered
        assert TEST_BASE_URL not in rendered
        assert "withheld" in rendered

    def test_transport_exceptions_cannot_publish_the_private_endpoint(self, monkeypatch):
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)

        def unreachable(req, timeout=None):
            raise OSError(f"could not resolve {TEST_BASE_URL}")

        monkeypatch.setattr(iv, "_urlopen", unreachable)
        failures = (
            iv._http_json("/models"),
            iv._responses_attempt("model", "system", "user"),
        )
        rendered = repr(failures)
        assert all(status == 0 for status, _detail in failures)
        assert TEST_BASE_URL not in rendered
        assert "verifier.example.test" not in rendered

    def test_success_body_that_echoes_the_key_is_rejected_not_rendered(
            self, monkeypatch):
        credential = "model-echo-verifier-credential"  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        verdict = json.dumps({
            "refuted": False,
            "confidence": "high",
            "reason": f"approved with {credential}",
            "defects": [],
            "proof": "challenge-1",
        })
        wire = _FakeWire(_sse_events([
            {"type": "response.output_text.delta", "delta": verdict},
            {"type": "response.completed", "response": {"id": "resp-echo"}},
        ]))
        monkeypatch.setattr(iv, "_urlopen", lambda req, timeout=None: wire)

        out = iv.attempt_once("model", "system", "user")
        assert out["ok"] is False
        assert out["status"] == 400
        assert credential not in repr(out)

    def test_required_verdict_key_may_equal_credential_without_making_json_impossible(
            self, monkeypatch):
        """Public JSON structure must not be mistaken for a peer secret echo."""
        credential = "confidence"  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        verdict = json.dumps({
            "refuted": False,
            "confidence": "high",
            "reason": "reviewed the changed gateway boundaries",
            "defects": [],
            "proof": "challenge-1",
        })
        wire = _FakeWire(_sse_events([
            {"type": "response.output_text.delta", "delta": verdict},
            {"type": "response.completed", "response": {"id": "resp-schema-key"}},
        ]))
        monkeypatch.setattr(iv, "_urlopen", lambda req, timeout=None: wire)

        out = iv.attempt_once("model", "system", "user")

        assert out["ok"] is True
        assert out["v"]["confidence"] == "high"

    def test_verdict_retains_raw_cross_token_credential_scan(self, monkeypatch):
        credential = 'ence":"high'  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        verdict = (
            '{"refuted":false,"confidence":"high",'
            '"reason":"reviewed gateway boundaries","defects":[],'
            '"proof":"challenge-1"}'
        )
        wire = _FakeWire(_sse_events([
            {"type": "response.output_text.delta", "delta": verdict},
            {"type": "response.completed", "response": {"id": "resp-boundary"}},
        ]))
        monkeypatch.setattr(iv, "_urlopen", lambda req, timeout=None: wire)

        out = iv.attempt_once("model", "system", "user")

        assert out["ok"] is False and out["status"] == 400
        assert credential not in repr(out)

    def test_wrapped_verdict_scans_a_credential_across_the_json_boundary(
            self, monkeypatch):
        credential = 'wrap{"ref'  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        verdict = (
            '{"refuted":false,"confidence":"high",'
            '"reason":"reviewed gateway boundaries","defects":[],'
            '"proof":"challenge-1"}'
        )
        wire = _FakeWire(_sse_events([
            {"type": "response.output_text.delta", "delta": "wrap" + verdict},
            {"type": "response.completed", "response": {"id": "resp-boundary"}},
        ]))
        monkeypatch.setattr(iv, "_urlopen", lambda req, timeout=None: wire)

        out = iv.attempt_once("model", "system", "user")

        assert out["ok"] is False and out["status"] == 400
        assert credential not in repr(out)

    def test_verdict_scan_fails_closed_when_json_exceeds_decoder_depth(self, monkeypatch):
        monkeypatch.setattr(iv, "KEY", "depth-test-credential")
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        deeply_nested = "[" * 1100 + '"SAFE"' + "]" * 1100

        assert iv._verdict_echoes_protected_text(deeply_nested) is True

    def test_schema_key_collision_does_not_exempt_the_credential_in_a_value(
            self, monkeypatch):
        credential = "confidence"  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        verdict = json.dumps({
            "refuted": False,
            "confidence": "high",
            "reason": f"reviewed with {credential}",
            "defects": [],
            "proof": "challenge-1",
        })
        wire = _FakeWire(_sse_events([
            {"type": "response.output_text.delta", "delta": verdict},
            {"type": "response.completed", "response": {"id": "resp-value-echo"}},
        ]))
        monkeypatch.setattr(iv, "_urlopen", lambda req, timeout=None: wire)

        out = iv.attempt_once("model", "system", "user")

        assert out["ok"] is False and out["status"] == 400
        assert credential not in repr(out)

    @pytest.mark.parametrize("fragment", [
        '"metadata":{"confidence":"safe"}',
        r'"\u0063onfidence_extra":"safe"',
    ])
    def test_root_key_exemption_does_not_cover_nested_or_unknown_keys(
            self, monkeypatch, fragment):
        credential = "confidence"  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        verdict = (
            '{"refuted":false,"confidence":"high",'
            '"reason":"reviewed gateway boundaries","defects":[],'
            '"proof":"challenge-1",' + fragment + '}'
        )
        wire = _FakeWire(_sse_events([
            {"type": "response.output_text.delta", "delta": verdict},
            {"type": "response.completed", "response": {"id": "resp-nested-key"}},
        ]))
        monkeypatch.setattr(iv, "_urlopen", lambda req, timeout=None: wire)

        out = iv.attempt_once("model", "system", "user")

        assert out["ok"] is False and out["status"] == 400
        assert credential not in repr(out)

    def test_duplicate_member_cannot_hide_an_escaped_credential_value(
            self, monkeypatch):
        credential = "secret-key-123"  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        verdict = (
            r'{"refuted":false,"confidence":"high",'
            r'"reason":"secret-key-\u0031\u0032\u0033",'
            r'"reason":"reviewed gateway boundaries","defects":[],'
            r'"proof":"challenge-1"}'
        )
        wire = _FakeWire(_sse_events([
            {"type": "response.output_text.delta", "delta": verdict},
            {"type": "response.completed", "response": {"id": "resp-duplicate"}},
        ]))
        monkeypatch.setattr(iv, "_urlopen", lambda req, timeout=None: wire)

        out = iv.attempt_once("model", "system", "user")

        assert out["ok"] is False and out["status"] == 400
        assert credential not in repr(out)

    def test_endpoint_literal_set_distinguishes_url_netloc_and_hostname(
            self, monkeypatch):
        base_url = "https://verifier.example.test:8443/v1"
        monkeypatch.setattr(iv, "BASE", base_url)

        assert set(iv._endpoint_literals()) == {
            base_url,
            "verifier.example.test:8443",
            "verifier.example.test",
        }

    @pytest.mark.parametrize("placement", ["prefix", "suffix"])
    @pytest.mark.parametrize("protected", [
        "wrapped-verifier-credential",
        TEST_BASE_URL,
        "verifier.example.test",
    ])
    def test_wrapped_verdict_scans_protected_text_outside_json(
            self, monkeypatch, placement, protected):
        credential = "wrapped-verifier-credential"  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        verdict = json.dumps({
            "refuted": False,
            "confidence": "high",
            "reason": "reviewed the changed gateway boundaries",
            "defects": [],
            "proof": "challenge-1",
        })
        wrapped = (f"{protected}\n{verdict}" if placement == "prefix"
                   else f"{verdict}\n{protected}")
        wire = _FakeWire(_sse_events([
            {"type": "response.output_text.delta", "delta": wrapped},
            {"type": "response.completed", "response": {"id": "resp-wrapped"}},
        ]))
        monkeypatch.setattr(iv, "_urlopen", lambda req, timeout=None: wire)

        out = iv.attempt_once("model", "system", "user")

        assert out["ok"] is False and out["status"] == 400
        assert protected not in repr(out)

    @pytest.mark.parametrize(("credential", "fragment"), [
        ("12345678", '"confidence":12345678'),
        ("12345678", '"usage":12345678'),
        ("12345678", '"usage":[12345678]'),
        ("12345678", '"usage":{"value":12345678}'),
        ("12345.678", '"usage":12345.678'),
        ("1.2345e67", '"usage":1.2345e67'),
        ("Infinity", '"usage":Infinity'),
    ])
    def test_numeric_credential_echo_in_json_value_fails_closed(
            self, monkeypatch, credential, fragment):
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        verdict = (
            '{"refuted":false,' + fragment + ','
            '"reason":"reviewed gateway boundaries","defects":[],'
            '"proof":"challenge-1"}'
        )
        wire = _FakeWire(_sse_events([
            {"type": "response.output_text.delta", "delta": verdict},
            {"type": "response.completed", "response": {"id": "resp-number"}},
        ]))
        monkeypatch.setattr(iv, "_urlopen", lambda req, timeout=None: wire)

        out = iv.attempt_once("model", "system", "user")

        assert out["ok"] is False and out["status"] == 400
        assert credential not in repr(out)

    def test_chat_fallback_that_echoes_the_key_is_rejected_not_rendered(
            self, monkeypatch):
        credential = "chat-echo-verifier-credential"  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        calls = 0

        def routed(req, timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 404, "no responses route", None, io.BytesIO(b"missing")
                )
            content = json.dumps({
                "refuted": False,
                "confidence": "high",
                "reason": f"approved with {credential}",
                "defects": [],
                "proof": "challenge-1",
            })
            return _FakeWire([json.dumps({
                "choices": [{"message": {"content": content}}],
            }).encode()])

        monkeypatch.setattr(iv, "_urlopen", routed)
        out = iv.attempt_once("model", "system", "user")
        assert calls == 2
        assert out["ok"] is False
        assert out["status"] == 400
        assert credential not in repr(out)

    def test_responses_json_escaped_key_echo_is_rejected(self, monkeypatch):
        monkeypatch.setattr(iv, "KEY", self.ESCAPED_KEY)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        wire = _FakeWire(_sse_events([
            {"type": "response.output_text.delta", "delta": self.ESCAPED_VERDICT},
            {"type": "response.completed", "response": {"id": "resp-escaped"}},
        ]))
        monkeypatch.setattr(iv, "_urlopen", lambda req, timeout=None: wire)

        out = iv.attempt_once("model", "system", "user")
        assert out["ok"] is False and out["status"] == 400
        assert self.ESCAPED_KEY not in repr(out)

    def test_chat_json_escaped_key_echo_is_rejected(self, monkeypatch):
        monkeypatch.setattr(iv, "KEY", self.ESCAPED_KEY)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        calls = 0

        def routed(req, timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 404, "no responses route", None, io.BytesIO(b"missing")
                )
            return _FakeWire([json.dumps({
                "choices": [{"message": {"content": self.ESCAPED_VERDICT}}],
            }).encode()])

        monkeypatch.setattr(iv, "_urlopen", routed)
        out = iv.attempt_once("model", "system", "user")
        assert calls == 2
        assert out["ok"] is False and out["status"] == 400
        assert self.ESCAPED_KEY not in repr(out)


class TestPanelFindingsOnItself:
    """Sol's veto on PR #21 raised two findings about the panel's own code;
    both responses are pinned here."""

    def test_privacy_excludes_are_case_insensitive(self):
        # uppercase .PNG/.SVG/.PDF must be excluded exactly like lowercase
        assert all(spec.startswith(":(exclude,icase,glob)") for spec in iv._EXCLUDES)

    @pytest.mark.parametrize("value,expected_on", [
        (None, True),          # variable deleted
        ("", True),            # variable present but empty
        ("   ", True),         # whitespace only
        ("flase", True),       # typo'd value
        ("maybe", True),       # anything unrecognised
        ("true", True),
        ("1", True),
        ("false", False),      # the only way off is to SAY so
        ("FALSE", False),
        ("off", False),
        ("0", False),
    ])
    def test_strict_mode_resolves_fail_closed(self, monkeypatch, value, expected_on):
        """Absence must not disable the gate.

        The flag was hardcoded because "a merge control that a variable can
        silently switch off is not a control". Making it settable is only safe
        while the ABSENT case means ON: otherwise deleting the variable,
        misspelling it in the workflow, or restoring a repo without its vars
        retires the gate with no signal. Only an explicit off value disables it."""
        monkeypatch.delenv("VERIFIER_STRICT_ANY_REFUTATION", raising=False)
        if value is not None:
            monkeypatch.setenv("VERIFIER_STRICT_ANY_REFUTATION", value)
        raw = (os.environ.get("VERIFIER_STRICT_ANY_REFUTATION") or "").strip()
        assert (raw.lower() not in ("0", "false", "no", "off")) is expected_on

    @pytest.mark.parametrize("min_others", [1, 0, -5, "", "abc", None])
    def test_the_two_voice_floor_is_code_not_configuration(self, min_others):
        """Strict mode off must not mean "anything passes".

        The floor is TWO distinct approving voices, one of them the required
        approver, and no repo variable can go below it: MIN_OTHER_APPROVERS
        degrades to 1 on zero, negative, blank and garbage. What the operator
        selects is "2 of 3 including the approver" vs "unanimous" — not whether
        review happens."""
        models = ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"]

        def vote(refuted, conf="high", reason="checked the gate assembly; it holds"):
            return {"ok": True, "v": {"refuted": refuted, "confidence": conf,
                                      "reason": reason}}

        ok, refute = vote(False), vote(True)
        approver_alone = iv.require_approvals([ok, refute, refute], models,
                                              "combo/SOTA-A", min_others)
        assert approver_alone["block"] is True, "the approver alone must never suffice"
        approver_plus_one = iv.require_approvals([ok, ok, refute], models,
                                                 "combo/SOTA-A", min_others)
        assert approver_plus_one["block"] is False, "two distinct approvals must suffice"

    def test_the_required_approver_veto_is_unconditional(self):
        """Off-switch or not, the required approver can always stop a merge."""
        models = ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"]

        def vote(refuted, conf="high", reason="checked the gate assembly; it holds"):
            return {"ok": True, "v": {"refuted": refuted, "confidence": conf,
                                      "reason": reason}}

        ok = vote(False)
        # a refutation at the LOWEST confidence still vetoes
        assert iv.require_approvals([vote(True, "low"), ok, ok], models,
                                    "combo/SOTA-A", 1)["block"] is True
        # so does a sham approval carrying no substantive reason
        assert iv.require_approvals([vote(False, "high", ""), ok, ok], models,
                                    "combo/SOTA-A", 1)["block"] is True
        # and so does the approver not being in the panel at all
        assert iv.require_approvals([ok, ok], ["combo/SOTA-B", "combo/SOTA-C"],
                                    "combo/SOTA-A", 1)["block"] is True

    def test_strict_mode_blocks_any_high_medium_refutation(self):
        models = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
        ok = {"ok": True, "v": {"refuted": False, "reason": "reason long enough"}}
        rf = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "bug"}}
        low = {"ok": True, "v": {"refuted": True, "confidence": "low", "reason": "doubt"}}
        assert iv.strict_any_refutation([ok, ok, rf], models)["block"] is True
        assert iv.strict_any_refutation([ok, ok, low], models)["block"] is False
        assert iv.strict_any_refutation([ok, ok, ok], models)["block"] is False

    def test_default_mode_stays_reference_identical(self):
        # WITHOUT strict mode the reference semantics hold: Sol + one distinct
        # corroborator green even when a third voice refutes high-confidence.
        models = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
        ok = {"ok": True, "v": {"refuted": False, "reason": "reason long enough x"}}
        ok2 = {"ok": True, "v": {"refuted": False, "reason": "reason long enough y"}}
        rf = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "bug"}}
        assert iv.require_approvals([rf, ok, ok2], models, "gpt-5.6-sol", 1)["block"] is False

    def test_fork_origin_without_key_fails_closed(self):
        # Sol round 2: fork PRs run with secrets withheld; no-key there must
        # BLOCK (exit 1), while same-repo no-key stays green-but-loud.
        script = str(Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
        env = {"PATH": "/usr/bin:/bin", "VERIFIER_REQUIRE_KEY": "true"}
        out = subprocess.run([sys.executable, script], capture_output=True, text=True,
                             timeout=60, env=env)
        assert out.returncode == 1
        assert "required verifier run" in out.stderr

    def test_round3_data_denylist_extended(self):
        for ext in ("csv", "tsv", "sql", "jsonl", "parquet", "sqlite", "dump", "bak"):
            assert ext in iv.EXCLUDE_EXTS
        # .json stays reviewable ON PURPOSE: frozen_methodology.json IS the
        # methodology and must be visible to the panel.
        assert "json" not in iv.EXCLUDE_EXTS

    def test_round3_truncation_is_explicitly_marked(self):
        long = "x" * 100
        marked = iv.truncate_marked(long, 40, "DIFF BODY")
        assert marked.startswith("x" * 40)
        assert "DIFF BODY TRUNCATED — 60 of 100 bytes omitted" in marked
        assert iv.truncate_marked("short", 40, "DIFF BODY") == "short"

    def test_round3_responses_only_models_are_served_by_the_primary_wire(self, monkeypatch):
        # Round-3 finding, resettled by the wire swap: a model served ONLY on
        # /responses used to need a body-sniffing 400/404 fallback off the chat
        # wire (should_fallback_responses). /responses is now the PRIMARY wire
        # -- streamed, because its providers reject the non-streaming form --
        # so that class is answered on the first attempt and the predicate is
        # gone. This pins the finding's actual property: such a model must
        # never block the panel.
        def fake_urlopen(req, timeout=None):
            assert req.full_url.endswith("/responses"), \
                "a responses-only model must never need the chat wire"
            return _FakeWire(_sse_events([
                {"type": "response.output_text.delta",
                 "delta": '{"refuted": false, "confidence": "high", '
                          '"reason": "docs only change", "defects": [], "proof": "x-1"}'},
                {"type": "response.completed", "response": {"id": "resp_r3", "output": []}},
            ]))
        monkeypatch.setattr(iv, "_urlopen", fake_urlopen)
        out = iv.attempt_once("responses-only-model", "sys", "usr")
        assert out["ok"] is True and out["v"]["refuted"] is False

    def test_round4_base_branch_from_github_base_ref(self, monkeypatch):
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        assert iv.base_branch() == "main"
        monkeypatch.setenv("GITHUB_BASE_REF", "")     # Actions empty-string trap
        assert iv.base_branch() == "main"
        monkeypatch.setenv("GITHUB_BASE_REF", "develop")
        assert iv.base_branch() == "develop"

    def test_round4_file_list_unfiltered_contents_filtered(self):
        cmds = iv.diff_commands("BASE")
        assert not any(a.startswith(":(exclude") for a in cmds["names"])
        assert any(a.startswith(":(exclude") for a in cmds["stat"])
        assert any(a.startswith(":(exclude") for a in cmds["body"])

    def test_round6_sh_failure_blocks_and_bad_utf8_stays_visible(self):
        import pytest as _pytest
        with _pytest.raises(iv.DiffError):
            iv._sh(["git", "rev-parse", "--verify", "no-such-ref-xyz123"], required=True)
        assert iv._sh(["false"]) == ""            # non-required keeps soft behavior
        out = iv._sh([sys.executable, "-c",
                      "import sys; sys.stdout.buffer.write(b'ok\\xff\\xfebad')"])
        assert "ok" in out and "bad" in out       # invalid UTF-8 visible, not vanished
        assert "�" in out

    def test_round6_diff_error_blocks_main(self, monkeypatch):
        monkeypatch.setattr(iv, "KEY", "fake-key-for-test")
        def boom():
            raise iv.DiffError("git exploded")
        monkeypatch.setattr(iv, "build_diff", boom)
        assert iv.main() == 1

    def test_round7_merge_base_failure_blocks_not_narrows(self, monkeypatch):
        import pytest as _pytest
        # a missing base ref must raise DiffError (block), never silently fall
        # back to HEAD~1 (which reviews only the tip commit of a multi-commit PR)
        monkeypatch.setenv("GITHUB_BASE_REF", "definitely-not-a-branch-xyz")
        with _pytest.raises(iv.DiffError):
            iv.build_diff()

    def test_round8_low_refutation_does_not_attest(self):
        ch = "selftest-challenge"
        sol_good = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol",
                                      "proof": f"{ch}-7"}}
        low_ref = {"ok": True, "v": {"refuted": True, "confidence": "low",
                                     "reason": "substantive doubt here", "proof": f"{ch}-9"}}
        canned = {"ok": True, "v": {"refuted": False, "reason": ""}}
        # a low refutation's reason/proof must NOT count toward the green majorities
        assert iv.attest_reasons([sol_good, low_ref, canned], 3)["block"] is True
        assert iv.attest_proof([sol_good, low_ref, canned], ch, 3)["block"] is True

    def test_round8_canned_corroborator_blocked_end_to_end(self):
        # require_approvals alone would green (Sol + codex approve), but the
        # canned codex approval fails attest_reasons now that the dissenting
        # low-refutation no longer attests on its behalf.
        ch = "selftest-challenge"
        models = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
        canned_approve = {"ok": True, "v": {"refuted": False, "reason": "", "proof": f"{ch}-3"}}
        sol_good = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol",
                                      "proof": f"{ch}-7"}}
        low_ref = {"ok": True, "v": {"refuted": True, "confidence": "low",
                                     "reason": "substantive doubt here", "proof": f"{ch}-9"}}
        votes = [canned_approve, sol_good, low_ref]
        assert iv.require_approvals(votes, models, "gpt-5.6-sol", 1, ch)["block"] is False
        assert iv.attest_reasons(votes, 3)["block"] is True   # the conjunctive gate catches it


class TestAttestConsistency:
    """A green vote whose OWN reason names a defect is an inconsistent vote, not
    an approval. decide() reads only the boolean, so without this gate such a
    vote both counts toward the quorum and supplies a substantive, distinct
    reason that helps attest_reasons pass. Found adversarially against a live
    panel; fail-closed."""

    M3 = ["m-a", "m-b", "m-c"]

    @staticmethod
    def _green(reason, refuted=False):
        return {"ok": True, "v": {"refuted": refuted, "reason": reason, "confidence": "high"}}

    def test_ordinary_approvals_pass(self):
        votes = [self._green("checked auth paths, no issue"),
                 self._green("docs only change"),
                 self._green("reviewed diff, behaviour unchanged")]
        assert iv.attest_consistency(votes, self.M3)["block"] is False

    def test_green_vote_naming_a_defect_blocks(self):
        votes = [self._green("checked auth paths"),
                 self._green("auth bypass when key is None"),
                 self._green("docs only")]
        out = iv.attest_consistency(votes, self.M3)
        assert out["block"] is True and "m-b" in out["reason"]

    def test_refuting_vote_may_name_a_defect(self):
        # Naming the defect is precisely a refutation's job -- only GREEN votes
        # are inspected, or every real finding would trip its own gate.
        votes = [self._green("fail-open on missing header", refuted=True),
                 self._green("docs only"), self._green("no issue found")]
        assert iv.attest_consistency(votes, self.M3)["block"] is False

    def test_hedging_is_not_a_defect_claim(self):
        votes = [self._green("could be more defensive; consider hardening"),
                 self._green("ok looks fine"), self._green("no problems seen")]
        assert iv.attest_consistency(votes, self.M3)["block"] is False

    def test_errored_vote_is_not_inspected(self):
        assert iv.attest_consistency([ERR, self._green("docs only"),
                                      self._green("no issue")], self.M3)["block"] is False


class TestGreenVotesAreNotAskedForDefectWords:
    """The fix for PR #64, and it is upstream of the gate.

    `build_system_prompt` used to ask a green vote to say "WHAT was checked +
    why no defect". The gate matches defect terms literally, so the protocol
    itself manufactured "no fail-open" and the gate discarded the vote — turning
    a UNANIMOUS 3/3 approval into a fail-closed BLOCK, and punishing the most
    specific reviewer while the vaguest one passed.

    Teaching the gate to read negation was tried and withdrawn: the panel found
    seven laundering paths in five rounds (see TestConsistencyMatchIsLiteral).
    So the ambiguous phrasing is prevented instead of interpreted."""

    def test_prompt_forbids_defect_terms_in_a_green_reason(self):
        prompt = iv.build_system_prompt("ch-1")
        assert "EVEN NEGATED" in prompt
        assert "no race condition" in prompt      # the worked counter-example

    def test_prompt_still_demands_the_refutation_schema(self):
        assert "path/file:line" in iv.build_system_prompt("ch-1")

    def test_prompt_still_carries_the_proof_of_check_challenge(self):
        assert "ch-1-<tier>" in iv.build_system_prompt("ch-1")


class TestConsistencyMatchIsLiteral:
    """The gate must keep matching defect terms LITERALLY.

    Every string below was proposed by the review panel against a version of
    this gate that tried to read negation, and every one was a real laundering
    path — an inconsistent green vote naming a live defect that the gate would
    have accepted. They are pinned here so a future attempt to make the gate
    "smarter" has to break this test first and explain why.

    `no fail-open` is in the list deliberately: it is a genuine assertion of
    ABSENCE and it still blocks. That is the accepted cost of a literal match,
    and the reason the prompt now steers green votes away from the phrasing."""

    M = ["m-a", "m-b", "m-c"]

    @staticmethod
    def _votes(reason):
        return [{"ok": True, "v": {"refuted": False, "reason": reason, "confidence": "medium"}},
                {"ok": True, "v": {"refuted": False, "reason": "docs only", "confidence": "low"}},
                {"ok": True, "v": {"refuted": False, "reason": "reviewed diff", "confidence": "low"}}]

    @pytest.mark.parametrize("reason", [
        "no fail-open",                                              # the false positive, accepted
        "no auth: privilege escalation via /admin",                  # negator binds another noun
        "without auth privilege escalation",                         # same, unpunctuated
        "no injection protection: raw query parameter reaches SQL",   # head of phrase is the DEFENCE
        "not regression-free",                                       # the absence-suffix, negated
        "no regression and an actual injection",                     # live conjunct
        "no injection: raw query reaches SQL",                       # colon elaboration
        "not without injection",                                     # double negation
        "auth bypass when key is None",                              # plain claim
    ])
    def test_every_defect_term_in_a_green_vote_blocks(self, reason):
        assert iv.attest_consistency(self._votes(reason), self.M)["block"] is True

    def test_a_reason_with_no_defect_term_passes(self):
        assert iv.attest_consistency(
            self._votes("throttle + lock ordering verified"), self.M)["block"] is False

    def test_the_block_quotes_the_clause_not_just_the_word(self):
        """Deciding whether a block was a real inconsistency or a phrasing
        artefact took reading the raw job log by hand. The summary now carries
        enough to tell them apart."""
        out = iv.attest_consistency(self._votes("checked writes; data loss on retry"), self.M)
        assert out["block"] is True
        assert "data loss on retry" in out["reason"]


class TestSelftestAttestsOnlyWhatItRan:
    """`--selftest` prints the functions it checked and CI reads that line as
    the attestation, so the print must not outlive the code.

    Editing the consistency block once sliced past its own boundary and deleted
    the auth_header, review_range, normalize_base and _api_url coverage — including the
    guard keeping a non-hex VERIFIER_HEAD_SHA out of a git argv — while the
    print went on naming all three. A false green on a security gate, which is
    the very defect class this file exists to catch.

    THESE TESTS RUN selftest AND COUNT CALLS. The first version searched its
    SOURCE for "auth_header(" — which the summary string it prints contains, so
    deleting every real call still passed. A check that cannot fail for the
    reason it exists is the same defect it is meant to catch, one level up."""

    #: Exactly the functions the closing summary claims were checked.
    ATTESTED = ("decide", "model_matches", "require_approvals", "attest_reasons",
                "attest_proof", "attest_consistency", "auth_header", "review_range",
                "normalize_base", "_api_url")

    @staticmethod
    def _run_recording(monkeypatch):
        """Run selftest() with every attested function wrapped, and report what
        was actually invoked (plus the argv-critical env review_range saw)."""
        called: set[str] = set()
        head_shas: list[str] = []

        def wrap(name, original):
            def recorder(*args, **kwargs):
                called.add(name)
                if name == "review_range":
                    import os

                    head_shas.append(os.environ.get("VERIFIER_HEAD_SHA", ""))
                return original(*args, **kwargs)
            return recorder

        for name in TestSelftestAttestsOnlyWhatItRan.ATTESTED:
            monkeypatch.setattr(iv, name, wrap(name, getattr(iv, name)))
        iv.selftest()
        return called, head_shas

    def test_every_function_the_summary_names_is_actually_called(self, monkeypatch, capsys):
        called, _ = self._run_recording(monkeypatch)
        missing = set(self.ATTESTED) - called
        assert not missing, f"--selftest claims to check {sorted(missing)}, but never calls them"

    def test_the_summary_names_exactly_what_ran(self, monkeypatch, capsys):
        """And nothing is exercised that the summary forgets to mention."""
        self._run_recording(monkeypatch)
        summary = capsys.readouterr().out
        for name in self.ATTESTED:
            assert f"{name}()" in summary, f"{name} runs but the attestation omits it"

    def test_the_shell_injection_guard_runs_against_the_malicious_value(self, monkeypatch, capsys):
        """Not that the string appears somewhere — that review_range was
        actually entered while it was set."""
        _, head_shas = self._run_recording(monkeypatch)
        assert any("rm -rf /" in sha for sha in head_shas), (
            "review_range() is never exercised with a non-hex VERIFIER_HEAD_SHA")

class TestAuthHeader:
    """The gateway's auth header is configurable because the deployed adapter runs
    providers.openai with authMode="forward" and reserves Authorization for
    upstream forwarding: Bearer answers 401 "opencodex API key required" for
    every model, X-OpenCodex-API-Key answers 200. Verified live."""

    @pytest.fixture(autouse=True)
    def _valid_key(self, monkeypatch):
        monkeypatch.setattr(iv, "KEY", "test-verifier-key")  # pragma: allowlist secret

    def test_defaults_to_bearer(self, monkeypatch):
        monkeypatch.delenv("VERIFIER_AUTH_HEADER", raising=False)
        assert "Authorization" in iv.auth_header()

    def test_custom_header_replaces_authorization(self, monkeypatch):
        monkeypatch.setenv("VERIFIER_AUTH_HEADER", "X-OpenCodex-API-Key")
        assert list(iv.auth_header()) == ["X-OpenCodex-API-Key"]

    def test_empty_variable_falls_back(self, monkeypatch):
        # Actions injects an EMPTY STRING for an unset repo variable; .get() with
        # a default would keep "" and send a nameless header.
        monkeypatch.setenv("VERIFIER_AUTH_HEADER", "")
        assert "Authorization" in iv.auth_header()

    def test_authorization_by_name_is_the_default_form(self, monkeypatch):
        monkeypatch.setenv("VERIFIER_AUTH_HEADER", "authorization")
        assert "Authorization" in iv.auth_header()

    def test_minimum_length_key_can_carry_an_ordinary_valid_vote(self, monkeypatch):
        monkeypatch.setattr(iv, "KEY", "12345678")  # pragma: allowlist secret
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        verdict = json.dumps({
            "refuted": False,
            "confidence": "high",
            "reason": "reviewed the candidate and found no concrete defect",
            "defects": [],
            "proof": "challenge-1",
        })
        wire = _FakeWire(_sse_events([
            {"type": "response.output_text.delta", "delta": verdict},
            {"type": "response.completed", "response": {"id": "resp-boundary"}},
        ]))
        monkeypatch.setattr(iv, "_urlopen", lambda req, timeout=None: wire)

        out = iv.attempt_once("model", "system", "user")

        assert out["ok"] is True
        assert out["v"]["refuted"] is False

    @pytest.mark.parametrize("name", [
        "Host",
        "Content-Type",
        "Proxy-Authorization",
        "Cookie",
        "Bad Header",
        "X-Key\r\nInjected: yes",
    ])
    def test_unsafe_header_is_guarded_at_both_network_sinks(self, monkeypatch, name):
        monkeypatch.setenv("VERIFIER_AUTH_HEADER", name)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        opened = False

        def forbidden_urlopen(*args, **kwargs):
            nonlocal opened
            opened = True
            raise AssertionError("verifier opened I/O with an unsafe credential header")

        monkeypatch.setattr(iv, "_urlopen", forbidden_urlopen)
        with pytest.raises(iv.ProviderConfigError, match="VERIFIER_AUTH_HEADER"):
            iv._http_json("/models")
        with pytest.raises(iv.ProviderConfigError, match="VERIFIER_AUTH_HEADER"):
            iv._responses_attempt("model", "system", "user")

        assert opened is False

    def test_credentialed_main_refuses_unsafe_header_before_diff_io(
            self, monkeypatch, capsys):
        credential = "unsafe-header-key-must-not-leak"  # pragma: allowlist secret
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        monkeypatch.setenv("VERIFIER_AUTH_HEADER", "Host")
        monkeypatch.setattr(
            iv, "build_diff",
            lambda: pytest.fail("verifier read the diff with an unsafe credential header"),
        )
        monkeypatch.setattr(iv.sys, "argv", ["independent_verify.py"])

        assert iv.main() == 1
        captured = capsys.readouterr()
        assert "VERIFIER_AUTH_HEADER" in captured.err
        assert credential not in captured.out + captured.err

    @pytest.mark.parametrize("credential", [
        "short7",
        "copied-key\n",
        "copied-key\r",
        "copied key",
        "copied-kéy",
    ])
    def test_unsafe_key_is_guarded_at_main_and_both_network_sinks(
            self, monkeypatch, capsys, credential):
        monkeypatch.setattr(iv, "KEY", credential)
        monkeypatch.setattr(iv, "BASE", TEST_BASE_URL)
        monkeypatch.setenv("VERIFIER_AUTH_HEADER", "X-OpenCodex-API-Key")
        opened = False

        def forbidden_urlopen(*args, **kwargs):
            nonlocal opened
            opened = True
            raise AssertionError("verifier opened I/O with an unsafe credential")

        monkeypatch.setattr(iv, "_urlopen", forbidden_urlopen)
        for request in (
            lambda: iv._http_json("/models"),
            lambda: iv._responses_attempt("model", "system", "user"),
        ):
            with pytest.raises(iv.ProviderConfigError, match="API key") as caught:
                request()
            assert credential not in str(caught.value)
            assert repr(credential)[1:-1] not in str(caught.value)

        monkeypatch.setattr(
            iv, "build_diff",
            lambda: pytest.fail("verifier read the diff with an unsafe credential"),
        )
        monkeypatch.setattr(iv.sys, "argv", ["independent_verify.py"])
        assert iv.main() == 1
        captured = capsys.readouterr()
        rendered = captured.out + captured.err
        assert credential not in rendered
        assert repr(credential)[1:-1] not in rendered
        assert opened is False


class TestReviewRange:
    """The job runs from the DEFAULT BRANCH under pull_request_target, where
    HEAD *is* main. Without an explicit candidate sha the diff collapses to
    main...main, returns empty, and the panel goes permanently FAKE-GREEN --
    reproduced before this guard existed."""

    @staticmethod
    def _record_git(monkeypatch) -> list[list[str]]:
        """Record every subprocess argv rather than raising on one.

        Deliberately not a raising sentinel: `_sh` wraps subprocess.run in a
        bare `except Exception` and converts whatever it catches into DiffError
        -- so a sentinel that raised would be laundered into the very exception
        these tests expect, and they would pass for the wrong reason twice
        over. Recording keeps the assertion about REACHING git separate from
        the assertion about the error."""
        calls: list[list[str]] = []

        class _Proc:
            returncode = 0
            stdout = b"0" * 40
            stderr = b""

        def fake_run(args, *_a, **_kw):
            calls.append(list(args))
            return _Proc()

        monkeypatch.setattr(iv.subprocess, "run", fake_run)
        return calls

    def test_non_hex_sha_is_rejected_before_reaching_git(self, monkeypatch):
        # The name claims two things -- rejected, and rejected BEFORE git sees
        # it. Asserting only `raises(DiffError)` checked neither: with the
        # 40-hex guard deleted, `git merge-base` fails on the garbage ref and
        # _sh(required=True) raises DiffError anyway. Verified by mutation --
        # the guard removed, all 44 tests stayed green. Both halves are now
        # asserted, and the argv assertion is what the name is really about.
        calls = self._record_git(monkeypatch)
        monkeypatch.setenv("VERIFIER_HEAD_SHA", "not-a-sha; rm -rf /")
        with pytest.raises(iv.DiffError, match="not a 40-hex sha"):
            iv.review_range()
        assert calls == [], f"the unvalidated sha was passed to git: {calls}"

    def test_short_sha_is_rejected(self, monkeypatch):
        calls = self._record_git(monkeypatch)
        monkeypatch.setenv("VERIFIER_HEAD_SHA", "8d85424")
        with pytest.raises(iv.DiffError, match="not a 40-hex sha"):
            iv.review_range()
        assert calls == [], f"the unvalidated sha was passed to git: {calls}"

    def test_a_candidate_that_is_an_ancestor_of_the_base_blocks(self, monkeypatch):
        """Nothing to review is a FAULT in a PR context, not an approval.

        Untested until now: deleting the ancestor check left all 44 tests
        green. It is the guard that stops a collapsed range greening the
        panel, which is the failure this whole class exists to describe."""
        sha = "a" * 40
        monkeypatch.setenv("VERIFIER_HEAD_SHA", sha)
        monkeypatch.setattr(iv, "_sh", lambda _args, **_kw: sha + "\n")
        with pytest.raises(iv.DiffError, match="ancestor"):
            iv.review_range()

    def test_an_empty_merge_base_blocks(self, monkeypatch):
        monkeypatch.setenv("VERIFIER_HEAD_SHA", "a" * 40)
        monkeypatch.setattr(iv, "_sh", lambda _args, **_kw: "  \n")
        with pytest.raises(iv.DiffError, match="empty merge-base"):
            iv.review_range()

    def test_a_candidate_ahead_of_the_base_is_accepted(self, monkeypatch):
        """The complement, so the two blocking tests above cannot be satisfied
        by a guard that simply refuses everything."""
        head, mb = "b" * 40, "c" * 40
        monkeypatch.setenv("VERIFIER_HEAD_SHA", head)
        monkeypatch.setattr(
            iv, "_sh",
            lambda args, **_kw: (mb + "\n") if args[1] == "merge-base" else (head + "\n"))
        assert iv.review_range() == (mb, head)


class TestStepSummary:
    """The panel must publish its findings where a reviewer will see them.

    GITHUB_STEP_SUMMARY renders as markdown on the run page, one click from the
    pull request's check. The reason text is model-authored from an untrusted
    diff, so it is hostile input to the RENDERER and must not break out of its
    table cell."""

    OK = {"ok": True, "v": {"refuted": False, "confidence": "high", "reason": "docs only change"}}
    REF = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "auth bypass"}}
    ERR = {"ok": False, "reason": "API 504"}
    MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "deepseek"]

    def _write(self, tmp_path, monkeypatch, votes, gates, blocked):
        target = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
        iv.write_step_summary(votes, self.MODELS, gates, blocked=blocked)
        return target.read_text(encoding="utf-8")

    def test_approved_run_lists_every_voice(self, tmp_path, monkeypatch):
        out = self._write(tmp_path, monkeypatch, [self.OK, self.OK, self.OK],
                          [("required-approver", {"block": False, "reason": "sol approves"})], False)
        assert "**Verdict: APPROVED**" in out
        for model in self.MODELS:
            assert model in out
        assert out.count("approves") >= 3

    def test_blocked_run_names_the_gate_that_blocked(self, tmp_path, monkeypatch):
        out = self._write(tmp_path, monkeypatch, [self.REF, self.OK, self.OK],
                          [("required-approver", {"block": True, "reason": "sol vetoes"})], True)
        assert "**Verdict: BLOCKED**" in out
        assert "blocked" in out and "sol vetoes" in out

    def test_a_voice_that_errored_is_shown_not_hidden(self, tmp_path, monkeypatch):
        # A panel that silently drops a failed voice looks like a smaller panel
        # that agreed, which is the opposite of what happened.
        out = self._write(tmp_path, monkeypatch, [self.OK, self.ERR, self.OK],
                          [("required-approver", {"block": False, "reason": "ok"})], False)
        assert "no vote" in out and "API 504" in out

    def test_written_on_both_paths(self, tmp_path, monkeypatch):
        for blocked in (True, False):
            out = self._write(tmp_path, monkeypatch, [self.OK] * 3,
                              [("g", {"block": blocked, "reason": "r"})], blocked)
            assert out.strip(), "a panel that only explains itself when it blocks is unreadable"

    def test_model_text_cannot_break_the_table(self, tmp_path, monkeypatch):
        hostile = {"ok": True, "v": {"refuted": False, "confidence": "high",
                                     "reason": "a|b\n| evil | row |\n`x`"}}
        out = self._write(tmp_path, monkeypatch, [hostile, self.OK, self.OK],
                          [("g", {"block": False, "reason": "r"})], False)
        body = [line for line in out.splitlines() if line.startswith("| 1 |")]
        assert len(body) == 1, "the reason injected extra table rows"
        assert "\\|" in body[0] and "\\`" in body[0]

    def test_absent_env_is_a_no_op(self, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        iv.write_step_summary([self.OK], ["m"], [("g", {"block": False, "reason": "r"})],
                              blocked=False)      # must not raise

    def test_an_unwritable_target_never_breaks_the_verdict(self, tmp_path, monkeypatch):
        # The REPORT must never turn a clean verdict into a red job.
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "no-such-dir" / "s.md"))
        iv.write_step_summary([self.OK], ["m"], [("g", {"block": False, "reason": "r"})],
                              blocked=False)      # must not raise


class TestMainFailsClosedOnAnUnreviewableDiff:
    """main()'s own refusals, none of which had a test.

    The parts of this script are well covered; the ASSEMBLY was not. A mutation
    run drove fourteen edits through the suite: eleven were caught, and the
    three survivors were all here or in review_range -- including the guard the
    source itself calls "the fake-green path". These tests drive main() with
    build_diff stubbed so the refusal, not the plumbing, is what is asserted."""

    @staticmethod
    def _armed(monkeypatch):
        """A run that has a key and is not a selftest -- i.e. past every early
        return, so what follows is genuinely main()'s diff handling."""
        monkeypatch.setattr(iv, "KEY", "test-key")
        monkeypatch.setattr(iv.sys, "argv", ["independent_verify.py"])

    def test_empty_diff_with_an_explicit_candidate_sha_blocks(self, monkeypatch, capsys):
        # THE FAKE-GREEN PATH. Under pull_request_target the job runs from the
        # default branch, where HEAD is main: if the range collapses, the diff
        # is empty and greening it approves the candidate without reading it.
        self._armed(monkeypatch)
        monkeypatch.setattr(iv, "build_diff", lambda: "")
        monkeypatch.setenv("VERIFIER_HEAD_SHA", "a" * 40)
        assert iv.main() == 1
        assert "review range" in capsys.readouterr().err

    def test_empty_diff_without_a_candidate_sha_is_green(self, monkeypatch, capsys):
        # The complement: with no candidate sha there is genuinely nothing to
        # review, and blocking every such run would make the gate unusable.
        self._armed(monkeypatch)
        monkeypatch.setattr(iv, "build_diff", lambda: "")
        monkeypatch.delenv("VERIFIER_HEAD_SHA", raising=False)
        assert iv.main() == 0
        assert "No diff to review" in capsys.readouterr().out

    def test_a_failed_diff_assembly_blocks(self, monkeypatch, capsys):
        self._armed(monkeypatch)

        def boom() -> str:
            raise iv.DiffError("merge-base unavailable")

        monkeypatch.setattr(iv, "build_diff", boom)
        assert iv.main() == 1
        assert "fail-closed" in capsys.readouterr().err

    def test_a_fork_run_without_a_key_blocks(self, monkeypatch, capsys):
        # Secrets are withheld from fork PRs, so "no key" there is an untrusted
        # origin, not the operator's documented residual.
        monkeypatch.setattr(iv, "KEY", "")
        monkeypatch.setattr(iv.sys, "argv", ["independent_verify.py"])
        monkeypatch.setenv("VERIFIER_REQUIRE_KEY", "true")
        assert iv.main() == 1
        assert "required verifier run" in capsys.readouterr().err

    def test_a_same_repo_run_without_a_key_reports_the_residual(self, monkeypatch, capsys):
        monkeypatch.setattr(iv, "KEY", "")
        monkeypatch.setattr(iv.sys, "argv", ["independent_verify.py"])
        monkeypatch.delenv("VERIFIER_REQUIRE_KEY", raising=False)
        assert iv.main() == 0
        assert "RESIDUAL" in capsys.readouterr().out


class TestPanelComposition:
    """Independence is a property of the PANEL'S COMPOSITION, and this script
    cannot see it.

    Two attempts to derive a vendor from a model id were refuted by the panel
    itself, on the pull request that carried each. The first keyed a single
    token, so `gpt-5.6-sol` -> "gpt" and `openai/gpt-4.1-mini` -> "openai" made
    one vendor look like two. The second used token sets, and the live
    catalogue killed it from the other side: `nvidia/` is a HOST prefix, so
    `nvidia/meta/muse-glimmer-30b` and `nvidia/deepseek-ai/deepseek-v4` are Meta
    and DeepSeek sharing a token. Wrong in both directions -- the id does not
    carry the fact.

    Each voice is now an inference-server GROUP that rotates over its own
    members. Probed live: a call to `combo/SOTA-A` answers `"model":
    "combo/SOTA-A"`, never the member that served it. So the operator attests
    that the groups are independent, and the script enforces the checkable part
    -- that two voices are not the same group -- and states the voices instead
    of claiming a vendor.
    """

    GROUPS = ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"]

    def test_three_distinct_groups_are_three_voices(self):
        assert iv.require_distinct_voices(self.GROUPS)["block"] is False

    def test_the_same_group_twice_is_one_opinion(self):
        out = iv.require_distinct_voices(["combo/SOTA-A", "combo/SOTA-A", "combo/SOTA-C"])
        assert out["block"] is True
        assert "more than once" in out["reason"]
        assert "combo/SOTA-A" in out["reason"], "the refusal must name the repeated voice"

    def test_all_three_identical_blocks(self):
        assert iv.require_distinct_voices(["combo/SOTA-A"] * 3)["block"] is True

    def test_the_reason_names_the_voices_on_the_passing_path(self):
        # A gate that explains itself only when it blocks leaves a reader unable
        # to tell "three voices agreed" from "the gate never ran".
        r = iv.require_distinct_voices(self.GROUPS)
        assert all(g in r["reason"] for g in self.GROUPS)

    def test_group_ids_are_matched_exactly_not_by_prefix(self):
        # SOTA-A and SOTA-C share every character but the last. A prefix match
        # would let the required approver be satisfied by the wrong group.
        assert iv.model_matches("combo/SOTA-A", "combo/SOTA-A") is True
        assert iv.model_matches("combo/SOTA-C", "combo/SOTA-A") is False
        assert iv.model_matches("combo/SOTA-B", "combo/SOTA-A") is False

    def test_approving_models_ignores_errored_and_refuting_voices(self):
        assert iv.approving_models([A, ERR, RF], self.GROUPS) == ["combo/SOTA-A"]
        assert iv.approving_models([A, A2, A2], self.GROUPS) == self.GROUPS

    def test_group_a_plus_one_other_is_the_quorum(self):
        # The user's rule: A must always agree, plus either B or C.
        assert iv.require_approvals([A, A2, ERR], self.GROUPS, "combo/SOTA-A", 1)["block"] is False
        assert iv.require_approvals([A, ERR, A2], self.GROUPS, "combo/SOTA-A", 1)["block"] is False
        # A alone is not enough...
        assert iv.require_approvals([A, ERR, ERR], self.GROUPS, "combo/SOTA-A", 1)["block"] is True
        # ...and B+C without A is not enough either.
        assert iv.require_approvals([ERR, A, A2], self.GROUPS, "combo/SOTA-A", 1)["block"] is True

    def test_a_refusing_group_a_vetoes_however_many_others_agree(self):
        assert iv.require_approvals([RF, A, A2], self.GROUPS, "combo/SOTA-A", 1)["block"] is True


class TestResolutionFailsClosed:
    """A voice that cannot be resolved is a panel that cannot be assembled.

    Substitution was fail-open: an id the account does not serve printed a
    WARNING and was replaced by the newest available model, so the panel
    reviewed with voices the operator never chose and the run went green. Two
    cheap ways to trigger it -- a group not yet published to /v1/models, and a
    transposition (`combo/SOTA-B` and `combo/SOAT-B` differ by two characters,
    and only combo/SOTA-B exists: the registry shipped the SOAT-B typo first
    and later renamed it, so the retired spelling is exactly the id a careless
    config still carries)."""

    CATALOGUE = ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C", "gpt-5.6-sol"]

    def _catalogue(self, monkeypatch, ids):
        monkeypatch.setattr(iv, "fetch_model_ids", lambda: (ids, ""))
        monkeypatch.delenv("VERIFIER_MODEL", raising=False)

    def test_all_present_resolves_exactly(self, monkeypatch):
        self._catalogue(monkeypatch, self.CATALOGUE)
        assert iv.resolve_panel_models(3, ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"]) == [
            "combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"]

    def test_an_unpublished_group_refuses(self, monkeypatch):
        self._catalogue(monkeypatch, ["gpt-5.6-sol"])
        with pytest.raises(iv.ProviderConfigError, match="not enabled on this account"):
            iv.resolve_panel_models(3, ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"])

    def test_a_transposed_id_refuses_rather_than_substituting(self, monkeypatch):
        self._catalogue(monkeypatch, self.CATALOGUE)
        with pytest.raises(iv.ProviderConfigError, match="combo/SOAT-B"):
            iv.resolve_panel_models(3, ["combo/SOTA-A", "combo/SOAT-B", "combo/SOTA-C"])

    def test_the_refusal_lists_what_is_available(self, monkeypatch):
        self._catalogue(monkeypatch, self.CATALOGUE)
        with pytest.raises(iv.ProviderConfigError, match="combo/SOTA-A"):
            iv.resolve_panel_models(3, ["nope/one", "nope/two", "nope/three"])


class TestConsistencyGateNegation:
    """A green vote naming a defect must still block. An ALL-CLEAR must not.

    The gate matched defect words by substring, so "security gates reviewed; no
    concrete regression" -- an explicit statement that nothing is wrong -- was
    scored as a defect claim. On run 32181953531 all three voices approved and
    the panel blocked anyway, on that word.

    These tests pin both directions, because the loosening is the dangerous
    half: the control exists because a panelist once returned refuted=false
    with a reason that named two concrete defects.
    """

    M3 = ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"]

    @staticmethod
    def _green(reason: str, refuted: bool = False) -> dict:
        return {"ok": True, "v": {"refuted": refuted, "reason": reason, "confidence": "high"}}

    def test_a_negated_defect_word_still_blocks(self):
        # This asserted `is False` while a negation window was in place. That
        # window measured WEAKER THAN MAIN on 71 of 165 defect-affirming reasons
        # and was withdrawn; the match is literal again. The false block is a
        # real cost, paid deliberately, and relieved in build_system_prompt by
        # asking a green vote for no defect term at all -- not even negated.
        votes = [self._green("security gates reviewed; no concrete regression"),
                 self._green("docs only"), self._green("looks fine")]
        assert iv.attest_consistency(votes, self.M3)["block"] is True

    def test_an_unnegated_defect_word_still_blocks(self):
        votes = [self._green("this introduces a regression in the dispatcher"),
                 self._green("docs only"), self._green("fine")]
        out = iv.attest_consistency(votes, self.M3)
        assert out["block"] is True
        assert "regression" in out["reason"]

    def test_one_negation_does_not_launder_a_later_claim(self):
        # The dangerous case for this change: a reason that opens with an
        # all-clear and then names a real defect must still block.
        votes = [self._green("no bypass here, but there is an injection in the parser"),
                 self._green("docs only"), self._green("fine")]
        assert iv.attest_consistency(votes, self.M3)["block"] is True

    def test_the_original_attack_still_blocks(self):
        # The vote the gate was built for: green, with two concrete defects.
        votes = [self._green("auth bypass when key is None and a race condition on refresh"),
                 self._green("docs only"), self._green("fine")]
        assert iv.attest_consistency(votes, self.M3)["block"] is True

    def test_a_refuting_vote_may_name_defects(self):
        votes = [self._green("fail-open on missing header", refuted=True),
                 self._green("docs only"), self._green("no issue found")]
        assert iv.attest_consistency(votes, self.M3)["block"] is False



class TestDeclaredDefectLedger:
    """The MECHANISM: the model declares its findings in a typed field and the
    gate compares that field with the boolean.

    Three predecessors inferred a fact from a string -- a vendor from a model
    id, a vendor from token sets, a defect claim from a negation window -- and
    each was defeated by an input its author had not imagined. This channel
    infers nothing: ``len(defects) > 0`` next to ``refuted is False`` is a
    comparison of two values the model itself supplied about one fact. Its
    input domain is {list of strings} + {everything else}, both handled."""

    M3 = ["m-a", "m-b", "m-c"]

    @staticmethod
    def _vote(reason="reviewed diff, behaviour unchanged", refuted=False, **kw):
        v = {"refuted": refuted, "reason": reason, "confidence": "high"}
        if "defects" in kw:
            v["defects"] = kw["defects"]
        return {"ok": True, "v": v}

    def test_reads_a_well_formed_ledger(self):
        assert iv.declared_defects({"defects": []}) == ("ok", [])
        assert iv.declared_defects({"defects": ["a.py:1 — x — y"]}) == ("ok", ["a.py:1 — x — y"])

    def test_absent_or_garbage_is_reported_not_guessed(self):
        assert iv.declared_defects({})[0] == "missing"
        assert iv.declared_defects(None)[0] == "missing"
        assert iv.declared_defects({"defects": "auth bypass"})[0] == "malformed"
        assert iv.declared_defects({"defects": None})[0] == "malformed"
        assert iv.declared_defects({"defects": {"a": 1}})[0] == "malformed"
        assert iv.declared_defects({"defects": [{"file": "a.py"}]})[0] == "malformed"

    def test_empty_sentinels_are_an_empty_ledger(self):
        assert iv.declared_defects({"defects": ["none", "", " N/A ", "nothing found"]}) == ("ok", [])
        assert iv.declared_defects({"defects": ["none", "a.py:2 — real"]})[1] == ["a.py:2 — real"]

    @pytest.mark.parametrize("key", ["defects", "Defects", "DEFECTS", " defects ",
                                     "defect", "findings", "defects_found"])
    def test_a_key_spelling_slip_does_not_silently_disarm_the_gate(self, key):
        # A mis-cased key read as "absent" would turn the gate into a silent
        # no-op on exactly the votes that answered the schema.
        assert iv.declared_defects({key: ["auth bypass when key is None"]}) == (
            "ok", ["auth bypass when key is None"])

    def test_a_declared_defect_on_a_green_vote_blocks(self):
        votes = [self._vote(defects=[]),
                 self._vote("looks fine to me",
                            defects=["api.py:7 — authz skipped — any user reads /admin"]),
                 self._vote(defects=[])]
        out = iv.attest_consistency(votes, self.M3)
        assert out["block"] is True
        assert "m-b" in out["reason"] and "ledger declares" in out["reason"]

    def test_the_ledger_blocks_even_when_the_prose_is_spotless(self):
        # The point of the field: no phrasing of `reason` hides the finding,
        # because this rung never reads `reason`.
        votes = [self._vote("all good, nothing of note", defects=["x.py:1 — y — z"]),
                 self._vote(defects=[]), self._vote(defects=[])]
        assert iv.attest_consistency(votes, self.M3)["block"] is True

    def test_a_bare_string_sentinel_is_an_empty_ledger(self):
        # '"defects": "none"' cannot name a defect, so reading it as [] is safe
        # in the P1 direction and removes the likeliest rollout wedge.
        assert iv.declared_defects({"defects": "none"}) == ("ok", [])
        assert iv.declared_defects({"defects": " N/A "}) == ("ok", [])

    def test_a_second_accepted_key_cannot_hide_a_finding(self):
        assert iv.declared_defects({"defects": [], "findings": ["auth bypass"]}) == (
            "ok", ["auth bypass"])
        votes = [self._vote(defects=[]), self._vote(defects=[]), self._vote(defects=[])]
        votes[0]["v"]["findings"] = ["svc.py:2 — token unchecked"]
        assert iv.attest_consistency(votes, self.M3)["block"] is True

    def test_a_malformed_ledger_fails_closed(self):
        votes = [self._vote(defects="auth bypass on /admin"), self._vote(defects=[]),
                 self._vote(defects=[])]
        out = iv.attest_consistency(votes, self.M3)
        assert out["block"] is True and "not a" in out["reason"]

    def test_an_absent_ledger_is_tolerated_by_default(self):
        # P2 / rollout: a voice that has not seen the new prompt must not block
        # a unanimous green. The prose tripwire still applies to it.
        votes = [self._vote(), self._vote(), self._vote()]
        assert iv.attest_consistency(votes, self.M3)["block"] is False

    def test_an_absent_ledger_blocks_when_the_operator_requires_it(self):
        votes = [self._vote(), self._vote(), self._vote()]
        out = iv.attest_consistency(votes, self.M3, require_ledger=True)
        assert out["block"] is True and "unverifiable green" in out["reason"]

    def test_a_refuting_vote_may_declare_defects(self):
        votes = [self._vote("fail-open on missing header", refuted=True,
                            defects=["h.py:3 — fail-open"]),
                 self._vote(defects=[]), self._vote(defects=[])]
        assert iv.attest_consistency(votes, self.M3)["block"] is False

    def test_an_errored_vote_has_no_ledger_to_read(self):
        assert iv.attest_consistency([ERR, self._vote(defects=[]), self._vote(defects=[])],
                                     self.M3, require_ledger=True)["block"] is False


class TestSharedAttackCorpus:
    """The shared corpus, scored through the real gate in all three modes.

    Mode B (the voice declares its finding) is what the design ships for: 7/7.
    Mode C (the voice hides the finding in prose and declares []) is what the
    demoted tripwire alone can do: 4/7, with three holes pinned as holes. The
    numbers are asserted here so that no future edit can silently claim the
    mode-B score for mode C."""

    M3 = ["m-a", "m-b", "m-c"]

    MUST_BLOCK = [
        "auth bypass when key is None and a race condition on refresh",
        "without fixing auth bypass",
        "merged without addressing the injection",
        "not fixed: privilege escalation in admin",
        "no bypass here, but there is an injection in the parser",
        "looks good; minor: unauthenticated /readyz leaks the key",
        "approved, though the deadlock on shutdown remains",
    ]
    # Carries no term from _DEFECT_WORDS, which is exactly what
    # build_system_prompt now asks a green vote to produce.
    MUST_PASS = [
        "docs only change",
        "reviewed diff, behaviour unchanged",
        "could be more defensive; consider hardening",
        "checked auth paths, no issue",
        "gates re-checked, all five fire",
    ]
    # Approving reasons that BLOCK because rung 4 matches literally. This is the
    # accepted P2 cost of withdrawing the negation window, measured rather than
    # asserted, and the reason the relief was moved into the prompt. The
    # operator remedy is a re-vote in which the voice names code paths instead.
    KNOWN_FALSE_BLOCKS = [
        "none of the auth paths bypass the check",
        "security gates reviewed; no concrete regression",
        "no regression, no bypass, no injection",
    ]
    # Formerly asserted as PASSING here, under a comment telling the next
    # reviewer not to repair them. They were never pre-existing holes: main
    # blocked all three, this branch briefly did not, and pinning them meant CI
    # enforced the bypass. combo/SOTA-A refuted it.
    FORMER_HOLES = [
        "without fixing auth bypass",
        "merged without addressing the injection",
        "not fixed: privilege escalation in admin",
    ]
    KNOWN_TRIPWIRE_HOLES = FORMER_HOLES

    @staticmethod
    def _panel(reason, defects):
        clean = {"ok": True, "v": {"refuted": False, "reason": "docs only change",
                                   "confidence": "high", "defects": []}}
        return [{"ok": True, "v": {"refuted": False, "reason": reason,
                                   "confidence": "high", "defects": list(defects)}},
                clean, clean]

    @pytest.mark.parametrize("reason", MUST_BLOCK)
    def test_mode_b_declared_finding_always_blocks(self, reason):
        # The voice routes its finding into the ledger. 7/7, independent of
        # phrasing, negation, inflection, unicode or language.
        assert iv.attest_consistency(self._panel(reason, [reason]), self.M3)["block"] is True

    @pytest.mark.parametrize("reason", MUST_PASS)
    def test_p2_an_ordinary_approval_passes(self, reason):
        assert iv.attest_consistency(self._panel(reason, []), self.M3)["block"] is False

    @pytest.mark.parametrize("reason", KNOWN_FALSE_BLOCKS)
    def test_p2_known_false_block_is_a_cost_not_a_bug(self, reason):
        assert iv.attest_consistency(self._panel(reason, []), self.M3)["block"] is True

    @pytest.mark.parametrize("reason", [
        "auth bypass when key is None and a race condition on refresh",
        "no bypass here, but there is an injection in the parser",
        "looks good; minor: unauthenticated /readyz leaks the key",
        "approved, though the deadlock on shutdown remains",
    ])
    def test_mode_c_hidden_finding_what_the_tripwire_still_catches(self, reason):
        assert iv.attest_consistency(self._panel(reason, []), self.M3)["block"] is True

    @pytest.mark.parametrize("reason", KNOWN_TRIPWIRE_HOLES)
    def test_the_former_holes_now_block(self, reason):
        # These three used to be asserted as PASSING here, under a comment
        # telling the next reviewer not to repair them. That was wrong twice
        # over: they are not pre-existing holes, they were REGRESSIONS against
        # main -- main blocked all three -- and pinning them meant CI enforced
        # the bypass. combo/SOTA-A refuted it; measurement put the real scope at
        # 71 of 165 defect-affirming reasons, not 3. The negation window is gone
        # and rung 4 is main's literal scan again.
        assert iv.attest_consistency(self._panel(reason, []), self.M3)["block"] is True

    def test_p1_beats_p2_when_the_ledger_disagrees(self):
        # An ordinary approving reason with a declared finding blocks anyway.
        votes = self._panel("docs only change", ["svc.py:9 — token never checked"])
        assert iv.attest_consistency(votes, self.M3)["block"] is True


class TestDefectWordInflections:
    """Widening a published term to its ordinary inflections is MONOTONE: it can
    only turn a PASS into a BLOCK. These are measured breaks of the pre-existing
    bare-word form, found by the panel against three separate proposals.

    Both directions block now, and that is the point: rung 4 is a LITERAL match,
    so "no injections found" blocks exactly as "two sql injections remain" does.
    The relief for the false block lives in build_system_prompt, never here."""

    M3 = ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"]

    @staticmethod
    def _panel(reason: str, defects: list[str]) -> list[dict]:
        subject = {"ok": True, "v": {"refuted": False, "confidence": "high",
                                     "reason": reason, "defects": defects}}
        clean = {"ok": True, "v": {"refuted": False, "confidence": "high",
                                   "reason": "docs only change", "defects": []}}
        return [subject, clean, dict(clean)]

    @pytest.mark.parametrize("reason", [
        "auth is bypassed when key is None",
        "the expired token bypasses validation entirely",
        "the auth check is bypassable with a null key",
        "two sql injections in the parser remain unfixed",
        "deadlocks on shutdown remain",
        "the handler deadlocked on shutdown",
        "race conditions on refresh",
        "privilege escalations in admin",
        "the gate fails open when the header is missing",
        # combo/SOTA-A, run 32312272105: the enumerated form missed these three.
        # Not regressions -- main misses them too -- but a list of endings is a
        # list somebody has to keep complete. Every alternative is a stem now.
        "the header path failed open on a missing key",
        "the gate is failing open when the header is absent",
        "injecting untrusted input into the query builder",
    ])
    def test_inflected_defect_words_are_claims(self, reason):
        assert iv.attest_consistency(self._panel(reason, []), self.M3)["block"] is True

    @pytest.mark.parametrize("reason", [
        "no bypasses, no regressions",
        "no injections found",
        "no race conditions observed",
    ])
    def test_widening_does_not_un_negate_an_all_clear(self, reason):
        assert iv.attest_consistency(self._panel(reason, []), self.M3)["block"] is True


class TestRequiredLedgerMeansRequired:
    """Group A's refutation, as tests.

    With VERIFIER_REQUIRE_DEFECT_LIST=true the gate claimed every approving vote
    carries `defects: string[]`. It did not: a bare sentinel string
    (`"defects": "none"`) and an alias-only key (`"findings": []`) both satisfied
    it, because declared_defects() tolerates those shapes. Tolerating a shape is
    not the schema being answered.

    The tolerance itself is deliberate and stays: those sentinels cannot name a
    defect, and reading every key spelling is what stops an alias from hiding an
    entry. What changed is that TOLERATED is no longer mistaken for CONFORMANT.
    """

    M = ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"]

    @staticmethod
    def _green(**kw) -> dict:
        return {"ok": True, "v": {"refuted": False, "reason": "docs only change", **kw}}

    def test_an_array_is_conformant(self):
        assert iv.has_conformant_ledger({"defects": []}) is True
        assert iv.has_conformant_ledger({"defects": ["auth bypass on /admin"]}) is True

    def test_a_sentinel_string_is_not_an_array(self):
        for bad in ("none", "n/a", "", "nothing found"):
            assert iv.has_conformant_ledger({"defects": bad}) is False

    def test_an_alias_alone_does_not_answer_the_schema(self):
        assert iv.has_conformant_ledger({"findings": []}) is False
        assert iv.has_conformant_ledger({"defects_found": []}) is False

    def test_a_non_dict_vote_is_not_conformant(self):
        assert iv.has_conformant_ledger(None) is False
        assert iv.has_conformant_ledger("defects") is False

    def test_required_mode_refuses_every_non_conformant_shape(self):
        for bad in ({"defects": "none"}, {"findings": []}, {"defects": "n/a"},
                    {"defects_found": ["x"]}):
            votes = [self._green(**bad), self._green(defects=[]), self._green(defects=[])]
            assert iv.attest_consistency(votes, self.M, True)["block"] is True, bad

    def test_required_mode_accepts_a_conformant_empty_ledger(self):
        votes = [self._green(defects=[])] * 3
        assert iv.attest_consistency(votes, self.M, True)["block"] is False

    def test_the_alias_still_cannot_hide_an_entry(self):
        # The safety property the tolerance exists for must survive the fix: a
        # conformant empty `defects` PLUS an alias naming a defect still blocks.
        votes = [self._green(defects=[], findings=["auth bypass on /admin"]),
                 self._green(defects=[]), self._green(defects=[])]
        out = iv.attest_consistency(votes, self.M, True)
        assert out["block"] is True
        assert "bypass" in out["reason"]

    def test_the_switch_off_path_is_unchanged(self):
        # No forced rollout: a fork whose models answer with a sentinel keeps
        # working until it opts in.
        votes = [self._green(defects="none"), self._green(), self._green()]
        assert iv.attest_consistency(votes, self.M, False)["block"] is False


class TestDuplicateKeysCannotEraseADeclaration:
    """`json.loads` keeps the LAST of duplicate keys, silently.

    Measured before the hook existed: a verdict declaring a defect and repeating
    the key empty in the same object parsed to `defects == []`, and the gate
    PASSED it. The ledger compares two declarations about one fact; that is only
    meaningful if the object says one thing per key.
    """

    M3 = ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"]
    ATTACK = ('{"refuted": false, "confidence": "high", "reason": "docs only change", '
              '"defects": ["admin.py:83 - auth bypass"], "defects": []}')

    def test_a_repeated_ledger_key_is_refused_not_resolved(self):
        assert iv.parse_verdict(self.ATTACK) is None

    def test_a_repeated_key_anywhere_is_refused(self):
        # Not only the ledger: any duplicate makes the object ambiguous.
        assert iv.parse_verdict('{"refuted": false, "refuted": true, "reason": "x"}') is None

    def test_an_ordinary_verdict_still_parses(self):
        v = iv.parse_verdict('{"refuted": false, "confidence": "high", '
                             '"reason": "docs only change", "defects": []}')
        assert v == {"refuted": False, "confidence": "high",
                     "reason": "docs only change", "defects": []}

    def test_a_duplicate_inside_an_embedded_object_is_refused(self):
        assert iv.parse_verdict('prose {"refuted": false, "reason": "x", '
                                '"defects": ["a"], "defects": []} trailing') is None

    def test_the_unparsable_vote_is_discarded_not_counted(self):
        # _is_valid() is false for a None verdict, so the vote cannot approve,
        # cannot corroborate, and cannot feed attest_reasons.
        assert iv._is_valid({"ok": True, "v": iv.parse_verdict(self.ATTACK)}) is False


class TestEveryVocabularyAlternativeIsAStem:
    """No alternative may enumerate endings.

    The published vocabulary and the enforced regex are one list, but a list of
    inflections is still a list somebody has to keep complete -- and the next
    inflection is always the one nobody wrote down. `fails? open` missed "failed
    open" and "failing open"; `inject(?:ions|ion|ed|able|s)` missed "injecting".
    This test is what stops the enumeration coming back.
    """

    def test_no_alternative_enumerates_inflections(self):
        # The variable part must be \w* and may sit mid-alternative
        # (`fail\w* open`); what is banned is a CLOSED set of endings -- an
        # optional-letter suffix (`conditions?`) or a group of spelt-out
        # alternatives (`(?:ions|ion|ed|able|s)`). Those are the two forms that
        # leaked.
        offenders = [(term, alt) for term, alt in iv._DEFECT_VOCAB
                     if r"\w*" not in alt or "(?:" in alt
                     or re.search(r"[A-Za-z]\?", alt)]
        assert not offenders, (
            "these alternatives enumerate endings instead of stemming; a stem "
            f"cannot be incomplete the way a list can: {offenders}")

    def test_the_regex_is_built_from_the_published_list(self):
        assert iv._DEFECT_WORDS.pattern == (
            r"\b(" + "|".join(a for _, a in iv._DEFECT_VOCAB) + r")\b")

    def test_every_published_term_is_blocked_by_the_regex_it_publishes(self):
        missed = [t for t, _ in iv._DEFECT_VOCAB if not iv._DEFECT_WORDS.search(t)]
        assert not missed, missed


class TestWireFailover:
    """/responses is the PRIMARY wire — STREAMED; chat/completions the ONE-SHOT failover.

    Operator measurement over 13,815 logged gateway requests: 516 chat-protocol
    requests, none ever past 91s, 150 clustered at ~90s -- while `responses`
    runs to 380s freely through the same edge. The cluster spans Anthropic
    (combo/SOTA-B) and NVIDIA (combo/SOTA-C), unrelated upstreams, so it is the
    wire: chat-completions emits no bytes while a model thinks and an edge idle
    timeout collects the silence, while streaming /responses trickles ~2s
    heartbeats. A curl with no client timeout ran the same model over
    chat-completions for 458s to a clean [DONE], so the gateway has no wall of
    its own.

    Hence the order, and hence the SHAPE: the /responses providers reject the
    non-streaming form ("Input must be a list", "Stream must be set to true" --
    both live 400s), so the primary wire sends a message-list input with
    stream:true and folds the SSE reply (_sse_fold), deltas first because the
    completed object can arrive with an empty output array. Chat-first also
    poisoned its own rescue: the ~90s chat death marked the upstream failed at
    the edge, so the immediate /responses failover answered an instant 504.
    Responses-first has no such window: the wire that survives long thinking is
    the one every attempt starts on, and chat gets ONE shot only when
    /responses itself fails (5xx/network/torn stream, or 404/405 from a gateway
    that does not route it).

    Losing a voice is not free here: an errored voice never approves, so two of
    them take the quorum with them and the panel blocks on infrastructure alone.
    Observed: two runs on PR #64 where combo/SOTA-B and combo/SOTA-C both
    returned 504 and the panel decided on one voice.
    """

    VERDICT = ('{"refuted": false, "confidence": "high", "reason": "docs only change", '
               '"defects": [], "proof": "x-1"}')
    OK_CHAT = (200, {"choices": [{"message": {"content":
        '{"refuted": false, "confidence": "high", "reason": "docs only change", '
        '"defects": [], "proof": "x-1"}'}}]})
    # A representative healthy stream, as observed live: lifecycle events, the
    # verdict split across output_text deltas, and a terminal
    # response.completed whose response object ALSO carries the text (the
    # well-behaved shape; the empty-output shape gets its own test).
    OK_RESP = (200, [
        {"type": "response.created"},
        {"type": "response.in_progress"},
        {"type": "response.output_item.added"},
        {"type": "response.output_text.delta", "delta": VERDICT[:23]},
        {"type": "response.output_text.delta", "delta": VERDICT[23:]},
        {"type": "response.completed",
         "response": {"id": "resp_ok", "output": [{"content": [{"text": VERDICT}]}]}},
    ])

    @staticmethod
    def _route(monkeypatch, resp, chat):
        """Serve /responses (SSE) and chat/completions (JSON) independently,
        recording hits and PINNING the exact wire payloads: /responses gets
        exactly {model, instructions, input, stream} with input a list of
        {role, content} messages and stream true (both provider-enforced --
        live 400s otherwise); chat/completions gets exactly {model, messages}.

        `resp`/`chat` are (status, body): body is an SSE event list for a
        /responses 200, a JSON-able dict for a chat 200, an error string
        otherwise. Status 0 simulates a network-level failure."""
        calls: list[str] = []

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            payload = json.loads(req.data.decode())
            if url.endswith("/responses"):
                calls.append("responses")
                assert set(payload) == {"model", "instructions", "input", "stream"}
                assert payload["stream"] is True
                assert isinstance(payload["input"], list) and payload["input"]
                assert all(isinstance(m, dict) and set(m) == {"role", "content"}
                           for m in payload["input"])
                status, body = resp
                if status == 200:
                    return _FakeWire(_sse_events(body))
            else:
                assert url.endswith("/chat/completions")
                calls.append("chat")
                assert set(payload) == {"model", "messages"}
                status, body = chat
                if status == 200:
                    return _FakeWire([json.dumps(body).encode()])
            if status == 0:
                raise OSError(str(body))
            raise urllib.error.HTTPError(url, status, "error", None,
                                         io.BytesIO(str(body).encode()))

        monkeypatch.setattr(iv, "_urlopen", fake_urlopen)
        return calls

    def test_a_504_is_retried_on_the_other_wire(self, monkeypatch, capsys):
        calls = self._route(monkeypatch, (504, "gateway timeout"), self.OK_CHAT)
        out = iv.attempt_once("combo/SOTA-B", "sys", "usr")
        assert out["ok"] is True
        assert out.get("wire") == "chat"
        assert calls == ["responses", "chat"]
        assert "retrying once on chat/completions" in capsys.readouterr().out

    def test_a_network_error_is_retried_on_the_other_wire(self, monkeypatch):
        # status 0 is the network-failure signal on either wire.
        calls = self._route(monkeypatch, (0, "connection reset"), self.OK_CHAT)
        assert iv.attempt_once("combo/SOTA-C", "sys", "usr")["ok"] is True
        assert calls == ["responses", "chat"]

    @pytest.mark.parametrize("status", [404, 405])
    def test_a_gateway_without_responses_falls_over_to_chat(self, monkeypatch, status):
        # The new leg of the failover condition: a gateway that does not route
        # /responses answers 404 (route absent) or 405 (method not wired). Both
        # are deterministic on that wire and say nothing about the chat wire.
        calls = self._route(monkeypatch, (status, "no such route"), self.OK_CHAT)
        out = iv.attempt_once("combo/SOTA-A", "sys", "usr")
        assert out["ok"] is True
        assert out.get("wire") == "chat"
        assert calls == ["responses", "chat"]

    def test_a_rate_limit_is_NOT_retried_on_the_other_wire(self, monkeypatch):
        # 429 is upstream state both wires share; a second attempt burns budget
        # and fails the same way.
        calls = self._route(monkeypatch, (429, "rate limited"), self.OK_CHAT)
        out = iv.attempt_once("combo/SOTA-A", "sys", "usr")
        assert out["ok"] is False
        assert calls == ["responses"], "429 must not touch the second wire"

    def test_an_exhausted_pool_is_NOT_retried_on_the_other_wire(self, monkeypatch):
        calls = self._route(monkeypatch, (401, "no usable account credential"), self.OK_CHAT)
        assert iv.attempt_once("combo/SOTA-A", "sys", "usr")["ok"] is False
        assert calls == ["responses"]

    def test_a_healthy_responses_stream_never_touches_the_other_wire(self, monkeypatch):
        calls = self._route(monkeypatch, self.OK_RESP, self.OK_CHAT)
        out = iv.attempt_once("combo/SOTA-A", "sys", "usr")
        assert out["ok"] is True and out["v"]["refuted"] is False
        assert calls == ["responses"]

    def test_the_folded_deltas_survive_an_empty_completed_output(self, monkeypatch):
        # Observed live on this gateway: response.completed can carry an EMPTY
        # "output" array even though text WAS produced -- it arrived as
        # response.output_text.delta events. The fold must treat the deltas as
        # the primary text source, the completed object as fallback only.
        calls = self._route(monkeypatch, (200, [
            {"type": "response.created"},
            {"type": "response.output_text.delta", "delta": self.VERDICT[:10]},
            {"type": "response.output_text.delta", "delta": self.VERDICT[10:]},
            {"type": "response.completed", "response": {"id": "resp_empty", "output": []}},
        ]), self.OK_CHAT)
        out = iv.attempt_once("combo/SOTA-C", "sys", "usr")
        assert out["ok"] is True and out["v"]["refuted"] is False
        assert calls == ["responses"], "an empty completed output must not cost the voice"

    def test_a_stream_cut_before_completed_is_transient(self, monkeypatch):
        # The stream died mid-think: no response.completed ever arrived. That
        # is classified like status 0/network -- the chat failover gets its one
        # shot, and the retry loop may try again.
        calls = self._route(monkeypatch, (200, [
            {"type": "response.created"},
            {"type": "response.output_text.delta", "delta": "partial"},
        ]), (504, "gateway timeout"))
        out = iv.attempt_once("combo/SOTA-B", "sys", "usr")
        assert out["ok"] is False
        assert out["status"] == 0
        assert "without response.completed" in out["reason"]
        assert iv.is_transient(out["status"]) is True
        assert calls == ["responses", "chat"]

    def test_an_error_typed_event_is_transient(self, monkeypatch):
        calls = self._route(monkeypatch, (200, [
            {"type": "response.created"},
            {"type": "error", "message": "upstream disconnected"},
        ]), (504, "gateway timeout"))
        out = iv.attempt_once("combo/SOTA-B", "sys", "usr")
        assert out["ok"] is False and out["status"] == 0
        assert "stream error event" in out["reason"]
        assert iv.is_transient(out["status"]) is True
        assert calls == ["responses", "chat"]

    def test_a_torn_stream_is_retried_and_recovers(self, monkeypatch):
        # End to end through verify_once: attempt 1 folds a torn stream (-> 0),
        # burns its one chat shot on a 504, and the retry loop -- unchanged
        # from the committed design -- brings attempt 2 home on /responses.
        monkeypatch.setattr(iv.time, "sleep", lambda s: None)
        calls: list[str] = []
        torn = _sse_events([{"type": "response.created"}])
        good = _sse_events(self.OK_RESP[1])

        def fake_urlopen(req, timeout=None):
            if req.full_url.endswith("/responses"):
                calls.append("responses")
                return _FakeWire(torn if calls.count("responses") == 1 else good)
            calls.append("chat")
            raise urllib.error.HTTPError(req.full_url, 504, "gateway timeout", None,
                                         io.BytesIO(b"gateway timeout"))

        monkeypatch.setattr(iv, "_urlopen", fake_urlopen)
        out = iv.verify_once("combo/SOTA-B", "sys", "usr")
        assert out["ok"] is True
        assert calls == ["responses", "chat", "responses"]

    def test_both_wires_failing_reports_the_ORIGINAL_failure(self, monkeypatch):
        # The operator needs the /responses status, not the second one: the
        # primary wire's failure is the symptom being diagnosed.
        self._route(monkeypatch, (504, "gateway timeout"), (500, "also broken"))
        out = iv.attempt_once("combo/SOTA-B", "sys", "usr")
        assert out["ok"] is False
        assert out["status"] == 504
        assert "504" in out["reason"]

    def test_wire_may_differ_predicate(self):
        assert iv.wire_may_differ(0) and iv.wire_may_differ(500) and iv.wire_may_differ(504)
        assert not iv.wire_may_differ(429)
        assert not iv.wire_may_differ(401)
        assert not iv.wire_may_differ(400)
        assert not iv.wire_may_differ(200)

    def test_should_fallback_chat_predicate(self):
        # wire_may_differ's set plus the two no-route statuses -- and nothing
        # else: 400/403 are deterministic verdicts about THIS request, 408/409
        # are same-wire retry material, and 429/401 are shared upstream state.
        for status in (0, 500, 502, 504, 404, 405):
            assert iv.should_fallback_chat(status) is True, status
        for status in (200, 400, 401, 403, 408, 409, 429):
            assert iv.should_fallback_chat(status) is False, status
