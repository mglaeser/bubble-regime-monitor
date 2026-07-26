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
from typing import Any, NamedTuple

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

_UNSET = object()


class ProbeRun(NamedTuple):
    stdout: str
    stderr: str
    summary: str
    exit_code: int | None

    @property
    def all_output(self) -> str:
        return self.stdout + self.stderr + self.summary


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
        elif isinstance(node, ast.ClassDef) and node.name == "LocalFailure":
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
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            body={"id": "gpt-5.3-codex", "object": "response.input_tokens",
                  "input_tokens": 7},
            headers={"x-request-id": SHORT_REQUEST_ID_SECRET})
        blob = res.all_output
        for fragment in ("sk-proj-", "abcdef123456",  # pragma: allowlist secret -- synthetic leak needles
                         SHORT_REQUEST_ID_SECRET):
            assert fragment not in blob, f"{fragment!r} reached the evidence"

    def test_request_id_fields_are_absent_from_the_schema(self, monkeypatch, tmp_path):
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            body={"id": "gpt-5.3-codex", "object": "response.input_tokens",
                  "input_tokens": 7},
            headers={"x-request-id": SHORT_REQUEST_ID_SECRET})
        evidence = json.loads(res.stdout)
        for result in evidence["results"]:
            # absent, not merely null
            assert "request_id" not in result["model_retrieve"]
            assert "request_id" not in result["input_token_count"]
        for banned in ("provider_request_id", "response_id", "request_id"):
            assert f'"{banned}"' not in res.stdout

    def test_correlation_uses_a_repository_owned_operation_id(
            self, monkeypatch, tmp_path):
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            body={"id": "gpt-5.3-codex", "object": "response.input_tokens",
                  "input_tokens": 7},
            headers={"x-request-id": SHORT_REQUEST_ID_SECRET})
        evidence = json.loads(res.stdout)
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
             read_raises: BaseException | None = None,
             enter_raises: BaseException | None = None,
             exit_raises: BaseException | None = None,
             status_exc: BaseException | None = None,
             read_returns: Any = _UNSET,
             responder=None) -> ProbeRun:
        """Execute the shipped embedded script against a controllable stub.

        Captures stdout, STDERR and the job summary (F-03: an escaping
        traceback prints to stderr, so a helper that redirects only stdout
        cannot see the very leak the transport tests exist to catch), and
        retains the SystemExit code instead of suppressing it (F-04: a failed
        scenario must be proven to exit 1, not merely to print sad JSON).

        `responder(url, request_body_bytes) -> (status, body_dict)` builds a
        realistic per-endpoint stub; the scalar knobs cover hostile cases."""
        import urllib.error
        import urllib.request

        class _Resp:
            def __init__(self, st=None, bd=None):
                self._st = status if st is None else st
                self._bd = body if bd is None else bd
                self.headers = headers or {}

            @property
            def status(self):
                if status_exc is not None:
                    raise status_exc
                return self._st

            def read(self):
                if read_raises is not None:
                    raise read_raises
                if read_returns is not _UNSET:
                    return read_returns
                if raw is not None:
                    return raw
                return json.dumps(self._bd).encode("utf-8")

            def __enter__(self):
                if enter_raises is not None:
                    raise enter_raises
                return self

            def __exit__(self, *a):
                if exit_raises is not None:
                    raise exit_raises
                return False

        def fake_urlopen(req, timeout=None):
            if responder is not None:
                st, bd = responder(req.full_url, req.data)
                return _Resp(st, bd)
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
        monkeypatch.setenv("WORKFLOW_RUN_ATTEMPT", "1")
        monkeypatch.setenv("WORKFLOW_JOB", "probe")
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

        out_buf, err_buf = io.StringIO(), io.StringIO()
        exit_code = None
        with contextlib.redirect_stdout(out_buf), \
                contextlib.redirect_stderr(err_buf):
            try:
                exec(compile(_embedded_python(), "<probe>", "exec"),  # noqa: S102 -- shipped source under test
                     {"__name__": "__probe__"})
            except SystemExit as exc:
                exit_code = exc.code
        return ProbeRun(
            stdout=out_buf.getvalue(),
            stderr=err_buf.getvalue(),
            summary=summary.read_text() if summary.exists() else "",
            exit_code=exit_code,
        )

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
        res = self._run(monkeypatch, tmp_path,
                                    body=hostile_body, headers=hostile_headers)
        blob = res.all_output
        assert blob.strip(), "the probe produced no evidence at all"
        for fragment in (*LEAK_FRAGMENTS, "THIS-IS-THE-REAL-KEY",
                         "not-even-an-int"):
            assert fragment not in blob, f"{fragment!r} leaked into the evidence"

    def test_hostile_run_still_fails_closed_and_reports_no_success(
            self, monkeypatch, tmp_path):
        res = self._run(
            monkeypatch, tmp_path,
            body={"error": {"type": "x", "code": "y", "param": "z",
                            "message": PARTIAL_KEY_ECHO}},
            headers={"x-request-id": "req_ok-1"})
        evidence = json.loads(res.stdout)
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
        res = self._run(
            monkeypatch, tmp_path,
            body={"id": "gpt-5.3-codex", "object": "response.input_tokens",
                  "input_tokens": 34},
            headers={"x-request-id": "req_valid-123"})
        evidence = json.loads(res.stdout)
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
        res = self._run(
            monkeypatch, tmp_path,
            body={"id": "attacker/../evil-" + SHORT_SECRET,
                  "object": "response.input_tokens", "input_tokens": 1},
            headers={"x-request-id": "req_x"})
        evidence = json.loads(res.stdout)
        first = evidence["results"][0]
        assert first["model_retrieve"]["returned_id"] is None
        assert first["model_retrieve"]["returned_id_matches"] is False
        assert SHORT_SECRET not in res.all_output


