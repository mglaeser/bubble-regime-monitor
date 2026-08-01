"""Audit remediation (v3.3.1) — red→green regression + preventive invariants.

Derived from the frozen behavioural spec (README epistemic guardrails +
audit/00 system map), NOT from the code under test:
  * A-25  a public read endpoint must never 500 on malformed input.
  * B-06/C-01  the guessable placeholder admin key must never authenticate.
  * C-07/C-08  no external free-text field may enter the LLM prompt template.
  * C-23  a personal phone number must not appear in cleartext in logs.
"""

from __future__ import annotations

import string

import pytest
from fastapi import HTTPException


class TestHistoryInputValidation:
    """A-25: GET /score/history with a bad date must be 422, not 500."""

    def _client(self):
        from fastapi.testclient import TestClient

        import app.main as m

        return TestClient(m.app, raise_server_exceptions=False)

    def test_bad_from_date_is_422_not_500(self):
        r = self._client().get("/api/v1/score/history?from=garbage")
        assert r.status_code == 422, f"expected 422, got {r.status_code}"

    def test_bad_to_date_is_422_not_500(self):
        r = self._client().get("/api/v1/score/history?to=not-a-date")
        assert r.status_code == 422, f"expected 422, got {r.status_code}"

    def test_valid_date_still_accepted(self, isolated_db):
        # a well-formed date must not be rejected (no over-correction)
        r = self._client().get("/api/v1/score/history?from=2026-01-01")
        assert r.status_code == 200  # empty history is a valid 200, not a 4xx/5xx

    def test_far_future_to_date_no_500(self, isolated_db):
        # ?to=9999-12-31 parses fine but to+1day overflows datetime.max ->
        # must NOT 500 (adversarial-verifier finding). Clamped to a valid query.
        r = self._client().get("/api/v1/score/history?to=9999-12-31")
        assert r.status_code == 200


class TestAdminKeyFailClosed:
    """B-06/C-01: the shipped placeholder admin key must not authenticate."""

    def test_placeholder_key_is_rejected(self, monkeypatch):
        from app import security

        class _S:
            admin_api_key = "change-me-to-a-long-random-string"  # pragma: allowlist secret -- throwaway test fixture
            read_endpoints_public = True

        monkeypatch.setattr(security, "get_settings", lambda: _S())
        with pytest.raises(HTTPException) as exc:
            security.require_admin_key(x_api_key="change-me-to-a-long-random-string")  # pragma: allowlist secret -- throwaway test fixture
        assert exc.value.status_code in (401, 503)

    def test_empty_key_is_rejected(self, monkeypatch):
        from app import security

        class _S:
            admin_api_key = ""
            read_endpoints_public = True

        monkeypatch.setattr(security, "get_settings", lambda: _S())
        with pytest.raises(HTTPException):
            security.require_admin_key(x_api_key="")

    def test_real_key_still_authenticates(self, monkeypatch):
        from app import security

        class _S:
            admin_api_key = "a-genuinely-long-random-secret-value-123456"  # pragma: allowlist secret -- throwaway test fixture
            read_endpoints_public = True

        monkeypatch.setattr(security, "get_settings", lambda: _S())
        # correct key: no exception
        security.require_admin_key(x_api_key="a-genuinely-long-random-secret-value-123456")  # pragma: allowlist secret -- throwaway test fixture
        # wrong key: 401
        with pytest.raises(HTTPException) as exc:
            security.require_admin_key(x_api_key="wrong")
        assert exc.value.status_code == 401

    def test_non_ascii_key_is_401_not_500(self, monkeypatch):
        # a non-ASCII X-API-Key must not raise a TypeError out of compare_digest
        # (which would surface as HTTP 500) — adversarial-verifier finding.
        from app import security

        class _S:
            admin_api_key = "a-genuinely-long-random-secret-value-123456"  # pragma: allowlist secret -- throwaway test fixture
            read_endpoints_public = True

        monkeypatch.setattr(security, "get_settings", lambda: _S())
        with pytest.raises(HTTPException) as exc:
            security.require_admin_key(x_api_key="cl\xe9f-invalide")  # latin-1 é; pragma: allowlist secret -- throwaway test fixture
        assert exc.value.status_code == 401


class TestReadAccessFailClosed:
    """B-06 clone (adversarial-verifier finding): keyed reads must also refuse
    the placeholder admin key, not just the admin endpoints."""

    def test_keyed_reads_reject_placeholder(self, monkeypatch):
        from app import security

        class _S:
            admin_api_key = "change-me-to-a-long-random-string"  # pragma: allowlist secret -- throwaway test fixture
            read_endpoints_public = False  # keyed reads

        monkeypatch.setattr(security, "get_settings", lambda: _S())
        with pytest.raises(HTTPException) as exc:
            security.require_read_access(request=None, x_api_key="change-me-to-a-long-random-string")  # pragma: allowlist secret -- throwaway test fixture
        assert exc.value.status_code == 503

    def test_public_reads_still_open(self, monkeypatch):
        from app import security

        class _S:
            admin_api_key = "change-me-to-a-long-random-string"  # pragma: allowlist secret -- throwaway test fixture
            read_endpoints_public = True

        monkeypatch.setattr(security, "get_settings", lambda: _S())
        security.require_read_access(request=None, x_api_key=None)  # no exception


class TestPromptIsNumbersOnly:
    """C-07/C-08 preventive invariant: the LLM prompt template must interpolate
    only the known numeric/enum fields — no field that could carry external
    free-text. Guards the architectural containment (A-10)."""

    ALLOWED = {
        "median", "iqr_lo", "iqr_hi", "band", "s_scores", "d_scores", "v",
        "red_flag_detail", "override", "spy_state", "qqq_state", "fast_alarm",
    }

    def test_judgment_prompt_fields_are_allowlisted(self):
        from app.engine.judgment import PROMPT_TEMPLATE

        fields = {fn for _, fn, _, _ in string.Formatter().parse(PROMPT_TEMPLATE) if fn}
        assert fields, "template has no interpolated fields?"
        assert fields <= self.ALLOWED, f"unexpected prompt field(s): {fields - self.ALLOWED}"

    def test_sms_prompt_only_prior_llm_free_text(self):
        # The SMS prompt is a SECOND model prompt (adversarial-verifier finding:
        # the judgment-prompt test gave false assurance of covering "the LLM
        # prompt"). Its fields must be the numeric/enum set plus exactly two
        # documented non-numeric fields: `limit` (an int) and `judgment` — which
        # is PRIOR, length-capped LLM output, never external source/API/DB text.
        from app.engine.sms_report import SMS_PROMPT

        numeric = {"median", "band", "iqr_lo", "iqr_hi", "flags", "override",
                   "spy", "qqq", "s", "d", "limit"}
        allowed = numeric | {"judgment"}
        fields = {fn for _, fn, _, _ in string.Formatter().parse(SMS_PROMPT) if fn}
        assert fields <= allowed, f"unexpected SMS prompt field(s): {fields - allowed}"
        # the only non-numeric interpolation is the prior LLM note
        assert (fields - numeric) <= {"judgment"}


class TestRecipientMasking:
    """C-23: the SMS recipient (personal data) must be masked before logging."""

    def test_mask_hides_the_number(self):
        from app.notify.sipgate import _mask_recipient

        masked = _mask_recipient("+490000000254")  # fabricated number, not a real recipient
        assert "0000000254" not in masked
        assert masked.endswith("254")  # last 3 kept for support triage
        assert masked != "+490000000254"

    def test_mask_handles_empty(self):
        from app.notify.sipgate import _mask_recipient

        assert _mask_recipient("") == "(none)"
