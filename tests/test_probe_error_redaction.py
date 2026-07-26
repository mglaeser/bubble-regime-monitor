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
import contextlib
import io
import json
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


# The exact value the required approver used to veto the shape-filter design.
# It is 20 characters and every character is in [A-Za-z0-9._:-], so it
# satisfies ^[A-Za-z0-9._:-]{1,128}$ — the regex that was supposed to make a
# provider request id safe to print.
SHORT_REQUEST_ID_SECRET = "sk-proj-abcdef123456"  # pragma: allowlist secret -- synthetic fixture

_WANTED_FUNCS = ("safe_error", "canonical_json", "local_operation_id",
                 "safe_status")


def _load_symbols() -> dict[str, Any]:
    """Compile ONLY the sanitisers and the constants they close over.

    The surrounding script reads environment variables and performs network
    calls at import, so the module cannot simply be exec'd. Lifting the exact
    definitions out of the parsed AST keeps these tests bound to the shipped
    source without executing anything else."""
    tree = ast.parse(_embedded_python())
    wanted: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED_FUNCS:
            wanted.append(node)
        elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", "").startswith("_") for t in node.targets):
            wanted.append(node)
    names = {n.name for n in wanted if isinstance(n, ast.FunctionDef)}
    assert set(_WANTED_FUNCS) <= names, f"missing {set(_WANTED_FUNCS) - names}"
    module = ast.Module(body=wanted, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {"Any": Any, "re": __import__("re"),
                                 "hashlib": __import__("hashlib"),
                                 "json": json}
    exec(compile(module, "<probe-workflow>", "exec"), namespace)  # noqa: S102 -- shipped source under test
    return namespace


@pytest.fixture(scope="module")
def safe_error():
    return _load_symbols()["safe_error"]


@pytest.fixture(scope="module")
def op_id():
    return _load_symbols()["local_operation_id"]


def _flatten(value: Any) -> str:
    """Every string anywhere in the returned structure, concatenated."""
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values()) + " " + \
               " ".join(str(k) for k in value)
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return "" if value is None else str(value)


# A SHORT, whitespace-free secret fragment. This is the case a shape filter
# (length + no-whitespace) lets straight through, and the reason `safe_error`
# now emits no provider-controlled value at all.
SHORT_SECRET = "sk-proj-abcdef123456"  # pragma: allowlist secret -- synthetic fixture
LEAK_FRAGMENTS = ("Bjufbiyf", "itvb", "sk-proj-", "Bearer", "DEADBEEF",
                  "Incorrect API key", "abcdef123456")  # pragma: allowlist secret -- synthetic leak-detection needles


class TestNoProviderControlledText:
    @pytest.mark.parametrize("message", [PARTIAL_KEY_ECHO, FULL_KEY_ECHO, BEARER_ECHO])
    def test_key_echo_never_survives(self, safe_error, message):
        out = safe_error({"error": {"type": "invalid_request_error",
                                    "code": "invalid_api_key",
                                    "param": None,
                                    "message": message}})
        blob = _flatten(out)
        for fragment in LEAK_FRAGMENTS:
            assert fragment not in blob, f"{fragment!r} leaked into {out!r}"

    def test_only_a_repository_owned_diagnostic_is_returned(self, safe_error):
        out = safe_error({"error": {"type": "t", "code": "c", "param": "p",
                                    "message": "anything at all"}})
        assert set(out) == {"diagnostic"}
        assert out["diagnostic"] == "provider returned a structured error"

    @pytest.mark.parametrize("field", ["type", "code", "param"])
    def test_short_whitespace_free_secret_in_an_enum_field_cannot_escape(
            self, safe_error, field):
        # THE PR25-01 regression. A length+whitespace filter accepts
        # "sk-proj-abcdef123456" unchanged; only refusing to emit provider
        # values at all closes it.
        err = {"type": None, "code": None, "param": None, "message": ""}
        err[field] = SHORT_SECRET
        out = safe_error({"error": err})
        assert SHORT_SECRET not in _flatten(out)
        assert set(out) == {"diagnostic"}

    @pytest.mark.parametrize("body", [None, "a string", 42, [], {"no_error": 1},
                                      {"error": "not-a-dict"}])
    def test_malformed_bodies_yield_static_diagnostics_only(self, safe_error, body):
        out = safe_error(body)
        assert set(out) == {"diagnostic"}
        assert out["diagnostic"].startswith("provider returned")


