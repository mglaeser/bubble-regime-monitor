#!/usr/bin/env python3
"""The findings gate — mandate Level 2, the control that makes `audit/` executable.

`audit/03-findings.json` is a 119-record ledger. Left as prose it decays into a
document nobody reads; this turns it into a state machine CI can fail on.

Three modes, deliberately separate:

  (default)     report the tally, exit 0 — the repair lane. Honest while the
                findings are still being worked: a gate that can never pass is
                a gate someone deletes under pressure.
  --admission   exit non-zero while production_eligible is false. This is the
                deploy gate. Fail-CLOSED: any error computing the answer is a
                refusal, never an admission.
  --strict      exit non-zero while any STOP-SHIP or BLOCKER is open, or any
                PASS record carries no standing control. The end state.

THE ONE RULE THE MANDATE STATES ABSOLUTELY: `production_eligible` may only ever
be COMPUTED. It was previously a hard-coded literal in two files, both now
deleted. There is no argument, environment variable or flag in this file that
can assert it — the only way to make it true is to close the findings.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FINDINGS_PATH = ROOT / "audit" / "03-findings.json"

# Ordered worst-first. `band` and `escalated_band` carry free text in the
# ledger — e.g. "STOP-SHIP (A-01+A-39 both fail)" — so a bare equality test
# silently scores an escalated STOP-SHIP as unrecognised, which would read as
# harmless. Matching is on the leading token.
BANDS = ("STOP-SHIP", "BLOCKER-1", "BLOCKER-2", "MUST-FIX", "SHOULD-FIX", "PLAN", "ASSESS")

# A record is OPEN unless it passed or does not apply. PARTIAL is open: the
# mandate treats a partially satisfied check as unsatisfied.
OPEN_VERDICTS = ("FAIL", "PARTIAL")

# The ONLY two verdicts that take a record out of the open count. Anything else
# — including a typo — is open. See is_open for why this is a tuple of its own
# rather than "not in OPEN_VERDICTS".
CLOSED_VERDICTS = ("PASS", "NOT-APPLICABLE")

# Bands that block production. BLOCKER-2 deliberately does not: the mandate
# bands it below the production bar, and folding it in here would mean the gate
# never opens, which is its own failure mode.
BLOCKING_BANDS = ("STOP-SHIP", "BLOCKER-1")


def normalize_band(raw: Any) -> str | None:
    """The band's leading token, or None if it names no known band.

    None is NOT "no problem" — every caller treats an unrecognised band as a
    fault, because a typo in the ledger must not silently shrink the count."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().upper()
    for band in BANDS:
        if re.match(rf"^{re.escape(band)}\b", text):
            return band
    return None


def effective_band(record: dict) -> str | None:
    """The band that counts: an escalation overrides the base band.

    A-01 sits at BLOCKER-1 and A-39 at PLAN, but both carry an escalated_band
    of STOP-SHIP because §3 escalates them jointly when both fail. Reading only
    `band` would report zero STOP-SHIP findings while two are open."""
    return normalize_band(record.get("escalated_band")) or normalize_band(record.get("band"))


