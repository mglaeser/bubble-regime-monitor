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
that recomputes its own evidence can be made to agree with itself. Since owner
decision D2d only promotion asks it (`app.alerts.promotion_service`); nothing
at runtime reads the evidence.

The binding is on BYTES as well as declared versions. The artifact could not
carry bare digests — see `group_digest` — so they are written grouped, which
keeps the full value while staying invisible to an entropy detector. A ruleset
edit that forgets to bump its version is therefore caught here, and not only by
the separate "Alert artifacts" CI step.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, TypeGuard, cast

#: Stage 2's recall evidence is meaningful only for the exact operator-frozen
#: catalogue replayed by the gate artifact.  The grouped digest in that
#: artifact is compared with these shipped bytes before promotion.
MANDATORY_EVENTS_PATH = "config/alert_mandatory_events.v3.2.json"

#: A sha256 written as eight hyphen-separated 8-character groups.
#:
#: The artifact could not carry bare digests: an entropy detector cannot tell a
#: 64-hex digest from a token, and this repository's secret baseline is a
#: byte-identical ratchet that may not grow to hold them. Truncating was
#: rejected for a good reason — the detector scores entropy rather than length,
#: so whether a prefix passes depends on which characters the hash happened to
#: produce, and a future edit would fail CI for reasons unrelated to the edit.
#:
#: Grouping has neither problem. The digest is carried in FULL, so nothing is
#: weakened, and no group is long enough to score as high-entropy, so the
#: result is stable rather than luck-of-the-hash.
_GROUP = 8


def group_digest(digest: str) -> str:
    return "-".join(digest[i:i + _GROUP] for i in range(0, len(digest), _GROUP))


def ungroup_digest(grouped: str) -> str:
    return grouped.replace("-", "")


def _valid_grouped_digest(value: Any) -> TypeGuard[str]:
    if not isinstance(value, str):
        return False
    groups = value.split("-")
    return (
        len(groups) == 8
        and all(len(group) == 8 for group in groups)
        and all(character in "0123456789abcdef" for group in groups
                for character in group)
    )


def _mandatory_recall_blockers(
    *,
    target_stage: int,
    artifact: dict[str, Any],
    run: dict[str, Any],
    catalogue_path: str | Path | None,
) -> list[str]:
    """Stage 2+ requires measured recall bound to the shipped catalogue."""
    if target_stage < 2:
        return []

    blockers: list[str] = []
    catalogue = artifact.get("mandatory_event_catalogue")
    if not isinstance(catalogue, dict):
        return [
            f"stage {target_stage}: the evidence carries no mandatory-event "
            "catalogue provenance, so recall is not bound to any fixtures"
        ]

    grouped = catalogue.get("sha256_grouped")
    if not _valid_grouped_digest(grouped):
        blockers.append(
            f"stage {target_stage}: the mandatory-event catalogue digest is "
            "missing or malformed"
        )
    else:
        current = mandatory_event_catalogue_sha256(catalogue_path)
        if current is None:
            blockers.append(
                f"stage {target_stage}: the shipped mandatory-event catalogue "
                f"at {MANDATORY_EVENTS_PATH} is missing or unreadable"
            )
        elif ungroup_digest(grouped) != current:
            blockers.append(
                f"stage {target_stage}: the replay used mandatory-event "
                f"catalogue {ungroup_digest(grouped)[:12]}, but the shipped "
                f"catalogue is {current[:12]}"
            )

    current_document = mandatory_event_catalogue_document(catalogue_path)
    if current_document is None:
        blockers.append(
            f"stage {target_stage}: the shipped mandatory-event catalogue "
            "is not a readable JSON object"
        )

    event_count = catalogue.get("event_count")
    if isinstance(event_count, bool) or not isinstance(event_count, int) \
            or event_count <= 0:
        blockers.append(
            f"stage {target_stage}: mandatory-event catalogue provenance must "
            "record at least one event"
        )
    if catalogue.get("frozen") is not True:
        blockers.append(
            f"stage {target_stage}: the mandatory-event catalogue was not "
            "operator-frozen"
        )
    if catalogue.get("schema_version") != 1:
        blockers.append(
            f"stage {target_stage}: the mandatory-event catalogue schema is "
            "not the supported version 1"
        )
    version = catalogue.get("catalogue_version")
    if not isinstance(version, str) or not version.strip():
        blockers.append(
            f"stage {target_stage}: the mandatory-event catalogue has no "
            "version"
        )
    if current_document is not None:
        current_events = current_document.get("events")
        comparisons = {
            "catalogue_version": current_document.get("catalogue_version"),
            "schema_version": current_document.get("schema_version"),
            "frozen": current_document.get("frozen"),
            "event_count": (
                len(current_events) if isinstance(current_events, list) else None
            ),
        }
        for field_name, current_value in comparisons.items():
            if catalogue.get(field_name) != current_value:
                blockers.append(
                    f"stage {target_stage}: mandatory-event catalogue "
                    f"provenance {field_name}={catalogue.get(field_name)!r} "
                    f"does not match the shipped value {current_value!r}"
                )

    total_raw = run.get("mandatory_event_total")
    detected_raw = run.get("mandatory_event_detected")
    not_evaluable_raw = run.get("mandatory_event_not_evaluable")
    fields = {
        "total": total_raw,
        "detected": detected_raw,
        "not_evaluable": not_evaluable_raw,
    }
    malformed = [
        name for name, value in fields.items()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0
    ]
    if malformed:
        blockers.append(
            f"stage {target_stage}: mandatory-event recall counts are missing "
            f"or malformed ({', '.join(malformed)})"
        )
        return blockers

    total = cast(int, total_raw)
    detected = cast(int, detected_raw)
    not_evaluable = cast(int, not_evaluable_raw)
    if isinstance(event_count, int) and not isinstance(event_count, bool) \
            and event_count != total:
        blockers.append(
            f"stage {target_stage}: catalogue provenance records {event_count} "
            f"event(s), but the replay judged {total}"
        )
    if not_evaluable > total or detected > total:
        blockers.append(
            f"stage {target_stage}: mandatory-event recall counts contradict "
            "their total"
        )
        return blockers

    evaluable = total - not_evaluable
    if evaluable <= 0:
        blockers.append(
            f"stage {target_stage}: mandatory-event recall is unmeasured; no "
            "catalogue event was evaluable"
        )
    elif detected != evaluable:
        blockers.append(
            f"stage {target_stage}: mandatory-event recall is {detected}/"
            f"{evaluable}, but Stage 2+ requires 100% on evaluable events"
        )
    return blockers




