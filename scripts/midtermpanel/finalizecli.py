"""`python -m midtermpanel.finalizecli` — no key, no pending status left behind.

Runs on every path including cancellation. Its single obligation is that every
`midterm-panel-*` context this run touched ends in a terminal state.
"""

from __future__ import annotations

import os
import sys

from . import PANEL_MODELS, REPOSITORY_NUMERIC_ID
from .clibase import require_env, run, self_test_report, self_test_requested, summary
from .finalize import (
    assert_no_trusted_claim,
    closure_description,
    contexts_needing_closure,
    latest_state_per_context,
    render_summary,
)
from .githubapi import ReadOnlyGitHub
from .status import publish, status_request

REQUIRED = ("GITHUB_TOKEN", "CANDIDATE_HEAD_SHA", "GITHUB_RUN_ID",
            "GITHUB_RUN_ATTEMPT")


def perform(environ: dict, *, api, opener) -> dict:
    env = require_env(environ, REQUIRED, where="finalizecli")
    head = env["CANDIDATE_HEAD_SHA"]
    run_id, attempt = int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"])
    target = environ.get("MIDTERM_RUN_URL") or (
        "https://github.com/mglaeser/bubble-regime-monitor/actions/runs/"
        f"{run_id}")

    statuses = api.commit_statuses(head)
    job_results = {"preflight": environ.get("PREFLIGHT_RESULT", ""),
                   "count": environ.get("COUNT_RESULT", ""),
                   "panel": environ.get("PANEL_RESULT", "")}
    closed = []
    for record in contexts_needing_closure(statuses=statuses,
                                           job_results=job_results):
        closed.append(publish(status_request(
            repository_numeric_id=REPOSITORY_NUMERIC_ID,
            candidate_head_sha=head, context=record["context"], state="error",
            description=closure_description(record), target_url=target,
            run_id=run_id, run_attempt=attempt),
            opener=opener, token=env["GITHUB_TOKEN"]))

    latest = latest_state_per_context(statuses)
    text = assert_no_trusted_claim(render_summary(
        pr_number=environ.get("CANDIDATE_PR_NUMBER", "?"), head_sha=head,
        base_sha=environ.get("CANDIDATE_BASE_SHA", "?"),
        ordinary_checks={}, high_risk=(
            str(environ.get("HIGH_RISK_WORKFLOW_CHANGE", "")).lower() == "true"),
        count_state=latest.get("midterm-panel-count", "not published"),
        panel_state=latest.get("midterm-panel-review", "not published"),
        models=list(PANEL_MODELS),
        decision=environ.get("MIDTERM_DECISION", "unknown"),
        evidence_digests={}, provider_calls=int(
            environ.get("MIDTERM_PROVIDER_CALLS", 0) or 0),
        generation_calls=int(environ.get("MIDTERM_GENERATION_CALLS", 0) or 0),
        run_url=target))
    summary(text)
    return {"closed": closed, "latest": latest, "summary": text}


def main() -> None:
    import urllib.request

    environ = dict(os.environ)
    api = ReadOnlyGitHub(token=environ["GITHUB_TOKEN"],
                         repository_numeric_id=REPOSITORY_NUMERIC_ID)
    perform(environ, api=api, opener=urllib.request.urlopen)


def _self_test() -> int:
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    touches_provider = any(
        isinstance(n, (ast.Import, ast.ImportFrom))
        and "transport" in ((getattr(n, "module", "") or "")
                            + " ".join(a.name for a in n.names))
        for n in ast.walk(tree))
    return self_test_report("finalizecli", {
        "module imports": True,
        "perform is callable": callable(perform),
        "holds no provider seam": not touches_provider,
        "summary refuses a trust claim": callable(assert_no_trusted_claim),
    })


if __name__ == "__main__":
    if self_test_requested(sys.argv[1:]):
        raise SystemExit(_self_test())
    raise SystemExit(run(main, name="finalizecli"))