class TestProviderRequestIdChannelIsRemoved:
    """PR25-04. The required approver vetoed the shape-filter design, correctly.

    `safe_request_id` accepted any value matching ^[A-Za-z0-9._:-]{1,128}$ and
    emitted it verbatim. "sk-proj-abcdef123456" satisfies that pattern, so the
    "validated" identifier was a disclosure channel — the SECOND shape filter
    in this file defeated by a short, whitespace-free secret.

    A regex proves shape. It cannot prove that provider-controlled text is not
    a key, a key fragment, a bearer token, a customer identifier or repository
    content. So the channel is removed rather than narrowed: no header is read,
    and no provider identifier appears in the schema at all."""

    def test_the_veto_example_would_have_passed_the_withdrawn_regex(self):
        # Pins WHY the design was wrong, so a future author cannot reintroduce
        # a "validated request id" believing the example was rejected.
        import re
        withdrawn = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
        assert withdrawn.fullmatch(SHORT_REQUEST_ID_SECRET), \
            "the veto example must satisfy the withdrawn regex"

    def test_provider_request_id_is_never_persisted(self, monkeypatch, tmp_path):
        stdout, summary = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            body={"id": "gpt-5.3-codex", "object": "response.input_tokens",
                  "input_tokens": 7},
            headers={"x-request-id": SHORT_REQUEST_ID_SECRET})
        blob = stdout + summary
        for fragment in ("sk-proj-", "abcdef123456",  # pragma: allowlist secret -- synthetic leak needles
                         SHORT_REQUEST_ID_SECRET):
            assert fragment not in blob, f"{fragment!r} reached the evidence"

    def test_request_id_fields_are_absent_from_the_schema(self, monkeypatch, tmp_path):
        stdout, _ = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            body={"id": "gpt-5.3-codex", "object": "response.input_tokens",
                  "input_tokens": 7},
            headers={"x-request-id": SHORT_REQUEST_ID_SECRET})
        evidence = json.loads(stdout)
        for result in evidence["results"]:
            # absent, not merely null
            assert "request_id" not in result["model_retrieve"]
            assert "request_id" not in result["input_token_count"]
        for banned in ("provider_request_id", "response_id", "request_id"):
            assert f'"{banned}"' not in stdout

    def test_correlation_uses_a_repository_owned_operation_id(
            self, monkeypatch, tmp_path):
        stdout, _ = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            body={"id": "gpt-5.3-codex", "object": "response.input_tokens",
                  "input_tokens": 7},
            headers={"x-request-id": SHORT_REQUEST_ID_SECRET})
        evidence = json.loads(stdout)
        first = evidence["results"][0]
        for section in ("model_retrieve", "input_token_count"):
            op_id = first[section]["local_operation_id"]
            assert isinstance(op_id, str) and len(op_id) == 64
            assert int(op_id, 16) >= 0          # hex digest, repo-derived
        # the two operations are distinct
        assert (first["model_retrieve"]["local_operation_id"]
                != first["input_token_count"]["local_operation_id"])


