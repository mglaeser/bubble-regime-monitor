"""structlog JSON logging configuration."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper(), 20))
    # httpx logs the FULL request URL at INFO on EVERY request, success
    # included — and FRED, Alpha Vantage, Polygon and Twelve Data all carry
    # their API key in that query string. The service runs containerised, so
    # those lines land in the container's journal, which the operator reads on
    # a failed release. One line closes all four providers on
    # both the success and the failure path.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # httpcore's DEBUG lines carry each answer's reason phrase and headers, a redirect's Location among them.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # The root logger above is every library's in this process: the boot
    # migration leaves it alone (app.db_migrate), where Alembic's own setup
    # used to replace it with alembic.ini's WARN at every boot. Two libraries
    # write a line per event and are held here.
    # APScheduler writes two INFO lines for every run of every job, and the
    # alert dispatcher runs every 20 s: 8,640 lines a day for that job alone.
    # Its warnings and errors stay (a run that was missed or skipped, a job
    # that raised; the production journal of 2026-10-05 to 2026-11-10 holds
    # none).
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    # slowapi writes one WARNING per refused request (executed: 80 requests
    # over the limit, 80 lines). The 429 is the answer. The production journal
    # of 2026-10-05 to 2026-11-10 holds no such line either: Alembic's setup
    # at boot disabled this logger (executed with the real server before this
    # change: 80 refusals, 0 lines).
    # Its errors stay (a limit it could not load or apply).
    logging.getLogger("slowapi").setLevel(logging.ERROR)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), 20)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
