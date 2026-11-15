"""In-process scheduled jobs: the alert system's, and the database's daily copy
(db_backup.py).

Each job is a thin, self-contained wrapper: it opens its own session (the copy:
its own connections), does one bounded piece of work, and never raises into the
scheduler. A failing job must degrade the alert system (the copy: leave the
earlier copies as they are), never take down the service.
"""

from __future__ import annotations