class TestTransportFailuresCannotLeak:
    """3.1 + F-02: a transport or decode failure must never print exception
    text — a URLError reason, an OSError strerror, an IncompleteRead partial
    or a UnicodeDecodeError byte excerpt is not repository-authored prose.
    request() originally caught HTTPError ONLY, so all of these escaped as an
    unhandled traceback; the first fix enumerated some classes but left
    http.client.HTTPException (e.g. IncompleteRead), EOFError and context-
    manager failures uncovered. The final containment boundary is
    `except Exception` around the whole network/read phase — never
    BaseException — returning a typed LocalFailure.

    Every scenario asserts over stdout AND stderr AND the job summary (F-03),
    and pins exit code 1 (F-04)."""

    def _assert_contained(self, res: ProbeRun):
        assert res.all_output.strip(), "the probe produced no evidence at all"
        for fragment in (*LEAK_FRAGMENTS, "Traceback", "urllib.error",
                         "URLError", "OSError", "TimeoutError",
                         "UnicodeDecodeError", "IncompleteRead", "EOFError"):
            assert fragment not in res.all_output, \
                f"{fragment!r} leaked into the evidence"
        evidence = json.loads(res.stdout)
        assert evidence["overall_ok"] is False
        assert evidence["generation_calls"] == 0
        assert res.exit_code == 1, "a failed probe must exit 1, not merely frown"
        return evidence

    def test_urlerror_with_secret_reason_is_contained(self, monkeypatch, tmp_path):
        import urllib.error
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            raise_exc=urllib.error.URLError(SHORT_SECRET))
        self._assert_contained(res)

    def test_timeout_with_secret_text_is_contained(self, monkeypatch, tmp_path):
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path, raise_exc=TimeoutError(SHORT_SECRET))
        self._assert_contained(res)

    def test_oserror_with_bearer_text_is_contained(self, monkeypatch, tmp_path):
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            raise_exc=OSError("Bearer sk-proj-DEADBEEFdeadbeef"))  # pragma: allowlist secret -- synthetic fixture
        self._assert_contained(res)

    def test_incomplete_read_is_contained(self, monkeypatch, tmp_path):
        # http.client.IncompleteRead is an HTTPException, NOT an OSError —
        # the exact class the first containment attempt missed. Its repr
        # includes the partial bytes, which here carry a secret.
        import http.client
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            read_raises=http.client.IncompleteRead(SHORT_SECRET.encode()))
        self._assert_contained(res)

    def test_eoferror_is_contained(self, monkeypatch, tmp_path):
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path, raise_exc=EOFError(SHORT_SECRET))
        self._assert_contained(res)

    def test_enter_raising_is_contained(self, monkeypatch, tmp_path):
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path, enter_raises=RuntimeError(SHORT_SECRET))
        self._assert_contained(res)

    def test_exit_raising_is_contained(self, monkeypatch, tmp_path):
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            body={"id": "gpt-5.3-codex"},
            exit_raises=RuntimeError(SHORT_SECRET))
        self._assert_contained(res)

    def test_status_property_raising_is_contained(self, monkeypatch, tmp_path):
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path, status_exc=RuntimeError(SHORT_SECRET))
        self._assert_contained(res)

    def test_read_raising_a_secret_bearing_error_is_contained(
            self, monkeypatch, tmp_path):
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path, read_raises=OSError(PARTIAL_KEY_ECHO))
        self._assert_contained(res)

    def test_unexpected_read_return_type_is_contained(self, monkeypatch, tmp_path):
        # read() returning something json.loads cannot take must become the
        # invalid_json category, not a TypeError traceback.
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path, read_returns=object())
        self._assert_contained(res)

    def test_invalid_utf8_body_is_contained(self, monkeypatch, tmp_path):
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path, raw=b"\xff\xfe not json at all \x80")
        evidence = self._assert_contained(res)
        # the invalid-JSON category maps to a repository-owned diagnostic
        first_err = evidence["results"][0]["model_retrieve"]["error"]
        assert first_err == {
            "diagnostic": "provider response bytes were not valid JSON"}

    def test_local_failure_diagnostics_are_repository_owned(
            self, monkeypatch, tmp_path):
        import urllib.error
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            raise_exc=urllib.error.URLError("anything"))
        evidence = json.loads(res.stdout)
        err = evidence["results"][0]["model_retrieve"]["error"]
        assert err == {"diagnostic": ("local transport failure before a "
                                      "provider response was decoded")}
        # a local failure yields no HTTP status to validate
        assert evidence["results"][0]["model_retrieve"]["status"] is None


