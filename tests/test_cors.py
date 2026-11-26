"""Browser-origin allowlist contract."""

import re
from pathlib import Path

from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.main import create_app


def test_ai_bubble_origin_is_allowed_and_kochbo_origins_are_absent(isolated_db):
    app = create_app()

    cors = next(
        middleware
        for middleware in app.user_middleware
        if middleware.cls is CORSMiddleware
    )
    origins = cors.kwargs["allow_origins"]
    assert not any("kochbo" in origin.casefold() for origin in origins)

    with TestClient(app) as client:
        response = client.options(
            "/healthz",
            headers={
                "Origin": "https://ai-bubble.fyi",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://ai-bubble.fyi"


def test_the_retired_klee_me_origins_are_absent(isolated_db):
    """DR-014 / Q21: crash.klee.me left the allowlist when the site moved.

    Pinned rather than merely deleted, because the risk outlives the move. The
    host does not resolve today, so nothing breaks either way and a revert
    would look harmless in review — but an allowlisted domain that has lapsed
    can be registered by anyone, and whoever registers it inherits a
    browser-trusted origin against this API.
    """
    app = create_app()

    cors = next(
        middleware
        for middleware in app.user_middleware
        if middleware.cls is CORSMiddleware
    )
    origins = cors.kwargs["allow_origins"]
    assert not any("klee.me" in origin.casefold() for origin in origins), origins
    assert "https://ai-bubble.fyi" in origins, "the live dashboard must stay"


def test_the_feed_documents_name_the_allowed_origins_and_no_other(isolated_db):
    """The dashboard feed is what a browser on another origin reads, and its
    contract (DASHBOARD_FEED_SPEC.md section 3) says which origins may."""
    import app.routers.dashboard as route
    import app.services.dashboard_feed as feed

    cors = next(
        middleware
        for middleware in create_app().user_middleware
        if middleware.cls is CORSMiddleware
    )
    spec = (Path(__file__).resolve().parents[1] / "DASHBOARD_FEED_SPEC.md").read_text(
        encoding="utf-8")
    line = next(ln for ln in spec.splitlines() if "CORS allows" in ln)
    assert set(re.findall(r"https://[a-z0-9.-]+", line)) == set(cors.kwargs["allow_origins"])
    for module in (route, feed):
        assert "crash.klee.me" not in module.__doc__, module.__name__
