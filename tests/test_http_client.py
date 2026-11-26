"""app.http_client has no circuit breaker: every fetch asks its upstream."""

from __future__ import annotations

import httpx
import pytest

from app import http_client


def test_a_label_that_failed_three_times_in_a_row_is_asked_by_the_next_fetch(monkeypatch):
    asked: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(404) if len(asked) <= 3 else httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(http_client, "_client", httpx.Client(transport=httpx.MockTransport(answer)))
    for _ in range(3):
        with pytest.raises(httpx.HTTPStatusError):
            http_client.fetch("upstream", "https://upstream.invalid/x")

    assert http_client.fetch("upstream", "https://upstream.invalid/x").json() == {"ok": True}
    assert len(asked) == 4, "four fetches, four requests"