class TestLocalFailureSentinelCannotBeForged:
    """F-01 / the required approver's veto on ac5bff2, verbatim finding:
    'provider can forge _local_failure sentinel — HTTP response body
    {"_local_failure":"timeout"} is misreported as local timeout despite
    decoded provider response, corrupting evidence provenance.'

    Confirmed defect: the sentinel was an ordinary dict key, and json.loads
    can produce any dict. The class fix is a TYPED LocalFailure object that
    JSON cannot instantiate — json.loads yields only dict/list/str/int/float/
    bool/None, so isinstance(body, LocalFailure) is unforgeable from a
    provider body by construction."""

    def test_provider_body_cannot_forge_a_local_timeout(self, monkeypatch, tmp_path):
        # THE veto reproduction: a well-formed HTTP 200 whose body is exactly
        # the forged sentinel. Provenance must say "provider response", never
        # "local failure".
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path,
            body={"_local_failure": "timeout"}, headers={})
        evidence = json.loads(res.stdout)
        err = evidence["results"][0]["model_retrieve"]["error"]
        assert err == {"diagnostic": "provider returned no structured error object"}, (
            "a decoded provider response was misreported as a local failure — "
            "evidence provenance is forged")
        assert "local timeout" not in res.all_output
        assert "local transport failure" not in res.all_output

    @pytest.mark.parametrize("forged", [
        {"_local_failure": "timeout"},
        {"_local_failure": "transport_error"},
        {"_local_failure": "invalid_json"},
        {"error": {"_local_failure": "timeout"}},
    ])
    def test_no_forged_shape_selects_a_local_diagnostic(self, safe_error, forged):
        out = safe_error(forged)
        assert "local" not in out["diagnostic"], (
            f"provider JSON {forged!r} selected a local-failure diagnostic")

    def test_the_sentinel_type_is_not_json_instantiable(self):
        # The property the fix rests on, pinned: everything json.loads can
        # produce fails the isinstance check the diagnostics now require.
        symbols = _load_symbols()
        local_failure_cls = symbols.get("LocalFailure")
        assert local_failure_cls is not None, "LocalFailure class missing"
        for value in (json.loads('{"_local_failure": "timeout"}'),
                      json.loads('"timeout"'), json.loads("null")):
            assert not isinstance(value, local_failure_cls)


