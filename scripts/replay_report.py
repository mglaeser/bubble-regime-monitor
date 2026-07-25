#!/usr/bin/env python3
"""RM-4/RM-5 CLI — policy replay studies over the persisted snapshot history.

Runs against the configured DB (DB_URL / default /data/bubble.db) and prints
ONE JSON document. Read-only; no scored value is touched.

Usage:
  python scripts/replay_report.py sufficiency   RM-2 S5 gate tracker
  python scripts/replay_report.py b             RM-4 B0-B5 coverage policies
  python scripts/replay_report.py s5            RM-4 positional-vs-calendar S5
  python scripts/replay_report.py evidence      RM-1 stamp/outcome summary
  python scripts/replay_report.py assemble      RM-5 per-PIN decision packages
"""

from __future__ import annotations

import json
import sys

from app.services import replay

STUDIES = {
    "sufficiency": replay.s5_tier_sufficiency,
    "b": replay.b_policy_report,
    "s5": replay.s5_dual_report,
    "evidence": replay.evidence_summary,
    "assemble": replay.assemble_decision_packages,
}


def main() -> int:
    study = sys.argv[1] if len(sys.argv) > 1 else ""
    if study not in STUDIES:
        print(f"usage: replay_report.py {{{'|'.join(STUDIES)}}}", file=sys.stderr)
        return 2
    print(json.dumps(STUDIES[study](), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
