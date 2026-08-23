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

One limitation, stated plainly so it is not mistaken for something stronger:
the binding is on DECLARED VERSIONS, not on content digests. The artifact
deliberately omits digests — an entropy detector cannot distinguish a 64-hex
digest from a 64-hex token, and this repository's secret baseline is a
byte-identical ratchet that may not grow to carry them (see
`scripts/export_alert_stage1_gate.py`). So a ruleset edit that fails to bump
its version is NOT caught here. It is caught by the separate "Alert artifacts"
CI step, which is why that step is not optional, and by the episode counts in
the artifact itself moving.
"""

from __future__ import annotations

import json
from pathlib import Path
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
    #
    # A MISSING provenance section does not excuse the check. Skipping the
    # binding when the artifact carries no `artifacts` object would mean an
    # artifact that says nothing about which ruleset it describes clears the
    # very gate that exists to establish it — the same fail-open shape this
    # module was written to remove, hidden one level down.
    if rule_version is not None or phrase_set_version is not None:
        declared = artifact.get("artifacts")
        if not isinstance(declared, dict):
            blockers.append(
                f"stage {target_stage}: the evidence carries no provenance "
                "section, so there is nothing to show it describes this "
                "ruleset rather than some other one")
        else:
            if rule_version is not None \
                    and declared.get("rule_version") != rule_version:
                blockers.append(
                    f"stage {target_stage}: the evidence was produced for rule "
                    f"version {declared.get('rule_version')!r}, but "
                    f"{rule_version!r} is committed — it does not describe "
                    "these rules")
            if phrase_set_version is not None \
                    and declared.get("phrase_set_version") != phrase_set_version:
                blockers.append(
                    f"stage {target_stage}: the evidence was produced for phrase "
                    f"set {declared.get('phrase_set_version')!r}, but "
                    f"{phrase_set_version!r} is committed")

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
    if not isinstance(failures, list):
        # Coercing this to [] would let a run whose failure list is a string,
        # an object, or absent report as having nothing wrong with it. A
        # verdict we cannot read is not a verdict that passed.
        blockers.append(
            f"stage {target_stage}: the run's failure list is not a list "
            f"({type(failures).__name__}), so its verdict cannot be read")
        return blockers
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


#: Where the committed evidence lives. It ships in the image (`COPY . .`), so
#: a running container can consult the same file CI does.
EVIDENCE_PATH = "docs/alert-stage1-gate.json"


def _repo_root() -> Path:
    """This file is app/alerts/promotion.py, so the root is three parents up."""
    return Path(__file__).resolve().parent.parent.parent


def load_evidence(path: str | Path | None = None) -> dict[str, Any] | None:
    """The committed gate artifact, or None if it cannot be read as one.

    None means "no usable evidence", which every caller must treat as a
    blocker. It deliberately does not raise: an unreadable artifact is a
    condition to report through the same channel as a failing one, not a
    traceback out of the dispatch loop.
    """
    candidate = Path(path) if path is not None else _repo_root() / EVIDENCE_PATH
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def live_admission_blockers(session: Any, *, path: str | Path | None = None,
                            ) -> list[str]:
    """Whether the ACTIVE ruleset may deliver at the stage it claims.

    This is the runtime half of the gate, and the reason the module is not just
    a test helper: a check that only CI performs protects the repository, not
    the operator. A container started from an image whose evidence does not
    support its own `active_stage` must not send.

    Fail-closed throughout — including on an unreadable artifact, and including
    when the active ruleset cannot be loaded at all.
    """
    from app.alerts.artifacts import load_active

    try:
        ruleset = load_active(session).ruleset
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        return [f"the active ruleset could not be loaded, so its stage cannot "
                f"be justified: {type(exc).__name__}"]

    stage = ruleset.document.meta.active_stage
    if stage < EVIDENCE_REQUIRED_FROM_STAGE:
        return []

    evidence = load_evidence(path)
    if evidence is None:
        return [f"stage {stage}: the gate evidence at {EVIDENCE_PATH} is missing "
                "or unreadable, so nothing justifies delivering at this stage"]

    return promotion_blockers(
        target_stage=stage, artifact=evidence,
        rule_version=ruleset.document.meta.rule_version,
        phrase_set_version=getattr(ruleset, "phrase_set_version", None),
    )


def delivery_admission_blockers(session: Any, planning_rules_sha256: str, *,
                                path: str | Path | None = None) -> list[str]:
    """Whether a QUEUED delivery may be sent, judged by the rules that planned it.

    `live_admission_blockers` asks about the ruleset that is active now. That
    is not the same question. A delivery sits in the outbox carrying the hash
    of the ruleset that planned it, and a promotion between planning and
    dispatch means the active ruleset is no longer the one whose stage
    authorised this message. Checking only the active one lets a delivery
    planned under an unbacked stage go out because something else is fine now.

    Fail-closed: a planning ruleset that cannot be rebuilt from the registry
    is a blocker, since nothing then establishes what it was allowed to do.
    """
    from app.alerts.artifacts import load_by_hash

    try:
        ruleset = load_by_hash(session, planning_rules_sha256).ruleset
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        return [f"the ruleset that planned this delivery "
                f"({planning_rules_sha256[:12]}) could not be rebuilt, so what "
                f"it was permitted to do cannot be established: "
                f"{type(exc).__name__}"]

    stage = ruleset.document.meta.active_stage
    if stage < EVIDENCE_REQUIRED_FROM_STAGE:
        return []

    evidence = load_evidence(path)
    if evidence is None:
        return [f"stage {stage}: the gate evidence at {EVIDENCE_PATH} is missing "
                "or unreadable, so nothing justifies sending this delivery"]

    return promotion_blockers(
        target_stage=stage, artifact=evidence,
        rule_version=ruleset.document.meta.rule_version,
        phrase_set_version=getattr(ruleset, "phrase_set_version", None),
    )
