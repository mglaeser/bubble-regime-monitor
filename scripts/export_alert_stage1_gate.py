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

The history is SYNTHETIC and generated in-repo — see
`tests/fixtures/gen_alert_replay_history.py`. It contains no market data of
record and no personal data of any kind; it exists to exercise the state
machine, not to establish recall. Real recall is a Stage 2 question and needs
the operator-frozen mandatory-event catalogue.
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


def build_evidence() -> dict:
    from app.alerts.artifacts import validate_from_disk
    from app.alerts.dto import AlertInput
    from app.alerts.replay import ReplayConfig, run_replay

    artifacts = validate_from_disk(rules_path=RULES, phrase_path=PHRASES,
                                   service_version="3.8.0")
    payload = json.loads(HISTORY.read_text(encoding="utf-8"))
    inputs = [AlertInput.model_validate(record) for record in payload["inputs"]]

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
            runs[f"stage_{stage}"] = summary.as_dict()

    return {
        "artifact": "alert-stage1-gate",
        "gate": "deterministic replay; no PII; no scoring regression",
        "history": {
            "source": str(HISTORY.relative_to(ROOT)),
            "synthetic": True,
            "inputs": len(inputs),
            "sha256": payload["sha256_of_inputs"],
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
