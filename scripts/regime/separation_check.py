#!/usr/bin/env python3
"""Separation of the gate from the gated — mandate B-35, Article II.

The identity that writes the code must not be able to move the gate that judges
it without the owning role's sign-off. In a single-maintainer repository that
separation cannot be a second human, so it is structural instead:

  - the ruleset on `main` (no bypass actors) means nothing merges unreviewed;
  - the review panel's definition lives on the default branch and its
    credential lives in an environment scoped to `main`, so a candidate branch
    cannot reach either;
  - and CODEOWNERS enumerates the surfaces that carry policy.

This file guards the third one. It fails CI when a path that carries policy has
no owner entry — which is how that list stops silently rotting as the
repository grows. It does NOT claim that Code Owner review is enforced; it is
not, and audit/06-residual-risk-register.md records why.

Deliberately a path-coverage check and not a pattern-equality check: CODEOWNERS
may legitimately be broader than this list. It may never be narrower.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"

# Every surface whose change alters what the pipeline ALLOWS, rather than what
# the product computes. Each entry is (path, why it carries policy).
PROTECTED: tuple[tuple[str, str], ...] = (
    (".github/", "workflow definitions and the CODEOWNERS list itself"),
    ("scripts/", "the review panel and the regime gates"),
    ("governance/", "the constitution and its attested hash"),
    ("audit/", "the findings ledger the deploy gate reads"),
    ("frozen_methodology.json", "the frozen scoring methodology (F-01/L-07)"),
    ("app/methodology.py", "the code that reads the frozen methodology"),
    ("app/alerts/", "the rules that decide whether a human is woken"),
    ("config/alert_rules.v3.2.yaml", "the alert ruleset artifact"),
    # BOTH, not the newest. v3.2 is released and frozen — hosts hold its bytes
    # and its version is a registry primary key — so it needs separation
    # protection more than the version still being worked on, not less.
    # Repointing this at v3.3 quietly removed the older one from cover.
    ("config/alert_phrases.v3.2.json", "the alert phrasing artifact (released)"),
    ("config/alert_phrases.v3.3.json", "the alert phrasing artifact (released)"),
    ("config/alert_phrases.v3.4.json", "the alert phrasing artifact"),
)


def owned_patterns(text: str) -> list[str]:
    """CODEOWNERS patterns that name at least one owner.

    A pattern with no owner is CODEOWNERS' way of REMOVING ownership from a
    path an earlier line covered, so counting it as coverage would invert its
    meaning."""
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and any(p.startswith("@") or "@" in p for p in parts[1:]):
            out.append(parts[0])
    return out


def covers(pattern: str, path: str) -> bool:
    """Does a CODEOWNERS pattern cover this path?

    Only the two forms this repository uses are honoured — a rooted directory
    prefix (`/scripts/`) and a rooted file path (`/frozen_methodology.json`).
    Anything more clever (globs, bare names matching at any depth) is NOT
    treated as coverage: a check that guesses generously about its own
    enforcement is worse than one that asks for an explicit line."""
    pat = pattern[1:] if pattern.startswith("/") else pattern
    target = path[1:] if path.startswith("/") else path
    if pat.endswith("/"):
        return target == pat or target.startswith(pat)
    return target == pat


def uncovered(text: str) -> list[tuple[str, str]]:
    patterns = owned_patterns(text)
    return [(p, why) for p, why in PROTECTED
            if not any(covers(pat, p) for pat in patterns)]


def selftest() -> int:
    def expect(cond: bool, msg: str) -> None:
        if not cond:
            print(f"FAIL selftest: {msg}", file=sys.stderr)
            sys.exit(1)

    expect(owned_patterns("/scripts/ @me") == ["/scripts/"], "an owned pattern is collected")
    expect(owned_patterns("# /scripts/ @me") == [], "a comment is not a rule")
    expect(owned_patterns("/scripts/") == [],
           "a pattern with NO owner removes ownership; it must not count as coverage")
    expect(owned_patterns("/a/ @x  # trailing") == ["/a/"], "trailing comments are stripped")

    expect(covers("/scripts/", "scripts/"), "directory covers itself")
    expect(covers("/scripts/", "scripts/regime/x.py"), "directory covers descendants")
    expect(not covers("/scripts/", "scripts_other/x.py"),
           "prefix must respect the separator, or scripts_other would look owned")
    expect(covers("/frozen_methodology.json", "frozen_methodology.json"), "exact file")
    expect(not covers("/frozen_methodology.json", "other.json"), "different file")
    expect(not covers("/app/alerts/", "app/"), "a child pattern does not cover its parent")

    expect(uncovered("/.github/ @m\n/scripts/ @m\n"),
           "a partial CODEOWNERS must report the rest as uncovered")
    full = "".join(f"{p if p.startswith('/') else '/' + p} @m\n" for p, _ in PROTECTED)
    expect(not uncovered(full), "a CODEOWNERS naming every protected path must pass")

    print("   OK selftest: owned_patterns + covers + uncovered")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if not CODEOWNERS.is_file():
        print("BLOCK separation check: .github/CODEOWNERS is missing — the protected "
              "surfaces have no owner of record (B-35).", file=sys.stderr)
        return 1
    missing = uncovered(CODEOWNERS.read_text(encoding="utf-8"))
    if missing:
        print("BLOCK separation check (B-35): these surfaces carry policy but have no "
              "CODEOWNERS entry:", file=sys.stderr)
        for path, why in missing:
            print(f"  {path}\n      {why}", file=sys.stderr)
        return 1
    print(f"Separation check: all {len(PROTECTED)} policy-carrying surfaces are owned in CODEOWNERS.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
