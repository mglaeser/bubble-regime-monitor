"""Browser-origin allowlist contract."""

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
