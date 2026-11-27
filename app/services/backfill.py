"""Historical seeding."""

from __future__ import annotations

from sqlalchemy import select

from app.db import session_scope
from app.logging_conf import get_logger

log = get_logger(__name__)


def seed_hy_oas_history() -> int:
    """First-boot seeding of hy_oas_history with the ~3 years FRED still serves.

    (FRED truncated BAMLH0A0HYM2 to a rolling 3-year window in April 2026.)
    Returns the number of rows now persisted."""
    from datetime import date

    from app.models import HyOasHistory
    from app.sources.fred import observations

    with session_scope() as session:
        existing = session.execute(select(HyOasHistory).limit(1)).scalars().first()
        if existing is None:
            try:
                for d, v in observations("BAMLH0A0HYM2"):
                    session.merge(HyOasHistory(date=date.fromisoformat(d), oas_bps=v * 100.0))
            except Exception as exc:
                log.warning("hy_oas_seed_failed", error=str(exc))
        count = len(session.execute(select(HyOasHistory)).scalars().all())
    return count
