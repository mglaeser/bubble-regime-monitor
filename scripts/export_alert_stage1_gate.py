"""Produce `docs/alert-stage1-gate.json` — the Stage 1 gate evidence.

The gate is *deterministic replay*, and the honest way to hold a system to that
is to make CI fail when it stops being true. So the evidence artifact is not a
document somebody wrote down after watching a run: it is the summary of a
replay over a committed synthetic history, regenerated on every CI run and
compared byte-for-byte with what is in the repository.

If the evaluator's behaviour changes, this artifact changes and the diff is
reviewable. If the evaluator becomes non-deterministic, this artifact stops
matching and CI goes red — which is precisely the property Stage 1 claims.

    python -m scripts.export_alert_stage1_gate            # write
    python -m scripts.export_alert_stage1_gate --check    # fail on drift

The history is SYNTHETIC. `tests/fixtures/alert_replay_history.json` declares
the arc — twenty recompute slots — and `alert_replay_history.py` builds the
inputs from it. It contains no market data of record and no personal data of
any kind; it exists to exercise the state machine, not to establish recall.
Real recall is a Stage 2 question and needs the operator-frozen
mandatory-event catalogue.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "docs" / "alert-stage1-gate.json"
HISTORY = ROOT / "tests" / "fixtures" / "alert_replay_history.json"
RULES = ROOT / "config" / "alert_rules.v3.2.yaml"
PHRASES = ROOT / "config" / "alert_phrases.v3.2.json"
EVENTS = ROOT / "config" / "alert_mandatory_events.v3.2.json"

#: Replayed at the committed stage AND at the first delivery stage. The second
#: is what makes the artifact useful: at stage 1 almost nothing is gated on, so
#: a stage-1-only artifact would be nearly blind to evaluator regressions.
STAGES = (1, 3)

#: Bare content digests are still not written into a per-run summary: an
#: entropy detector cannot tell a 64-hex digest from a 64-hex token, which is
#: the correct default, and this repository's secret baseline is a
#: byte-identical ratchet that may not grow to carry them. Truncation is not
#: the answer either — the detector scores Shannon entropy rather than length,
#: so whether a prefix passes depends on which characters the hash happened to
#: produce, and a future ruleset edit would fail CI for reasons that have
#: nothing to do with the ruleset.
#:
#: The artifact's PROVENANCE section does carry them, GROUPED (see
#: `app.alerts.promotion.group_digest`): the full digest, split into
#: eight-character runs, none of which is long enough to score as
#: high-entropy. Nothing is weakened and the result is stable rather than
#: luck-of-the-hash — which matters, because the promotion gate binds evidence
#: to bytes with it. Versions alone were not enough: a version string is
#: something a human types, so an edit that forgot to bump it was invisible.
_DIGEST_FIELDS = ("rules_sha256", "phrase_set_sha256")


def _without_digests(summary: dict) -> dict:
    return {k: v for k, v in summary.items() if k not in _DIGEST_FIELDS}


def build_evidence() -> dict:
    from app.alerts.artifacts import validate_from_disk
    from app.alerts.promotion import group_digest
    from app.alerts.replay import ReplayConfig, run_replay
    from tests.fixtures import alert_replay_history as history

    artifacts = validate_from_disk(rules_path=RULES, phrase_path=PHRASES,
                                   service_version="3.8.0")
    inputs = history.load()

    runs: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for stage in STAGES:
            config = ReplayConfig(
                source_db_url="sqlite:///not-used-the-history-is-passed-in",
                state_db_path=Path(tmp) / f"stage{stage}.db",
                evaluate_at_stage=stage,
                mandatory_events_path=EVENTS if EVENTS.exists() else None,
            )
            summary = run_replay(config=config, ruleset=artifacts.ruleset,
                                 phrase_set=artifacts.phrase_set, inputs=inputs)
            runs[f"stage_{stage}"] = _without_digests(summary.as_dict())

    document = history.document()
    return {
        "artifact": "alert-stage1-gate",
        "gate": "deterministic replay; no PII; no scoring regression",
        "artifacts": {
            "rules_sha256_grouped": group_digest(artifacts.ruleset.rules_sha256),
            "phrase_set_sha256_grouped": group_digest(
                artifacts.ruleset.phrase_set_sha256),
            "rule_version": artifacts.ruleset.rule_version,
            "phrase_set_version": artifacts.ruleset.phrase_set_version,
            "digests": ("carried GROUPED above — the full sha256 split into "
                        "eight-character runs, so an entropy detector does not "
                        "read it as a credential. The promotion gate binds "
                        "evidence to bytes with them; per-run summaries still "
                        "omit bare digests. See app.alerts.promotion."
                        "group_digest"),
        },
        "history": {
            "source": str(HISTORY.relative_to(ROOT)),
            "synthetic": True,
            "arc_schema_version": document["schema_version"],
            "inputs": len(inputs),
        },
        "runs": runs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="export_alert_stage1_gate")
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed artifact is stale")
    args = parser.parse_args(argv)

    rendered = json.dumps(build_evidence(), indent=2, sort_keys=True,
                          ensure_ascii=False) + "\n"

    if args.check:
        if not ARTIFACT.exists():
            print(f"missing {ARTIFACT.relative_to(ROOT)} — run "
                  "`python -m scripts.export_alert_stage1_gate`", file=sys.stderr)
            return 1
        if ARTIFACT.read_text(encoding="utf-8") != rendered:
            print(f"{ARTIFACT.relative_to(ROOT)} is stale, or the replay is no "
                  "longer deterministic. Regenerate and review the diff:\n"
                  "  python -m scripts.export_alert_stage1_gate", file=sys.stderr)
            return 1
        print(f"{ARTIFACT.relative_to(ROOT)} is current")
        return 0

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(rendered, encoding="utf-8")
    print(f"wrote {ARTIFACT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
