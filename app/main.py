"""FastAPI app factory, router mounting, lifespan."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import get_settings
from app.logging_conf import configure_logging, get_logger
from app.references import DISCLAIMER

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    from app.db_migrate import ensure_schema

    ensure_schema()  # Alembic upgrade head (create_all fallback); self-heals legacy DBs

    def _seed() -> None:
        try:
            from app.services.backfill import seed_hy_oas_history

            seed_hy_oas_history()
        except Exception as exc:
            log.warning("hy_oas_seed_skipped", error=str(exc))
        try:
            from app.engine.gsadf_runner import r_selfcheck

            r_selfcheck()
        except Exception as exc:
            log.warning("gsadf_selfcheck_skipped", error=str(exc))

    def _breadth_backfill() -> None:
        # Cold-start breadth backfill: warms breadth_symbol_cache so D1 comes
        # alive without waiting for the first scheduled 01:00/13:00 sweep. It
        # is self-limiting — a warm cache leaves few stale/missing symbols, so
        # this returns quickly on restarts.
        try:
            from app.sources.breadth import DEFAULT_BACKFILL, refresh_breadth

            refresh_breadth(max_symbols=DEFAULT_BACKFILL)  # Polygon cold-start when keyed, else TD
        except Exception as exc:
            log.warning("breadth_backfill_skipped", error=str(exc))

    # First-boot seeding does live network fetches — run off the boot path so a
    # slow/unreachable upstream can never delay readiness. Breadth backfill gets
    # its own thread (it can take ~1h fully cold) so it never serializes the
    # quick HY-OAS seed / GSADF self-check behind it.
    import threading

    threading.Thread(target=_seed, name="hy-oas-seed", daemon=True).start()
    threading.Thread(target=_breadth_backfill, name="breadth-backfill", daemon=True).start()

    from app import scheduler

    scheduler.start()
    yield
    scheduler.shutdown()
    from app.http_client import close_client

    close_client()


def create_app() -> FastAPI:
    app = FastAPI(
        title="bubblegauge",
        version=get_settings().service_version,
        description=(
            "Self-hosted AI bubble regime monitor. Three-leg composite (valuation, credit, "
            "breadth, GSADF, LPPLS) with Monte Carlo bands, Faber trend trigger, and API. "
            "Research, not advice.\n\n" + DISCLAIMER
        ),
        lifespan=lifespan,
    )

    from slowapi import _rate_limit_exceeded_handler

    from app.routers import admin, health, indicators, legs, meta, score, status
    from app.security import limiter

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # CORS so a browser dashboard on another origin (crash.klee.me) can call the
    # public read API. Reads are public (READ_ENDPOINTS_PUBLIC=true) so no
    # credentials/X-API-Key are sent — keep allow_credentials=False. GET-only
    # (read-only API). Added at app level, so it covers every route
    # (/api/v1/*, /api/v1/status, /healthz, /readyz, /).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://crash.klee.me",
            # add local dev origins only if a dashboard dev server calls the API, e.g.:
            # "http://localhost:8000",
        ],
        allow_credentials=False,   # public read-only data, no cookies — keep False
        allow_methods=["GET"],     # read-only API
        allow_headers=["*"],
        max_age=86400,
    )

    app.include_router(score.router)
    app.include_router(indicators.router)
    app.include_router(legs.router)
    app.include_router(meta.router)
    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(status.router)
    return app


app = create_app()