class TestSourceLevelGuardAgainstReintroduction:
    """Source guards over KNOWN header-access forms, plus behavioural tests.

    Scope stated honestly: these AST checks forbid the enumerated access
    shapes below. They are NOT a semantic proof that no header can ever be
    read — a helper that receives the response object, or an API shape not
    listed here, would evade them. The load-bearing guarantee is the
    end-to-end hostile-header test (TestWholeRenderedEvidence and
    TestProviderRequestIdChannelIsRemoved), which plants a secret in the
    header and proves it never reaches the rendered evidence."""

    # Attribute names and call names that constitute header access on
    # http.client / urllib response objects.
    _HEADER_ATTRS = {"headers", "hdrs"}
    _HEADER_CALLS = {"getheader", "getheaders", "info"}

    def test_no_known_header_access_form_in_the_script(self):
        # AST, not text: the docstrings deliberately DISCUSS the removed
        # header, and prose explaining a fix must not trip the guard on the
        # fix. What matters is that no code path uses a known access form.
        tree = ast.parse(_embedded_python())
        attr_reads = [n for n in ast.walk(tree)
                      if isinstance(n, ast.Attribute)
                      and n.attr in self._HEADER_ATTRS
                      and isinstance(n.ctx, ast.Load)]
        call_reads = [n for n in ast.walk(tree)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute)
                      and n.func.attr in self._HEADER_CALLS]
        assert not attr_reads and not call_reads, (
            "the script uses a known response-header access form; the "
            "provider request id channel is reopened")

    def test_no_safe_request_id_function_remains(self):
        tree = ast.parse(_embedded_python())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert "safe_request_id" not in names
        assert "local_operation_id" in names

    def test_no_request_id_evidence_keys_are_constructed(self):
        tree = ast.parse(_embedded_python())
        keys = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for banned in ("request_id", "provider_request_id", "response_id",
                       "count_request_id", "model_request_id"):
            assert banned not in keys, f"{banned!r} is still a literal in the script"

    def test_request_helper_returns_only_status_and_body(self):
        tree = ast.parse(_embedded_python())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "request")
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        assert returns, "request() must return"
        for node in returns:
            assert isinstance(node.value, ast.Tuple), "expected a tuple return"
            assert len(node.value.elts) == 2, \
                "request() must return exactly (status, body) — a third element " \
                "is how the provider request id got into the evidence"


