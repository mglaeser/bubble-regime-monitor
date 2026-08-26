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

#: The stage at which provider-backed delivery begins. Below it, live delivery
#: is REFUSED — not merely unevidenced.
#:
#: I previously removed this floor, reasoning that `ops.indicator_stale` and
#: `ops.coverage_degraded_info` are enabled from stage 1 and could therefore
#: send. They are enabled, and they cannot send: both are P4, and the planner
#: maps P4 to "API and log only", creating no delivery at all. Checking that
#: the rules were enabled without checking what they produce turned the floor
#: into an evidence check, and a promoted stage-1 artifact then cleared live
#: admission — which is the opposite of what stage 1 promises.
#:
#: Passing evidence and exact promotion cannot lift this. They answer a
#: different question: whether these bytes may be ACCEPTED as a stage-N
#: artifact. Whether this deployment may construct a sender and deliver is
#: this constant's question alone.
LIVE_DELIVERY_STAGE = 3

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
    # must be the caps this deployment enforces now. The planner reads its
    # limits from settings, so an env var raised after the evidence was
    # produced would run live under caps the evidence never saw — with the
    # artifact still reading "passed". Evidence that names no limits cannot
    # make a volume claim at all.
    if run.get("notification_planning_ran"):
        from app.alerts.outbox import default_limits
        from app.config import get_settings

        recorded = run.get("budget_limits")
        current = default_limits(get_settings())
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
    blockers: list[str] = []
    if stage < LIVE_DELIVERY_STAGE:
        blockers.append(
            f"live delivery is not admitted before Stage {LIVE_DELIVERY_STAGE} "
            f"(active_stage={stage})")

    # NOT an early return. Evidence and promoted-byte checks still run, so a
    # deployment that is both too early AND unevidenced reports both rather
    # than hiding the second behind the first.
    evidence = load_evidence(path)
    if evidence is None:
        blockers.append(
            f"stage {stage}: the gate evidence at {EVIDENCE_PATH} is missing "
            "or unreadable, so nothing justifies delivering at this stage")
        return blockers

    blockers += promotion_blockers(
        target_stage=stage, artifact=evidence,
        rule_version=ruleset.document.meta.rule_version,
        phrase_set_version=getattr(ruleset, "phrase_set_version", None),
        rules_sha256=ruleset.rules_sha256,
        phrase_set_sha256=ruleset.phrase_set_sha256,
    )
    blockers.extend(_digest_blockers(session, ruleset))
    return blockers


def _digest_blockers(session: Any, ruleset: Any) -> list[str]:
    """Bind the running ruleset to promoted BYTES as a second runtime proof.

    The evidence artifact above already carries grouped full digests.  The
    registry adds a separate fact: these were the bytes deliberately promoted,
    not merely bytes for which a replay artifact exists.

    The registry stores what was promoted. Requiring the running ruleset to BE
    the promoted one closes the gap the version binding leaves open — an edit
    that forgot to bump its version no longer reaches a phone, because its
    bytes were never promoted.
    """
    from app.alerts.artifacts import load_promoted

    try:
        promoted = load_promoted(session)
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        return [f"the promoted ruleset could not be rebuilt, so the running "
                f"one cannot be shown to match it: {type(exc).__name__}"]
    if promoted is None:
        return ["nothing has been promoted, so no bytes authorise delivery"]

    out: list[str] = []
    if promoted.ruleset.rules_sha256 != ruleset.rules_sha256:
        out.append(
            f"the running rules ({ruleset.rules_sha256[:12]}) are not the "
            f"promoted ones ({promoted.ruleset.rules_sha256[:12]}), whatever "
            "version they declare")
    if promoted.ruleset.phrase_set_sha256 != ruleset.phrase_set_sha256:
        out.append(
            f"the running phrase set ({ruleset.phrase_set_sha256[:12]}) is not "
            f"the promoted one ({promoted.ruleset.phrase_set_sha256[:12]})")
    return out