class TestLocalOperationIdBindsTheRun:
    """Section 3.3 / the required approver's veto on 571b5ed: the docstring
    promised WORKFLOW_SHA, run id and result position, but the hash contained
    none of them, so identical operations across runs produced identical ids
    and per-run correlation was silently impossible. Option A: the hash now
    binds exactly schema version, workflow SHA, run id, result index, model,
    operation and payload hash."""

    BASE_ARGS = ("gpt-5.6-sol", "responses.input_tokens", "ab" * 32, 1,
                 "sha-A", "run-A", "attempt-1")

    def test_same_full_inputs_give_the_same_id(self, op_id):
        assert op_id(*self.BASE_ARGS) == op_id(*self.BASE_ARGS)

    @pytest.mark.parametrize("index,replacement", [
        (0, "gpt-4.1-mini"),        # model
        (1, "models.retrieve"),     # operation
        (2, "cd" * 32),             # payload hash
        (3, 2),                     # result index
        (4, "sha-B"),               # workflow SHA
        (5, "run-B"),               # workflow run id
        (6, "attempt-2"),           # run attempt (F-05 Option A: github.run_id
                                    # is STABLE across reruns; run_attempt is
                                    # what distinguishes attempt from attempt)
    ])
    def test_changing_any_bound_field_changes_the_id(self, op_id, index,
                                                     replacement):
        changed = list(self.BASE_ARGS)
        changed[index] = replacement
        assert op_id(*changed) != op_id(*self.BASE_ARGS), (
            f"field {index} does not enter the hash — the id is not "
            "attempt-specific and the docstring is lying again")

    def test_live_evidence_ids_are_deterministic_and_position_distinct(
            self, monkeypatch, tmp_path):
        # Cross-RUN distinctness is proven at the function level above (fields
        # 4 and 5 of the parametrized test); here the real end-to-end script
        # proves position distinctness within a run and determinism across
        # identical runs.
        body = {"id": "gpt-5.3-codex", "object": "response.input_tokens",
                "input_tokens": 7}
        res1 = TestWholeRenderedEvidence._run(monkeypatch, tmp_path,
                                                 body=body, headers={})
        res2 = TestWholeRenderedEvidence._run(monkeypatch, tmp_path,
                                                 body=body, headers={})
        ev1, ev2 = json.loads(res1.stdout), json.loads(res2.stdout)
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
                     "workflow_run_attempt", "workflow_job", "probed_at",
                     "results", "overall_ok"}
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
        res = TestWholeRenderedEvidence._run(monkeypatch, tmp_path, **kw)
        return json.loads(res.stdout)

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


class TestPositivePathIsFullyPinned:
    """F-04: fail-closed proofs are meaningless if the success path was never
    demonstrated against a realistic per-endpoint stub. This responder answers
    the model-retrieval and count endpoints correctly for all three models."""

    @staticmethod
    def _responder(url, data):
        import urllib.parse
        if "/models/" in url:
            model = urllib.parse.unquote(url.rsplit("/", 1)[-1])
            return 200, {"id": model, "object": "model"}
        payload = json.loads(data)
        return 200, {"object": "response.input_tokens",
                     "input_tokens": 30 + len(payload["model"])}

    def test_all_three_models_green_with_no_failure_exit(
            self, monkeypatch, tmp_path):
        res = TestWholeRenderedEvidence._run(
            monkeypatch, tmp_path, responder=self._responder)
        evidence = json.loads(res.stdout)
        assert evidence["overall_ok"] is True
        assert evidence["generation_calls"] == 0
        assert res.exit_code is None            # the script never called exit(1)
        assert len(evidence["results"]) == 3
        for result in evidence["results"]:
            model = result["requested_model_id"]
            assert result["model_retrieve"]["ok"] is True
            assert result["model_retrieve"]["returned_id"] == model
            assert result["model_retrieve"]["error"] is None
            count = result["input_token_count"]
            assert count["ok"] is True
            assert count["object_matches"] is True
            assert count["input_tokens"] == 30 + len(model)
            assert count["error"] is None


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