class TestWholeRenderedEvidence:
    """PR25-03: sanitise the EVIDENCE OBJECT, not just one helper.

    Executes the workflow's real embedded script end-to-end with a hostile
    provider stub that plants secret material in every provider-controlled
    field — error.type/code/param/message, the returned model id, the object
    string and the x-request-id header — then flattens both the printed JSON
    and the job summary and proves nothing survived. Testing `safe_error`
    alone would have missed the model id, the object field and the request id,
    all of which were previously written out verbatim."""

    @staticmethod
    def _run(monkeypatch, tmp_path, *, body: dict | None = None,
             headers: dict | None = None, status: int = 200,
             raise_http: bool = False, raise_exc: BaseException | None = None,
             raw: bytes | None = None,
             read_raises: BaseException | None = None):
        import urllib.error
        import urllib.request

        class _Resp:
            def __init__(self):
                self.status = status
                self.headers = headers or {}

            def read(self):
                if read_raises is not None:
                    raise read_raises
                if raw is not None:
                    return raw
                return json.dumps(body).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            if raise_exc is not None:
                raise raise_exc
            if raise_http:
                raise urllib.error.HTTPError(
                    "https://example.invalid", status, "err",
                    headers, io.BytesIO(json.dumps(body).encode("utf-8")))
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.invalid/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-THIS-IS-THE-REAL-KEY")  # pragma: allowlist secret -- synthetic fixture
        monkeypatch.setenv("WORKFLOW_SHA", "deadbeef")
        monkeypatch.setenv("WORKFLOW_RUN_ID", "1")
        monkeypatch.setenv("WORKFLOW_JOB", "probe")
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            exec(compile(_embedded_python(), "<probe>", "exec"),  # noqa: S102 -- shipped source under test
                 {"__name__": "__probe__"})
        return buf.getvalue(), (summary.read_text() if summary.exists() else "")

    def test_secrets_planted_in_every_provider_field_never_surface(
            self, monkeypatch, tmp_path):
        hostile_body = {
            "id": SHORT_SECRET,
            "object": "Bjufbiyf-object-itvb",
            "input_tokens": "sk-proj-not-even-an-int",
            "error": {
                "type": SHORT_SECRET,
                "code": "Bjufbiyf",
                "param": "itvb",
                "message": PARTIAL_KEY_ECHO,
            },
        }
        # The veto example, not a sentence: a full-sentence header contains
        # spaces and therefore never exercised the shape-filter bypass.
        hostile_headers = {"x-request-id": SHORT_REQUEST_ID_SECRET}
        stdout, summary = self._run(monkeypatch, tmp_path,
                                    body=hostile_body, headers=hostile_headers)
        blob = stdout + summary
        assert blob.strip(), "the probe produced no evidence at all"
        for fragment in (*LEAK_FRAGMENTS, "THIS-IS-THE-REAL-KEY",
                         "not-even-an-int"):
            assert fragment not in blob, f"{fragment!r} leaked into the evidence"

    def test_hostile_run_still_fails_closed_and_reports_no_success(
            self, monkeypatch, tmp_path):
        stdout, _ = self._run(
            monkeypatch, tmp_path,
            body={"error": {"type": "x", "code": "y", "param": "z",
                            "message": PARTIAL_KEY_ECHO}},
            headers={"x-request-id": "req_ok-1"})
        evidence = json.loads(stdout)
        assert evidence["overall_ok"] is False
        assert evidence["generation_calls"] == 0
        for result in evidence["results"]:
            assert result["model_retrieve"]["ok"] is False
            assert result["model_retrieve"]["returned_id"] is None
            assert result["input_token_count"]["input_tokens"] is None

    def test_valid_response_records_the_expected_values_safely(
            self, monkeypatch, tmp_path):
        # A well-behaved provider: the evidence keeps the REQUESTED id (proved
        # equal), a boolean for the object field, and the validated request id.
        stdout, _ = self._run(
            monkeypatch, tmp_path,
            body={"id": "gpt-5.3-codex", "object": "response.input_tokens",
                  "input_tokens": 34},
            headers={"x-request-id": "req_valid-123"})
        evidence = json.loads(stdout)
        first = evidence["results"][0]
        assert first["requested_model_id"] == "gpt-5.3-codex"
        assert first["model_retrieve"]["returned_id"] == "gpt-5.3-codex"
        assert first["model_retrieve"]["returned_id_matches"] is True
        assert first["input_token_count"]["object_matches"] is True
        assert first["input_token_count"]["input_tokens"] == 34
        # correlation is repository-owned; no provider identifier is kept
        assert len(first["input_token_count"]["local_operation_id"]) == 64
        assert "request_id" not in first["input_token_count"]
        # the raw provider `object` string is never emitted
        assert "object" not in first["input_token_count"]

    def test_mismatched_model_id_is_dropped_not_echoed(self, monkeypatch, tmp_path):
        stdout, _ = self._run(
            monkeypatch, tmp_path,
            body={"id": "attacker/../evil-" + SHORT_SECRET,
                  "object": "response.input_tokens", "input_tokens": 1},
            headers={"x-request-id": "req_x"})
        evidence = json.loads(stdout)
        first = evidence["results"][0]
        assert first["model_retrieve"]["returned_id"] is None
        assert first["model_retrieve"]["returned_id_matches"] is False
        assert SHORT_SECRET not in stdout


