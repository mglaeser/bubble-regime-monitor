"""Leave no status pending, and say honestly what happened.

## Why this job exists at all

Every other job can die. A runner can be reclaimed, a step can time out, a
refusal can fire between publishing `pending` and publishing a terminal state.
Each of those leaves a `midterm-panel-*` status sitting at `pending` on a real
commit, and a pending status is not a neutral outcome:

  * under a required-status configuration it blocks the pull request forever;
  * under any other configuration it reads as "still working" indefinitely,
    which is indistinguishable from a panel that is genuinely still running.

So `finalize` runs on every path — `if: always()` — with NO credential in scope,
and its single obligation is that every context this run touched ends terminal.

## Why it holds no key

It runs after failures, which is exactly when the least-exercised code paths
execute. A credential in scope during the paths nobody tests is the credential
most likely to reach a log.
"""

from __future__ import annotations

import re

from . import HIGH_RISK_MARKER, PANEL_STATUSES
from .errors import refuse
from .status import TERMINAL_STATES

#: Job results that mean the corresponding status cannot legitimately be green.
UNSUCCESSFUL_RESULTS = ("failure", "cancelled", "skipped")


def latest_state_per_context(statuses: list) -> dict:
    """The current state of each mid-term context on the head.

    GitHub returns statuses newest-first; this takes the FIRST occurrence of
    each context for that reason. Taking the last would report the `pending` that
    was published before the terminal one and conclude the run never finished."""
    latest = {}
    for entry in statuses or []:
        if not isinstance(entry, dict):
            continue
        context = str(entry.get("context") or "")
        if context in PANEL_STATUSES and context not in latest:
            latest[context] = str(entry.get("state") or "")
    return latest


def contexts_needing_closure(*, statuses: list, job_results: dict) -> list:
    """Which contexts must be forced to `error`, and why.

    A context is closed out when it is still pending. The job results decide the
    REASON, not the decision: a pending status is wrong regardless of why, and
    keying the decision on job results would leave a pending status untouched
    whenever the job that abandoned it reported success."""
    latest = latest_state_per_context(statuses)
    failed_jobs = sorted(name for name, result in (job_results or {}).items()
                         if str(result) in UNSUCCESSFUL_RESULTS)
    needing = []
    for context in PANEL_STATUSES:
        state = latest.get(context)
        if state is None:
            # Never published. Nothing to close: publishing an error for a
            # context that never went pending would invent a failure for a job
            # that correctly refused before starting.
            continue
        if state not in TERMINAL_STATES:
            needing.append({
                "context": context,
                "observed_state": state,
                "reason": (f"a prior job ended {failed_jobs}" if failed_jobs
                           else "the run ended without publishing a terminal "
                                "state"),
            })
    return needing


def closure_description(record: dict) -> str:
    """A short, sanitized reason that fits GitHub's 140-character limit.

    Never includes provider text, exception messages or server bodies. The
    status description is published on a public-ish surface and is the one place
    a careless interpolation would be most visible."""
    text = (f"mid-term panel did not complete: {record['reason']}. "
            "Not write-separated; human merge required.")
    return text[:140]