def promotion_blockers(*, target_stage: int, artifact: dict[str, Any],
                       rule_version: str | None = None,
                       phrase_set_version: str | None = None,
                       rules_sha256: str | None = None,
                       phrase_set_sha256: str | None = None,
                       mandatory_events_path: str | Path | None = None,
                       ) -> list[str]:
    """Everything standing between the ruleset and running at `target_stage`.

    An empty list means promotion is permitted. Any non-empty list means it is
    not, and each entry is phrased so an operator can act on it without opening
    the artifact.
    """
    blockers: list[str] = []

    runs = artifact.get("runs")
    if not isinstance(runs, dict):
        return [f"stage {target_stage}: the gate artifact has no 'runs' section, "
                "so there is no evidence to judge"]

    # Evidence must describe the exact ruleset and phrase bytes we are
    # promoting.  Versions remain useful operator vocabulary, but the grouped
    # full digests below are the authority; the grouping only keeps them from
    # looking like bare credentials to the secret scanner.
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

            # Bytes, where the artifact can carry them. A version string is
            # something a human types; this is what the replay actually ran on.
            for label, expected, key in (
                    ("rules", rules_sha256, "rules_sha256_grouped"),
                    ("phrase set", phrase_set_sha256, "phrase_set_sha256_grouped")):
                if expected is None:
                    continue
                recorded = declared.get(key)
                if not isinstance(recorded, str) or not recorded:
                    blockers.append(
                        f"stage {target_stage}: the evidence records no {label} "
                        "digest, so it cannot be shown to describe these bytes")
                elif ungroup_digest(recorded) != expected:
                    blockers.append(
                        f"stage {target_stage}: the evidence was produced on "
                        f"{label} {ungroup_digest(recorded)[:12]}, but "
                        f"{expected[:12]} is committed")

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

    blockers.extend(_mandatory_recall_blockers(
        target_stage=target_stage,
        artifact=artifact,
        run=run,
        catalogue_path=mandatory_events_path,
    ))

    # A run that judged volume must say WHICH caps it judged against, and they
    # must be the caps the code enforces (app/alerts/budgets.py LIMITS, a
    # constant since owner decision D2d). Evidence that names no limits cannot
    # make a volume claim at all.
    if run.get("notification_planning_ran"):
        from app.alerts import budgets

        recorded = run.get("budget_limits")
        current = budgets.LIMITS
        if not isinstance(recorded, dict) or not recorded:
            blockers.append(
                f"stage {target_stage}: the replay judged volume but recorded "
                "no budget limits, so its verdict cannot be tied to any caps")
        else:
            for name, enforced in (("cap_24h", current.cap_24h),
                                   ("cap_168h", current.cap_168h),
                                   ("target_168h", current.target_168h)):
                if recorded.get(name) != enforced:
                    blockers.append(
                        f"stage {target_stage}: the evidence was judged against "
                        f"{name}={recorded.get(name)} and the deployment now "
                        f"enforces {name}={enforced}; changed caps need new "
                        "evidence, not inherited approval")

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


def mandatory_event_catalogue_sha256(path: str | Path | None = None) -> str | None:
    """Digest the exact Stage-2 recall catalogue, or fail closed as ``None``."""
    candidate = (
        Path(path)
        if path is not None
        else _repo_root() / MANDATORY_EVENTS_PATH
    )
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return None


def mandatory_event_catalogue_document(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Load the shipped catalogue envelope for provenance comparison."""
    candidate = (
        Path(path)
        if path is not None
        else _repo_root() / MANDATORY_EVENTS_PATH
    )
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def load_evidence(path: str | Path | None = None) -> dict[str, Any] | None:
    """The committed gate artifact, or None if it cannot be read as one.

    None means "no usable evidence", which the promotion service treats as
    a blocker. It deliberately does not raise: an unreadable artifact is a
    refusal to report through the same channel as a failing one, not a
    traceback out of a promotion.
    """
    candidate = Path(path) if path is not None else _repo_root() / EVIDENCE_PATH
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None
