"""Each visitor has their own rate-limit bucket behind the reverse proxy.

The limiter keys requests by client address (app/security.py). In production
the only way in is nginx-proxy-manager on the host network, and rootless
Podman's port handler rewrites every forwarded connection's source to
10.0.2.100 inside the container (measured on leaf 2026-09-28). Uvicorn trusts
X-Forwarded-For only from FORWARDED_ALLOW_IPS (default 127.0.0.1), so until
the deploy set it, every visitor was keyed as 10.0.2.100: one bucket for all.

The trust boundary is the host: a local process connecting to the loopback
port arrives as the same hop and is trusted like the proxy, as uvicorn's
default trusts 127.0.0.1. The port is no longer reachable from outside.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from slowapi.util import get_remote_address
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

ROOT = Path(__file__).resolve().parents[1]
PROXY_HOP = "10.0.2.100"


def _key_seen_by_the_limiter(trusted_hosts: str, forwarded_for: str) -> str:
    async def whoami(request: Request) -> PlainTextResponse:
        return PlainTextResponse(get_remote_address(request))

    app = ProxyHeadersMiddleware(Starlette(routes=[Route("/", whoami)]),
                                 trusted_hosts=trusted_hosts)
    transport = httpx.ASGITransport(app=app, client=(PROXY_HOP, 40000))

    async def fetch() -> str:
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            response = await client.get("/", headers={"X-Forwarded-For": forwarded_for})
            return response.text

    import asyncio

    return asyncio.run(fetch())


def test_the_limiter_keys_the_visitor_the_proxy_appended():
    # nginx's $proxy_add_x_forwarded_for appends the real peer to whatever
    # the client sent, so the header reads "<client's claim>, <real address>".
    assert _key_seen_by_the_limiter(PROXY_HOP, "203.0.113.9, 198.51.100.7") == "198.51.100.7"


def test_trusting_every_hop_would_let_a_visitor_choose_the_bucket():
    """Why the deploy names the hop instead of "*": with every hop trusted,
    uvicorn takes the LEFTMOST entry, which the client wrote itself."""
    assert _key_seen_by_the_limiter("*", "203.0.113.9, 198.51.100.7") == "203.0.113.9"


def test_the_default_trust_keys_every_visitor_as_the_proxy_hop():
    """The defect: uvicorn's default trusts only 127.0.0.1."""
    assert _key_seen_by_the_limiter("127.0.0.1", "203.0.113.9, 198.51.100.7") == PROXY_HOP


def _runs(deploy: str) -> list[str]:
    """Each `$ENGINE run -d ...` command in deploy.sh, joined across lines."""
    return [re.sub(r"\\\n\s*", " ", block)
            for block in re.findall(r"\$ENGINE run -d(?:[^\n]*\\\n)+[^\n]*", deploy)]


def test_deploy_publishes_on_loopback_and_trusts_only_the_proxy_hop():
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert f'PROXY_HOP="{PROXY_HOP}"' in deploy
    assert 'PUBLISH="127.0.0.1:$PORT:8000"' in deploy
    runs = _runs(deploy)
    assert len(runs) == 2, "the deploy and the rollback each start the container"
    for run in runs:
        assert '-p "$PUBLISH"' in run and 'FORWARDED_ALLOW_IPS="$PROXY_HOP"' in run, run


def test_compose_publishes_on_loopback_and_trusts_only_the_proxy_hop():
    compose = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:8000:8000"' in compose
    assert f'FORWARDED_ALLOW_IPS: "{PROXY_HOP}"' in compose


@pytest.mark.parametrize("value", [PROXY_HOP])
def test_uvicorn_reads_the_trust_from_the_environment(monkeypatch, value):
    import uvicorn

    monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
    assert uvicorn.Config("app.main:app").forwarded_allow_ips == value