def render_summary(*, pr_number, head_sha: str, base_sha: str,
                   ordinary_checks: dict, high_risk: bool,
                   count_state: str, panel_state: str, models: list,
                   decision: str, evidence_digests: dict,
                   provider_calls: int, generation_calls: int,
                   run_url: str) -> str:
    """The job summary. Aggregates only — no prompts, no verdict text.

    The private plan and the raw verdicts carry the candidate's code and the
    reviewer's questions. Only digests and counts appear here."""
    lines = [
        "## Mid-term single-repository panel",
        "",
        "> **This is not a trusted review.** The panel runs from a "
        "repository-scoped secret in the repository it reviews. It is not "
        "write-separated, rests on no recorded operator prerequisite, and a "
        "pull request that edits a `pull_request` workflow may be able to read "
        "the same key. **A human must make the merge decision on the exact "
        "head.**",
        "",
        f"- pull request: **#{pr_number}**",
        f"- head: `{head_sha}`",
        f"- base: `{base_sha}`",
        f"- ordinary checks: {', '.join(f'{k}={v}' for k, v in sorted(ordinary_checks.items())) or 'n/a'}",
        f"- `midterm-panel-count`: **{count_state}**",
        f"- `midterm-panel-review`: **{panel_state}**",
        f"- panel: {', '.join(sorted(models)) or 'n/a'}",
        f"- aggregate decision: **{decision}**",
        f"- provider calls: **{provider_calls}** · generation calls: "
        f"**{generation_calls}**",
        f"- run: {run_url}",
    ]
    if evidence_digests:
        lines.append("- evidence digests:")
        lines += [f"  - `{k}`: `{str(v)[:16]}…`"
                  for k, v in sorted(evidence_digests.items())]
    if high_risk:
        lines += [
            "",
            f"### {HIGH_RISK_MARKER}",
            "",
            "This pull request changes GitHub Actions or lane code while a "
            "repository-scoped provider secret exists. The panel review does "
            "**not** prevent the pull request's own workflows from referencing "
            "that secret. Require a human review of the workflow diff, and "
            "consider rotating the key after merge.",
        ]
    return "\n".join(lines)


#: Phrases the summary may mention but must never assert about itself.
FORBIDDEN_CLAIMS = ("trusted review", "write-separated review",
                    "independently verified", "third-party attestation",
                    "trusted-verifier-count", "trusted-cross-vendor-review")

#: What counts as denying the phrase that follows it.
NEGATIONS = ("not", "never", "no", "neither", "nor")

#: What may sit between the negation and the phrase it denies: words, spaces
#: and markdown emphasis, and nothing else. An article ("not **a** trusted
#: review") has to be allowed; a comma, a full stop or a colon must NOT be,
#: because those are where one clause ends and a different claim begins.
_NEGATION_LINK = r"[\s\w*`_-]{0,24}?"

_NEGATED = {
    phrase: re.compile(r"\b(?:" + "|".join(NEGATIONS) + r")\b"
                       + _NEGATION_LINK + re.escape(phrase))
    for phrase in FORBIDDEN_CLAIMS
}


def assert_no_trusted_claim(text: str) -> str:
    """The summary must not describe itself as trusted or independent.

    A last check on the one artifact a human actually reads. Every other honesty
    control protects machine-readable records; this protects the sentence
    somebody skims before clicking merge.

    ## Why this is not a substring test any more

    It was, and it was wrong in both directions at once. The test was

        if phrase in lowered and "not " + phrase not in lowered: refuse

    which asks two questions badly.

    FALSE REFUSAL. `"not " + phrase` demands literal adjacency, and this
    module's own disclaimer reads "This is **not a** trusted review" — one
    article in between. So the sentence that makes the summary honest was the
    sentence that made the guard reject it, and `finalizecli` could never
    publish a summary at all. Nothing caught it because the closeout step had
    never once run with `proceed == true`: every earlier panel run stopped at
    preflight, so the only code path that calls this had never executed. Found
    when it finally did, in run 31441359197.

    FAIL-OPEN. The exemption was global. One negation anywhere in the document
    excused EVERY occurrence, so a summary could disclaim trust in its header
    and then assert it in its verdict line and pass. That is the direction that
    matters, and it is the reason this is not simply widened to `"not a "`.

    So each occurrence is judged on its own, and a negation counts only when it
    is close in front of the phrase with nothing but words and emphasis
    between. That is still a text heuristic and it is not a claim to understand
    English — it is a check that a specific denial sits immediately before a
    specific phrase. Sentence-ending punctuation deliberately breaks it."""
    lowered = text.lower()
    for phrase in FORBIDDEN_CLAIMS:
        negated_ends = {m.end() for m in _NEGATED[phrase].finditer(lowered)}
        index = lowered.find(phrase)
        while index != -1:
            if index + len(phrase) not in negated_ends:
                refuse(f"category=summary_claims_trust phrase={phrase!r} "
                       f"at_offset={index} — this panel is not write-separated "
                       "and must not describe itself as though it were. Every "
                       "occurrence is judged separately: a denial elsewhere in "
                       "the summary does not excuse this one")
            index = lowered.find(phrase, index + 1)
    return text
