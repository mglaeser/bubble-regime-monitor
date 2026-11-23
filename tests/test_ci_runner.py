"""Every job of the blocking gate names its runner release.

"ubuntu-latest" is a label that moves. Between 2026-11-12 and 2026-11-21 it went
from Ubuntu 24.04 to 26.04, and the gate turned red on an unchanged main: on the
26.04 image tests/test_host_outage_notifier.py::
test_a_podman_that_ignores_sigterm_still_cannot_block_the_alert fails (the script
under test did not return within the test's thirty seconds); on the 24.04 image
the same tree passes. The suite executes the scripts under deploy/, so the release
it runs on is part of what it proves, and it changes by a commit, not by a label.
"""
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
GATE_RELEASE = "ubuntu-24.04"


def test_every_job_of_the_gate_names_one_release():
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    assert jobs, "ci.yml defines no job"
    assert {name: job["runs-on"] for name, job in jobs.items()} == dict.fromkeys(jobs, GATE_RELEASE)
