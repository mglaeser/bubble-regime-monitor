"""Whether the evidence permits running at a given rollout stage.

The rollout stages exist so that delivery rules switch on only after a replay
has shown what they would have done. That protection is worth exactly as much
as the check that reads the evidence, and a check that treats *missing*
evidence as *satisfactory* protects nothing — the failure mode is silent, and
it looks identical to success.

So this module is fail-closed in all three directions:

  * evidence absent for the target stage -> BLOCKED (not "nothing to object to")
  * evidence present but failing         -> BLOCKED, quoting its own failures
  * evidence for a different ruleset     -> BLOCKED (it describes other rules)

It answers one question — "may the ruleset run at stage N?" — and answers it
from the committed artifact only. It never re-runs the replay, because a gate
that recomputes its own evidence can be made to agree with itself.
"""

from __future__ import annotations

from typing import Any

#: Stages at or below this need no replay evidence: no delivery rule is live,
#: so there is nothing a replay could tell us that the ruleset does not.
#: Mandate 13: delivery is gated at stage 3 and above.
EVIDENCE_REQUIRED_FROM_STAGE = 3


def promotion_blockers(*, target_stage: int, artifact: dict[str, Any],
                       rule_version: str | None = None,
                       phrase_set_version: str | None = None) -> list[str]:
    """Everything standing between the ruleset and running at `target_stage`.

    An empty list means promotion is permitted. Any non-empty list means it is
    not, and each entry is phrased so an operator can act on it without opening
    the artifact.
    """
    blockers: list[str] = []

    if target_stage < EVIDENCE_REQUIRED_FROM_STAGE:
        return blockers

    runs = artifact.get("runs")
    if not isinstance(runs, dict):
        return [f"stage {target_stage}: the gate artifact has no 'runs' section, "
                "so there is no evidence to judge"]

    # Evidence must describe the ruleset we are promoting. The artifact omits
    # the exact digests by design (they are gated by a separate CI step), so
    # this binds on the declared versions — weaker than a hash, and the reason
    # the digest step is not optional.
    declared = artifact.get("artifacts")
    if isinstance(declared, dict):
        if rule_version is not None and declared.get("rule_version") != rule_version:
            blockers.append(
                f"stage {target_stage}: the evidence was produced for rule version "
                f"{declared.get('rule_version')!r}, but {rule_version!r} is committed "
                "— it does not describe these rules")
        if phrase_set_version is not None \
                and declared.get("phrase_set_version") != phrase_set_version:
            blockers.append(
                f"stage {target_stage}: the evidence was produced for phrase set "
                f"{declared.get('phrase_set_version')!r}, but {phrase_set_version!r} "
                "is committed")

    key = f"stage_{target_stage}"
    run = runs.get(key)
    if not isinstance(run, dict):
        blockers.append(
            f"stage {target_stage}: no replay was recorded at this stage "
            f"(the artifact has {', '.join(sorted(runs)) or 'nothing'}). "
            "Absent evidence does not clear the gate.")
        return blockers

    if run.get("evaluated_at_stage") != target_stage:
        blockers.append(
            f"stage {target_stage}: the run filed under {key!r} was evaluated at "
            f"stage {run.get('evaluated_at_stage')!r}")

    failures = run.get("failures")
    failures = list(failures) if isinstance(failures, list) else []
    if failures:
        blockers.extend(f"stage {target_stage}: {failure}" for failure in failures)

    # `passed` is not trusted on its own: a verdict that disagrees with its own
    # failure list is itself the finding.
    if run.get("passed") is not True and not failures:
        blockers.append(
            f"stage {target_stage}: the replay did not pass and recorded no "
            "reason, which is a broken verdict rather than an empty one")
    elif run.get("passed") is True and failures:
        blockers.append(
            f"stage {target_stage}: the replay reports passed=true while listing "
            f"{len(failures)} failure(s) — the artifact contradicts itself")

    return blockers
