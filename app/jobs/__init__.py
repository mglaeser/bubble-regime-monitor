"""In-process scheduled jobs for the alert system.

Each job is a thin, self-contained wrapper: it opens its own session, does one
bounded piece of work, and never raises into the scheduler. A failing job must
degrade the alert system, never take down the service.
"""

from __future__ import annotations