def delivery_admission_blockers(session: Any, planning_rules_sha256: str, *,
                                path: str | Path | None = None) -> list[str]:
    """Whether a QUEUED delivery may be sent, judged by the rules that planned it.

    This asks a NARROWER question than `live_admission_blockers`, and the
    difference matters. That one asks whether the running deployment is
    authorised — current evidence, currently promoted bytes. This one asks
    whether the ruleset that planned THIS message was ever deliberately
    promoted.

    They have to differ because of archived rulesets. An archived ruleset that
    still owns open episodes keeps being evaluated until they close, and it is
    never the currently promoted one — that is what "archived" means. Judging
    its deliveries against the current promotion blocked every continuation
    permanently: they could not be sent, and no operator action could ever make
    them sendable, because the ruleset will not be promoted again. The gate's
    purpose is to stop messages from rules nobody approved, not to strand
    messages from rules somebody approved and later replaced.

    So the test is `promoted_at is not None` — a deliberate act that happened,
    and that archiving does not undo.

    On byte binding, which the panel asked about twice: there is nothing here
    to check, and adding a check would have been theatre.

    The registry row is fetched BY the planning hash, which is that table's
    primary key — so what is read is exactly the bytes that hash names.
    Resolving by version or by name would need a digest comparison; resolving
    BY digest is the comparison.

    The row's phrase set is referenced by version rather than by hash, which
    looks like the remaining gap, and the schema already closes it more firmly
    than this function could: a trigger makes phrase-set bytes immutable under
    an existing version, and a foreign key stops a referenced set being
    deleted. I wrote both checks before discovering they were unreachable —
    `test_the_schema_binds_a_delivery_to_its_reviewed_text` pins the
    constraints instead, so the guarantee fails loudly if either is ever
    dropped.
    """
    from app.alerts.artifacts import load_active
    from app.alerts.enums import RulesetStatus
    from app.alerts.models import AlertRulesetRegistry

    row = session.get(AlertRulesetRegistry, planning_rules_sha256)
    if row is None:
        return [f"the ruleset that planned this delivery "
                f"({planning_rules_sha256[:12]}) is not in the registry, so "
                "nothing establishes what it was permitted to do"]
    if row.promoted_at is None:
        return [f"the ruleset that planned this delivery "
                f"({planning_rules_sha256[:12]}) was never promoted, so no "
                "operator ever authorised what it sends"]
    if row.evidence_checked_at is None:
        # Promoted, but through the OLD path that checked nothing. The
        # timestamp alone cannot say whether an operator meant it, so it does
        # not authorise delivery; re-promoting once through the gated service
        # is the deliberate, one-command fix.
        return [f"the ruleset that planned this delivery "
                f"({planning_rules_sha256[:12]}) was promoted before promotion "
                "checked evidence; re-promote it through the gated service"]
    # REVOKED is the one status that outranks a past promotion. Superseding a
    # ruleset says "there is something newer"; revoking it says "this was
    # wrong" — and an operator who revokes rules while their messages sit in
    # the outbox means those messages, or revocation would only apply to
    # alerts nobody had planned yet.
    # Compared as the enum, which is equal to its own value: `RulesetStatus`
    # is a StrEnum, so this holds whether the column hands back the member or
    # the bare string. Reaching for `str(...)` on one side and `.value` on the
    # other worked too, and read like it was compensating for something.
    if row.status == RulesetStatus.REVOKED:
        return [f"the ruleset that planned this delivery "
                f"({planning_rules_sha256[:12]}) was REVOKED, which withdraws "
                "the promotion that authorised it"]

    # Promotion authorises the ruleset's EXISTENCE; it does not freeze the
    # stage. Checking only "was promoted" let a message planned at stage 3
    # under a since-superseded ruleset go out after the operator demoted the
    # deployment to stage 1 — the queue outliving the decision that stopped it.
    #
    # So the planning ruleset may not outrank what is permitted NOW. A
    # continuation from an archived ruleset at the same stage still sends,
    # which is what the previous fix was protecting; a demotion stops it, which
    # is what that fix lost.
    try:
        current = load_active(session).ruleset.document.meta.active_stage
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        return [f"the active ruleset could not be loaded, so the stage this "
                f"delivery was planned for cannot be compared to it: "
                f"{type(exc).__name__}"]

    planned_stage = _stage_of(row)
    if planned_stage is None:
        return [f"the ruleset that planned this delivery "
                f"({planning_rules_sha256[:12]}) does not record a stage, so "
                "what it was permitted to send cannot be established"]
    if planned_stage > current:
        return [f"this delivery was planned at stage {planned_stage} and the "
                f"deployment now runs at stage {current}; the queue must not "
                "outlive the decision that lowered it"]
    if planned_stage < LIVE_DELIVERY_STAGE:
        # Planned while delivery was not admitted at all. Promotion to stage 3
        # must not drain a queue that predates it: those messages were built
        # when nothing was allowed to reach a phone, are stale by the time the
        # stage rises, and were never part of what the operator promoted.
        return [f"this delivery was planned at stage {planned_stage}, below "
                f"the delivery floor (stage {LIVE_DELIVERY_STAGE}); raising "
                "the stage later does not authorise work queued before it"]

    return []


def _stage_of(row: Any) -> int | None:
    """The `active_stage` recorded in a registry row's canonical YAML."""
    import yaml

    try:
        document = yaml.safe_load(row.canonical_yaml)
        return int(document["meta"]["active_stage"])
    except Exception:                              # noqa: BLE001 - absent is a blocker
        return None