class TestTransportFailuresCannotLeak:
    """Section 3.1: a transport or decode failure must never print exception
    text, because a URLError reason, an OSError strerror or a
    UnicodeDecodeError byte excerpt is not repository-authored prose. Before
    this fix, request() caught HTTPError ONLY, so any of these escaped as an
    unhandled traceback — contradicting the claim that repository-owned
    diagnostics are the only prose an error path can emit."""

    def _assert_contained(self, stdout, summary):
        blob = stdout + summary
        assert blob.strip(), "the probe produced no evidence at all"
        for fragment in (*LEAK_FRAGMENTS, "Traceback", "urllib.error",
                         "URLError", "OSError", "TimeoutError",
                         "UnicodeDecodeError"):
            assert fragment not in blob, f"{fragment!r} leaked into the evidence"
        evidence = json.loads(stdout)
        assert evidence["overall_ok"] is False
        assert evidence["generation_calls"] == 0
        return evidence

    def test_urlerror_with_secret_reason_is_contained(self, monkeypatch, tmp_path):
        import urllib.error
        stdout, summary = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            raise_exc=urllib.error.URLError(SHORT_SECRET))
        self._assert_contained(stdout, summary)

    def test_timeout_with_secret_text_is_contained(self, monkeypatch, tmp_path):
        stdout, summary = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path, raise_exc=TimeoutError(SHORT_SECRET))
        self._assert_contained(stdout, summary)

    def test_oserror_with_bearer_text_is_contained(self, monkeypatch, tmp_path):
        stdout, summary = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            raise_exc=OSError("Bearer sk-proj-DEADBEEFdeadbeef"))  # pragma: allowlist secret -- synthetic fixture
        self._assert_contained(stdout, summary)

    def test_read_raising_a_secret_bearing_error_is_contained(
            self, monkeypatch, tmp_path):
        stdout, summary = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path, read_raises=OSError(PARTIAL_KEY_ECHO))
        self._assert_contained(stdout, summary)

    def test_invalid_utf8_body_is_contained(self, monkeypatch, tmp_path):
        stdout, summary = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path, raw=b"\xff\xfe not json at all \x80")
        evidence = self._assert_contained(stdout, summary)
        # the invalid-JSON category maps to a repository-owned diagnostic
        first_err = evidence["results"][0]["model_retrieve"]["error"]
        assert first_err == {
            "diagnostic": "provider response bytes were not valid JSON"}

    def test_local_failure_diagnostics_are_repository_owned(
            self, monkeypatch, tmp_path):
        import urllib.error
        stdout, _ = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            raise_exc=urllib.error.URLError("anything"))
        evidence = json.loads(stdout)
        err = evidence["results"][0]["model_retrieve"]["error"]
        assert err == {"diagnostic": ("local transport failure before a "
                                      "provider response was decoded")}
        # a local failure yields no HTTP status to validate
        assert evidence["results"][0]["model_retrieve"]["status"] is None


class TestLocalOperationIdBindsTheRun:
    """Section 3.3 / the required approver's veto on 571b5ed: the docstring
    promised WORKFLOW_SHA, run id and result position, but the hash contained
    none of them, so identical operations across runs produced identical ids
    and per-run correlation was silently impossible. Option A: the hash now
    binds exactly schema version, workflow SHA, run id, result index, model,
    operation and payload hash."""

    BASE_ARGS = ("gpt-5.6-sol", "responses.input_tokens", "ab" * 32, 1,
                 "sha-A", "run-A")

    def test_same_full_inputs_give_the_same_id(self, op_id):
        assert op_id(*self.BASE_ARGS) == op_id(*self.BASE_ARGS)

    @pytest.mark.parametrize("index,replacement", [
        (0, "gpt-4.1-mini"),        # model
        (1, "models.retrieve"),     # operation
        (2, "cd" * 32),             # payload hash
        (3, 2),                     # result index
        (4, "sha-B"),               # workflow SHA
        (5, "run-B"),               # workflow run id
    ])
    def test_changing_any_bound_field_changes_the_id(self, op_id, index,
                                                     replacement):
        changed = list(self.BASE_ARGS)
        changed[index] = replacement
        assert op_id(*changed) != op_id(*self.BASE_ARGS), (
            f"field {index} does not enter the hash — the id is not "
            "run-specific and the docstring is lying again")

    def test_live_evidence_ids_are_deterministic_and_position_distinct(
            self, monkeypatch, tmp_path):
        # Cross-RUN distinctness is proven at the function level above (fields
        # 4 and 5 of the parametrized test); here the real end-to-end script
        # proves position distinctness within a run and determinism across
        # identical runs.
        body = {"id": "gpt-5.3-codex", "object": "response.input_tokens",
                "input_tokens": 7}
        out1, _ = TestWholeRenderedEvidence._run(monkeypatch, tmp_path,
                                                 body=body, headers={})
        out2, _ = TestWholeRenderedEvidence._run(monkeypatch, tmp_path,
                                                 body=body, headers={})
        ev1, ev2 = json.loads(out1), json.loads(out2)
        ids1 = [r["model_retrieve"]["local_operation_id"]
                for r in ev1["results"]]
        assert len(set(ids1)) == len(ids1)          # positions are distinct
        assert ids1 == [r["model_retrieve"]["local_operation_id"]
                        for r in ev2["results"]]    # identical env -> identical ids


