"""The trusted capability probe must never emit provider free-form text.

Run 30214247762 proved this matters: with an invalid key, OpenAI's
`error.message` embeds a partially-redacted echo of the submitted key
("Incorrect API key provided: XXXXXXXX******XXXX"). The first version of the
workflow passed that message through truncated to 300 characters, so a key
fingerprint reached the workflow log while the same file claimed it never
prints the key.

These tests drive the REAL function out of the committed workflow — extracted
from the YAML and compiled from its own source — rather than a copy. A copy
would let the workflow and the test drift apart, which is precisely how a
security guarantee becomes decorative.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = (Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "openai-verifier-capability-probe.yml")

# Realistic provider echoes. The first is the exact shape observed on run
# 30214247762; the second is a hypothetical full-key echo, which must be
# refused just as firmly.
PARTIAL_KEY_ECHO = ("Incorrect API key provided: Bjufbiyf******itvb. You can "
                    "find your API key at https://platform.openai.com/account/api-keys.")
FULL_KEY_ECHO = ("Incorrect API key provided: "
                 "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH")  # pragma: allowlist secret -- synthetic fixture
BEARER_ECHO = "Invalid Authorization header: Bearer sk-proj-DEADBEEFdeadbeef"  # pragma: allowlist secret -- synthetic fixture


def _embedded_python() -> str:
    """The heredoc body of the workflow's only run step."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    run = doc["jobs"]["probe"]["steps"][0]["run"]
    lines = run.split("\n")
    start = next(i for i, line in enumerate(lines)
                 if line.strip().startswith("python3 - <<'PY'"))
    end = next(i for i, line in enumerate(lines) if line == "PY")
    return "\n".join(lines[start + 1:end])


def _load_safe_error():
    """Compile ONLY `safe_error` (and the constants it closes over).

    The surrounding script reads environment variables and performs network
    calls at import, so the module cannot simply be exec'd. Lifting the exact
    function definition out of the parsed AST keeps the test bound to the
    shipped source without executing anything else."""
    tree = ast.parse(_embedded_python())
    wanted = [node for node in tree.body
              if (isinstance(node, ast.FunctionDef) and node.name == "safe_error")
              or (isinstance(node, ast.Assign)
                  and any(getattr(t, "id", "") == "_DIAGNOSTIC" for t in node.targets))]
    assert any(isinstance(n, ast.FunctionDef) for n in wanted), \
        "safe_error not found in the workflow's embedded script"
    module = ast.Module(body=wanted, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {"Any": Any}
    exec(compile(module, "<probe-workflow>", "exec"), namespace)  # noqa: S102 -- shipped source under test
    return namespace["safe_error"]


@pytest.fixture(scope="module")
def safe_error():
    return _load_safe_error()


def _flatten(value: Any) -> str:
    """Every string anywhere in the returned structure, concatenated."""
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values()) + " " + \
               " ".join(str(k) for k in value)
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return "" if value is None else str(value)


class TestNoFreeFormProviderText:
    @pytest.mark.parametrize("message", [PARTIAL_KEY_ECHO, FULL_KEY_ECHO, BEARER_ECHO])
    def test_key_echo_never_survives(self, safe_error, message):
        out = safe_error({"error": {"type": "invalid_request_error",
                                    "code": "invalid_api_key",
                                    "param": None,
                                    "message": message}})
        blob = _flatten(out)
        for fragment in ("Bjufbiyf", "itvb", "sk-proj-", "Bearer", "DEADBEEF",
                         "Incorrect API key"):
            assert fragment not in blob, f"{fragment!r} leaked into {out!r}"

    def test_message_field_is_not_returned_at_all(self, safe_error):
        out = safe_error({"error": {"type": "t", "code": "c", "param": "p",
                                    "message": "anything at all"}})
        assert "message" not in out
        assert "anything" not in _flatten(out)

    def test_allowlisted_fields_are_retained(self, safe_error):
        out = safe_error({"error": {"type": "invalid_request_error",
                                    "code": "invalid_api_key",
                                    "param": "model",
                                    "message": PARTIAL_KEY_ECHO}})
        assert out["type"] == "invalid_request_error"
        assert out["code"] == "invalid_api_key"
        assert out["param"] == "model"
        assert set(out) == {"type", "code", "param", "diagnostic"}

    def test_free_text_smuggled_into_an_enum_field_is_dropped(self, safe_error):
        # If a provider ever put a sentence — or a key — in `code`, it must not
        # become a disclosure channel. Dropped whole, never truncated: a
        # truncated secret is still a secret.
        out = safe_error({"error": {"type": "t", "code": PARTIAL_KEY_ECHO,
                                    "param": None, "message": ""}})
        assert out["code"] == "dropped_unexpected_shape"
        assert "Bjufbiyf" not in _flatten(out)

    def test_overlong_enum_field_without_spaces_is_dropped(self, safe_error):
        out = safe_error({"error": {"type": "x" * 65, "code": None,
                                    "param": None, "message": ""}})
        assert out["type"] == "dropped_unexpected_shape"

    @pytest.mark.parametrize("body", [None, "a string", 42, [], {"no_error": 1},
                                      {"error": "not-a-dict"}])
    def test_malformed_bodies_yield_static_diagnostics_only(self, safe_error, body):
        out = safe_error(body)
        assert set(out) == {"type", "code", "param", "diagnostic"}
        assert out["type"] is None and out["code"] is None
        # the diagnostic is repository-authored prose, not provider text
        assert out["diagnostic"].startswith("provider returned")


class TestWorkflowStillSatisfiesItsOtherGuarantees:
    def test_no_checkout_and_no_generation_endpoint(self):
        raw = WORKFLOW.read_text()
        doc = yaml.safe_load(raw)
        job = doc["jobs"]["probe"]
        assert not any("uses" in step for step in job["steps"]), \
            "a checkout would run branch code with the key in scope"
        code = _embedded_python()
        assert '"/responses/input_tokens"' in code
        assert '"/responses"' not in code, "probe must never call generation"

    def test_environment_gate_and_no_repo_level_secret_fallback(self):
        raw = WORKFLOW.read_text()
        doc = yaml.safe_load(raw)
        assert doc["jobs"]["probe"].get("environment") == "verifier-probe"
        # A repository-level secret is readable from ANY ref and would defeat
        # the environment's deployment-branch policy.
        import re
        live = re.findall(r"\$\{\{\s*secrets\.(\w+)\s*\}\}", raw)
        assert live == ["OPENAI_VERIFIER_PROBE_API_KEY"], live
