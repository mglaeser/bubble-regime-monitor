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

#: The one state in which there is genuinely nothing to close out.
NOTHING_TO_FINALIZE = "NOTHING_TO_FINALIZE_PREFLIGHT_RESOLVED_NO_CANDIDATE"


def preflight_never_resolved_a_candidate(environ: dict) -> bool:
    """True when preflight refused BEFORE it published a head to close against.

    `finalize` runs `if: always()` and its obligation is that no
    `midterm-panel-*` status is left pending. That obligation is about a
    COMMIT, and when preflight refuses early there is no commit: its outputs
    render as empty strings, and no status was ever published because `count`
    — the first thing that publishes — was skipped.

    The first real privileged run failed here for exactly that reason, turning
    one honest refusal into two red jobs and no summary.

    Deliberately narrow. It requires preflight to have NOT succeeded. If
    preflight reports success and the head is still blank, that is an output
    that went missing between jobs — the defect this lane already lost two
    digests to — and it must keep failing loudly."""
    head = str(environ.get("CANDIDATE_HEAD_SHA") or "").strip()
    preflight = str(environ.get("PREFLIGHT_RESULT") or "").strip()
    return not head and preflight not in ("", "success")


def perform(environ: dict, *, api, opener) -> dict:
    if preflight_never_resolved_a_candidate(environ):
        return {"outcome": NOTHING_TO_FINALIZE,
                "preflight_result": environ.get("PREFLIGHT_RESULT"),
                "closed": [], "statuses_seen": 0,
                "honest_scope": (
                    "preflight refused before resolving a candidate head, so "
                    "no midterm-panel status was ever published and there is "
                    "nothing to close. This is not a claim that the run "
                    "succeeded — preflight's own failure is the run's result")}
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
    outcome = perform(environ, api=api, opener=urllib.request.urlopen)
    if outcome.get("outcome") == NOTHING_TO_FINALIZE:
        # Said out loud. A job that exits 0 having done nothing must report
        # WHY, or its green is indistinguishable from a green that closed
        # statuses it never looked at.
        summary(f"### {NOTHING_TO_FINALIZE}\n\n{outcome['honest_scope']}\n\n"
                f"- preflight result: `{outcome['preflight_result']}`\n"
                "- midterm-panel statuses published this run: none\n")
        sys.stdout.write(f"{NOTHING_TO_FINALIZE}: "
                         f"preflight={outcome['preflight_result']}\n")


def _self_test() -> int:
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    touches_provider = any(
        isinstance(n, (ast.Import, ast.ImportFrom))
        and "transport" in ((getattr(n, "module", "") or "")
                            + " ".join(a.name for a in n.names))
        for n in ast.walk(tree))
    # `callable(assert_no_trusted_claim)` is what used to be here, and it is
    # the shape of self-test that reports green for a function that refuses
    # every input. It did: the guard rejected this module's OWN summary, so the
    # closeout could never publish one, and a probe asking whether the name
    # exists could not tell. Both directions are exercised now, against the
    # real renderer rather than a fixture.
    from .errors import PanelRefusal
    rendered = render_summary(
        pr_number=0, head_sha="0" * 40, base_sha="0" * 40, ordinary_checks={},
        high_risk=True, count_state="success", panel_state="success",
        models=list(PANEL_MODELS), decision="SELF_TEST", evidence_digests={},
        provider_calls=0, generation_calls=0, run_url="self-test")
    try:
        assert_no_trusted_claim(rendered)
        accepts_its_own_summary = True
    except PanelRefusal:
        accepts_its_own_summary = False
    try:
        assert_no_trusted_claim("The verdict is a trusted review.")
        refuses_a_real_claim = False
    except PanelRefusal:
        refuses_a_real_claim = True
    return self_test_report("finalizecli", {
        "module imports": True,
        "perform is callable": callable(perform),
        "holds no provider seam": not touches_provider,
        "accepts the summary it renders": accepts_its_own_summary,
        "still refuses an actual trust claim": refuses_a_real_claim,
    })


if __name__ == "__main__":
    if self_test_requested(sys.argv[1:]):
        raise SystemExit(_self_test())
    raise SystemExit(run(main, name="finalizecli"))
