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
    # a failed release. This line closes httpx's own request log line for
    # all four providers, on the success and on the failure path. The text
    # of an HTTP status error is a second channel, and this line does not
    # reach it: app.http_client.require_success raises that error without
    # the query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)
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
