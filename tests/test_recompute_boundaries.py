"""`run_recompute` executed: a step that fails after the commit does not cost the snapshot.

docs/ALERT_SYSTEM.md: "Alerting never touches scoring"; the hook runs "after
the snapshot commits, inside a `try/except` that logs and swallows".
DASHBOARD_FEED_SPEC.md: "a feed failure never touches scoring".

The test that carried this claim in tests/test_alert_foundations.py patched the
hook and then called persist_snapshot, which never calls it.
"""

from __future__ import annotations

import importlib

import pytest
from sqlalchemy import func, select

from app.db import session_scope
from app.models import Snapshot
from app.services import compute
from tests.conftest import make_golden_raw_inputs

#: The two documented steps after the commit, each inside a boundary of its own:
#: (module, attribute) as `run_recompute` imports them when it reaches the step.
STEPS = {
    "feed": ("app.services.dashboard_feed", "build_and_persist_feed"),
    "alerts": ("app.services.alert_integration", "on_snapshot_committed"),
}


def _snapshots() -> int:
    with session_scope() as session:
        return session.execute(select(func.count()).select_from(Snapshot)).scalar_one()


@pytest.mark.parametrize("failing", sorted(STEPS))
def test_a_failing_step_after_the_commit_does_not_cost_the_snapshot(isolated_db, monkeypatch, failing):
    monkeypatch.setattr(compute, "gather_inputs", make_golden_raw_inputs)
    reached = []
    for name, (module, attribute) in STEPS.items():
        def _step(*_a, _name=name, **_k):
            reached.append(_name)
            if _name == failing:
                raise RuntimeError(f"{_name} exploded")

        monkeypatch.setattr(importlib.import_module(module), attribute, _step)

    snap_id = compute.run_recompute()                   # must not raise

    assert set(reached) == set(STEPS)                   # the failure did not stop the other step
    assert _snapshots() == 1
    with session_scope() as session:
        assert session.get(Snapshot, snap_id) is not None
