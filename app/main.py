"""FastAPI app factory, router mounting, lifespan."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
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

    from app.db import get_engine
    from app.models import Base

    Base.metadata.create_all(get_engine())  # Alembic owns migrations; create_all covers first boot

    def _seed() -> None:
        try:
            from app.services.backfill import seed_hy_oas_history

            seed_hy_oas_history()
        except Exception as exc:
            log.warning("hy_oas_seed_skipped", error=str(exc))

    # First-boot HY OAS seeding does a live FRED fetch — run it off the boot
    # path so a slow/unreachable FRED can never delay readiness.
    import threading

    threading.Thread(target=_seed, name="hy-oas-seed", daemon=True).start()

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

    from app.routers import admin, health, indicators, legs, meta, score
    from app.security import limiter

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    app.include_router(score.router)
    app.include_router(indicators.router)
    app.include_router(legs.router)
    app.include_router(meta.router)
    app.include_router(health.router)
    app.include_router(admin.router)
    return app


app = create_app()