def normalize_verdict(raw: Any) -> str | None:
    """The verdict as a known token, or None if it names no known verdict.

    None is NOT "no problem" — the same contract normalize_band carries, for
    the same reason. A ledger entry whose verdict cannot be read is a check
    whose state is unknown."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().upper()
    return text if text in OPEN_VERDICTS + CLOSED_VERDICTS else None


def is_open(record: dict) -> bool:
    """Open unless the record explicitly PASSes or does not apply.

    Fail-closed, and this is the whole point of the function. The previous form
    asked whether the verdict was IN OPEN_VERDICTS, so an unrecognised verdict
    fell out of the tally entirely: not counted open, not counted PASS, not
    counted NOT-APPLICABLE, not counted anywhere. Rewriting the ledger's 80
    open verdicts to plausible synonyms ("FAILED", "PARTIALLY") therefore
    reported `open findings 0`, computed production_eligible TRUE and granted
    deploy admission.

    normalize_band already refuses to let a typo shrink the count. This is the
    same defence on the sibling field, which is where it was missing."""
    return normalize_verdict(record.get("verdict")) not in CLOSED_VERDICTS


def load_findings(path: Path = FINDINGS_PATH) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must hold a JSON list of records, found {type(data).__name__}")
    return data


def tally(records: list[dict]) -> dict[str, Any]:
    """Counts, plus the two integrity problems the mandate calls out by name.

    unbanded: a record whose band matches nothing known. Counted and reported
    rather than ignored, because an unreadable band is an uncounted finding.

    unrecognised_verdict: the same fault on the verdict field. Such a record is
    counted OPEN (is_open fails closed) and listed here as well, so a ledger
    typo can never be mistaken for a genuine FAIL — nor hidden among them.

    pass_without_control: §9.10.1 — a PASS with no standing control is a claim,
    not a control, and the mandate downgrades it to PARTIAL. All 8 PASS records
    currently lack one, which is why --strict stays red until they are written."""
    counts = dict.fromkeys(BANDS, 0)
    unbanded: list[str] = []
    unrecognised: list[str] = []
    for r in records:
        if normalize_verdict(r.get("verdict")) is None:
            unrecognised.append(str(r.get("id", "?")))
        if not is_open(r):
            continue
        band = effective_band(r)
        if band is None:
            unbanded.append(str(r.get("id", "?")))
        else:
            counts[band] += 1
    passes = [r for r in records if normalize_verdict(r.get("verdict")) == "PASS"]
    not_applicable = [r for r in records
                      if normalize_verdict(r.get("verdict")) == "NOT-APPLICABLE"]
    return {
        "registered_check_count": len(records),
        "open_by_band": counts,
        "open_total": sum(counts.values()) + len(unbanded),
        "unbanded_open": unbanded,
        "unrecognised_verdict": unrecognised,
        "pass_without_standing_control": [str(r.get("id", "?")) for r in passes
                                          if not r.get("standing_control")],
        "not_applicable_without_tripwire": [str(r.get("id", "?")) for r in not_applicable
                                            if not r.get("tripwire")],
    }


def production_eligible(t: dict) -> tuple[bool, str]:
    """COMPUTED, never asserted. Returns (eligible, the reason it is not).

    An unbanded open record blocks: it is a finding whose severity is unknown,
    and unknown severity cannot be assumed harmless."""
    blocking = {b: t["open_by_band"][b] for b in BLOCKING_BANDS if t["open_by_band"][b]}
    if t["unrecognised_verdict"]:
        return False, (f"{len(t['unrecognised_verdict'])} finding(s) carry an unrecognised verdict "
                       f"({', '.join(t['unrecognised_verdict'][:5])}) — state unknown, treated as blocking")
    if t["unbanded_open"]:
        return False, (f"{len(t['unbanded_open'])} open finding(s) carry an unrecognised band "
                       f"({', '.join(t['unbanded_open'][:5])}) — severity unknown, treated as blocking")
    if blocking:
        return False, ", ".join(f"{n} open {b}" for b, n in blocking.items())
    return True, ""


def _render(t: dict, eligible: bool, why: str) -> str:
    lines = [
        f"registered checks        {t['registered_check_count']}",
        f"open findings            {t['open_total']}",
    ]
    for band in BANDS:
        n = t["open_by_band"][band]
        if n:
            mark = "  <-- blocks production" if band in BLOCKING_BANDS else ""
            lines.append(f"  {band:<12} {n}{mark}")
    if t["unbanded_open"]:
        lines.append(f"  {'UNBANDED':<12} {len(t['unbanded_open'])}  <-- blocks production")
    if t["unrecognised_verdict"]:
        lines.append(f"  {'UNREADABLE':<12} {len(t['unrecognised_verdict'])}  "
                     f"<-- unrecognised verdict, blocks production")
    lines += [
        f"PASS without standing control        {len(t['pass_without_standing_control'])}",
        f"NOT-APPLICABLE without tripwire      {len(t['not_applicable_without_tripwire'])}",
        "",
        f"production_eligible      {str(eligible).lower()}  (computed)",
    ]
    if not eligible:
        lines.append(f"  reason: {why}")
    return "\n".join(lines)


def selftest() -> int:
    def expect(cond: bool, msg: str) -> None:
        if not cond:
            print(f"FAIL selftest: {msg}", file=sys.stderr)
            sys.exit(1)

    expect(normalize_band("STOP-SHIP") == "STOP-SHIP", "plain band")
    expect(normalize_band("STOP-SHIP (A-01+A-39 both fail)") == "STOP-SHIP",
           "free-text suffix must still resolve — this is the real ledger's shape")
    expect(normalize_band("blocker-1") == "BLOCKER-1", "case-insensitive")
    expect(normalize_band("STOP-SHIPPING") is None,
           "a longer word must NOT match: \\b guards the token boundary")
    expect(normalize_band("") is None and normalize_band(None) is None, "empty/None -> None")
    expect(normalize_band(123) is None, "non-string -> None")

    esc = {"band": "PLAN", "escalated_band": "STOP-SHIP (A-01+A-39)", "verdict": "FAIL"}
    expect(effective_band(esc) == "STOP-SHIP", "escalation must override the base band")
    expect(effective_band({"band": "MUST-FIX", "verdict": "FAIL"}) == "MUST-FIX", "base band when not escalated")

    expect(is_open({"verdict": "FAIL"}) and is_open({"verdict": "PARTIAL"}), "FAIL/PARTIAL are open")
    expect(not is_open({"verdict": "PASS"}), "PASS is not open")
    expect(not is_open({"verdict": "NOT-APPLICABLE"}), "NOT-APPLICABLE is not open")
    expect(is_open({"verdict": "FAILED"}) and is_open({"verdict": "PARTIALLY"}),
           "an unrecognised verdict must count as OPEN -- a typo must not shrink the count")
    expect(is_open({}) and is_open({"verdict": None}) and is_open({"verdict": 7}),
           "missing/None/non-string verdict is open, never silently closed")
    expect(normalize_verdict("pass") == "PASS" and normalize_verdict(" fail ") == "FAIL",
           "verdicts normalise on case and surrounding space")
    expect(normalize_verdict("FAILED") is None and normalize_verdict("PASSED") is None,
           "a near-miss verdict resolves to None, not to the token it resembles")

    typo = tally([{"id": "X-6", "verdict": "FAILED", "band": "MUST-FIX"}])
    expect(typo["unrecognised_verdict"] == ["X-6"], "an unreadable verdict is reported by id")
    expect(typo["open_total"] == 1, "and is still counted among the open findings")
    ok, why = production_eligible(typo)
    expect(not ok and "unrecognised verdict" in why,
           "an unreadable verdict must block: unknown state is not harmless")

    # The regression this gate exists to refuse: rewriting every open verdict to
    # a plausible synonym previously reported `open findings 0` and granted
    # deploy admission. Two records, one FAIL and one PARTIAL, both misspelt.
    forged = tally([{"id": "X-7", "verdict": "FAILED", "band": "STOP-SHIP"},
                    {"id": "X-8", "verdict": "PARTIALLY", "band": "BLOCKER-1"}])
    expect(forged["open_total"] == 2, "misspelt verdicts stay counted, not dropped")
    expect(not production_eligible(forged)[0], "and cannot compute production_eligible true")

    clean = tally([{"id": "X-1", "verdict": "PASS", "band": "PLAN", "standing_control": "ci"}])
    ok, _ = production_eligible(clean)
    expect(ok, "no open blocker -> eligible")

    blocked = tally([{"id": "X-2", "verdict": "FAIL", "band": "BLOCKER-1"}])
    ok, why = production_eligible(blocked)
    expect(not ok and "BLOCKER-1" in why, "an open BLOCKER-1 must block and say so")

    b2 = tally([{"id": "X-3", "verdict": "FAIL", "band": "BLOCKER-2"}])
    expect(production_eligible(b2)[0], "BLOCKER-2 alone must not block production")

    weird = tally([{"id": "X-4", "verdict": "FAIL", "band": "banana"}])
    ok, why = production_eligible(weird)
    expect(not ok and "unrecognised" in why,
           "an unreadable band must block: unknown severity is not harmless")

    nc = tally([{"id": "X-5", "verdict": "PASS", "band": "PLAN"}])
    expect(nc["pass_without_standing_control"] == ["X-5"], "PASS without a standing control is flagged")

    print("   OK selftest: normalize_band + effective_band + is_open + tally + production_eligible")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    try:
        records = load_findings()
        t = tally(records)
        eligible, why = production_eligible(t)
    except Exception as exc:                                   # noqa: BLE001
        # FAIL-CLOSED. Every mode. An unreadable ledger is a refusal to admit,
        # never an admission — the failure mode this whole file guards against
        # is a gate that opens because something went wrong.
        print(f"REFUSE findings gate: cannot evaluate the ledger — {exc}", file=sys.stderr)
        return 1

    if "--admission" in argv:
        if not eligible:
            print(f"REFUSE deploy admission: production_eligible is false — {why}", file=sys.stderr)
            return 1
        print("Deploy admission granted: production_eligible is true (computed).")
        return 0

    print(_render(t, eligible, why))

    if "--strict" in argv:
        problems = []
        blocking = sum(t["open_by_band"][b] for b in BLOCKING_BANDS) + len(t["unbanded_open"])
        if blocking:
            problems.append(f"{blocking} open STOP-SHIP/BLOCKER-1 finding(s)")
        if t["unrecognised_verdict"]:
            problems.append(f"{len(t['unrecognised_verdict'])} finding(s) with an unrecognised verdict")
        if t["pass_without_standing_control"]:
            problems.append(f"{len(t['pass_without_standing_control'])} PASS record(s) with no "
                            f"standing control (§9.10.1 downgrades these to PARTIAL)")
        if problems:
            print(f"BLOCK strict findings gate: {'; '.join(problems)}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
