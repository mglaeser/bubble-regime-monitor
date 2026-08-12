"""`python -m midtermpanel.finalizecli` — no key, no pending status left behind.

Runs on every path including cancellation. Its single obligation is that every
`midterm-panel-*` context this run touched ends in a terminal state.
"""

from __future__ import annotations

import os
import sys

from . import PANEL_MODELS, REPOSITORY_NUMERIC_ID, attemptjournal
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


def _as_count(environ: dict, key: str):
    """An integer, or `UNKNOWN`. Never a zero invented to fill a gap.

    The previous version returned 0 on `ValueError`, which turned a job output
    that arrived malformed — or as the empty string from a job that never ran —
    into the affirmative claim "nothing was spent". That is the fail-open this
    function exists to remove: a run that cannot say what it spent must say so,
    because a zero is read as evidence and an absence is not."""
    raw = str(environ.get(key) or "").strip()
    if not raw:
        return attemptjournal.UNKNOWN
    try:
        value = int(raw)
    except ValueError:
        return attemptjournal.UNKNOWN
    return value if value >= 0 else attemptjournal.UNKNOWN


def _sum(*values):
    """`UNKNOWN` is absorbing: a total containing an unknown is unknown."""
    if any(value == attemptjournal.UNKNOWN for value in values):
        return attemptjournal.UNKNOWN
    return sum(values)


def _job_attempts(environ: dict, job: str) -> dict:
    """One spending job's contribution, and whether it may be believed.

    Three cases, kept apart because collapsing any two of them is how a zero
    gets invented:

    * the job never ran — it attempted nothing, and that is KNOWN, not unknown;
    * the job ran and its ledger is whole — the numbers are exact;
    * the job ran and its ledger is missing, invalid or truncated — UNKNOWN,
      whatever the numeric outputs happen to say. A number carried out of a
      broken ledger is not a smaller number; it is not a number."""
    result = str(environ.get(f"{job}_RESULT") or "").strip().lower()
    state = str(environ.get(f"MIDTERM_{job}_ACCOUNTING_STATE") or "").strip()
    complete = str(environ.get(f"MIDTERM_{job}_ACCOUNTING_COMPLETE") or ""
                   ).strip().lower() == "true"
    if result in ("", "skipped", "cancelled"):
        return {"provider": 0, "generation": 0, "believable": True,
                "state": attemptjournal.ATTEMPTS_KNOWN_ZERO, "ran": False}
    if state not in attemptjournal.KNOWN_STATES or not complete:
        return {"provider": attemptjournal.UNKNOWN,
                "generation": attemptjournal.UNKNOWN, "believable": False,
                "state": state or attemptjournal.ATTEMPT_ACCOUNTING_UNAVAILABLE,
                "ran": True}
    return {"provider": _as_count(environ, f"MIDTERM_{job}_PROVIDER_ATTEMPTS"),
            "generation": _as_count(
                environ, f"MIDTERM_{job}_GENERATION_ATTEMPTS")
            if job == "PANEL" else 0,
            "believable": True, "state": state, "ran": True}


def attempt_totals(environ: dict) -> dict:
    """What was attempted, from the durable ledger rather than the evidence.

    The evidence files are written at the END of each path, so a run that
    refused after its first provider call reported nothing at all — the state
    panel run 31608202983 ended in. The attempt journal is written before each
    call and survives the refusal, and its totals arrive here as job outputs
    because `RUNNER_TEMP` does not cross a job boundary.

    One unbelievable job poisons the total. A believable count beside an
    unreadable panel is not a run that spent the count's number; it is a run
    that cannot say what it spent."""
    jobs = {job: _job_attempts(environ, job) for job in ("COUNT", "PANEL")}
    provider = _sum(*(j["provider"] for j in jobs.values()))
    generation = _sum(*(j["generation"] for j in jobs.values()))
    states = sorted({j["state"] for j in jobs.values() if j["ran"]})
    if not any(j["believable"] for j in jobs.values()) or not all(
            j["believable"] for j in jobs.values()):
        unbelievable = [j["state"] for j in jobs.values()
                        if not j["believable"]]
        return {"provider_calls": attemptjournal.UNKNOWN,
                "generation_calls": attemptjournal.UNKNOWN,
                "accounting_state": unbelievable[0],
                "accounting_detail": f"job states {states or ['none']}"}
    if not states:
        return {"provider_calls": 0, "generation_calls": 0,
                "accounting_state": attemptjournal.ATTEMPTS_KNOWN_ZERO,
                "accounting_detail": "no job that could spend ever ran"}
    state = (attemptjournal.ATTEMPTS_KNOWN_NONZERO
             if attemptjournal.ATTEMPTS_KNOWN_NONZERO in states
             else attemptjournal.ATTEMPTS_KNOWN_ZERO)
    return {"provider_calls": provider, "generation_calls": generation,
            "accounting_state": state,
            "accounting_detail": f"job states {states}"}


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
    totals = attempt_totals(environ)
    text = assert_no_trusted_claim(render_summary(
        pr_number=environ.get("CANDIDATE_PR_NUMBER", "?"), head_sha=head,
        base_sha=environ.get("CANDIDATE_BASE_SHA", "?"),
        ordinary_checks={}, high_risk=(
            str(environ.get("HIGH_RISK_WORKFLOW_CHANGE", "")).lower() == "true"),
        count_state=latest.get("midterm-panel-count", "not published"),
        panel_state=latest.get("midterm-panel-review", "not published"),
        models=list(PANEL_MODELS),
        decision=environ.get("MIDTERM_DECISION", "unknown"),
        evidence_digests={},
        provider_calls=totals["provider_calls"],
        generation_calls=totals["generation_calls"],
        accounting_state=totals["accounting_state"],
        accounting_detail=totals["accounting_detail"],
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