class TestEvidenceSchemaIsClosed:
    """Section 4.C/4.D: the schema is enumerated, and every public string is
    repository-owned or an exact-match substitution. An unexpected key is a
    failure — new fields must be added HERE at the same time as in the
    workflow, so no field can slip into evidence unreviewed."""

    EVIDENCE_KEYS = {"schema_version", "probe_type", "generation_calls",
                     "base_url", "workflow_sha", "workflow_run_id",
                     "workflow_job", "probed_at", "results", "overall_ok"}
    RESULT_KEYS = {"requested_model_id", "model_retrieve", "input_token_count"}
    MODEL_KEYS = {"status", "returned_id", "returned_id_matches",
                  "local_operation_id", "ok", "error"}
    COUNT_KEYS = {"status", "object_matches", "input_tokens",
                  "local_operation_id", "payload_sha256", "ok", "error"}
    MODELS = ("gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini")
    DIAGNOSTICS = {
        "provider returned a non-object error response",
        "provider returned no structured error object",
        "provider returned a structured error",
        "local transport failure before a provider response was decoded",
        "local timeout before a provider response was decoded",
        "provider response bytes were not valid JSON",
        "unrecognized local failure category",
    }

    def _evidence(self, monkeypatch, tmp_path, **kw):
        stdout, _ = TestWholeRenderedEvidence._run(monkeypatch, tmp_path, **kw)
        return json.loads(stdout)

    @pytest.mark.parametrize("kw", [
        {"body": {"id": "gpt-5.3-codex", "object": "response.input_tokens",
                  "input_tokens": 7}, "headers": {}},
        {"body": {"error": {"type": "x", "message": PARTIAL_KEY_ECHO}},
         "headers": {}},
        {"raw": b"\xff\xfe"},
    ])
    def test_exact_key_sets_in_every_scenario(self, monkeypatch, tmp_path, kw):
        evidence = self._evidence(monkeypatch, tmp_path, **kw)
        assert set(evidence) == self.EVIDENCE_KEYS
        for result in evidence["results"]:
            assert set(result) == self.RESULT_KEYS
            assert set(result["model_retrieve"]) == self.MODEL_KEYS
            assert set(result["input_token_count"]) == self.COUNT_KEYS

    def test_every_string_value_is_repository_owned_or_exact_match(
            self, monkeypatch, tmp_path):
        evidence = self._evidence(
            monkeypatch, tmp_path,
            body={"id": "gpt-5.3-codex", "object": "response.input_tokens",
                  "input_tokens": 7}, headers={})

        def strings(value):
            if isinstance(value, dict):
                for v in value.values():
                    yield from strings(v)
            elif isinstance(value, list):
                for v in value:
                    yield from strings(v)
            elif isinstance(value, str):
                yield value

        import re as _re
        from datetime import datetime
        hex64 = _re.compile(r"^[0-9a-f]{64}$")
        allowed_fixed = {
            "OPENAI_RESPONSES_INPUT_TOKEN_CAPABILITY",
            "https://api.openai.invalid/v1",     # repo-set env in this test
            "deadbeef", "1", "probe",            # repo-set env in this test
            *self.MODELS, *self.DIAGNOSTICS,
        }
        for text in strings(evidence):
            if text in allowed_fixed or hex64.fullmatch(text):
                continue
            # the only remaining string is the timestamp; it must parse
            datetime.fromisoformat(text)

    def test_status_appears_only_in_validated_form(self, monkeypatch, tmp_path):
        # A status outside the HTTP range must become null, never raw.
        evidence = self._evidence(
            monkeypatch, tmp_path,
            body={"error": {"type": "x"}}, headers={}, status=999)
        for result in evidence["results"]:
            assert result["model_retrieve"]["status"] is None
            assert result["input_token_count"]["status"] is None


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
