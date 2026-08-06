"""The accepted secret-baseline reference, in one place, as a pinned commit.

## Why this module exists

`.secrets.baseline` is a ratchet: the gate refuses any secret in a tracked file
that the baseline does not already carry, and the baseline may not GROW without
someone looking. Two tests enforced that, and each hardcoded its own copy of the
reference commit:

    tests/test_verifier_mc4_passc.py    b08844a0…   byte-identity
    tests/test_secret_gate_policy.py    b08844a0…   growth ratchet

Two copies of one authority is two things that can disagree. They also both
pinned a commit from before the mid-term panel existed, so merging PR #34 —
which legitimately added ONE entry — turned both red at once.

## Why a pinned commit and not merge-base

Deliberate, and it is the whole point. `merge-base` is a function of where
branches happen to be; it moves when someone pushes. A ratchet whose reference
moves is not a ratchet — a branch that drifts far enough would carry its own
growth along as the new "accepted" state, and nothing would report it. The
accepted baseline is a decision a person made once, so it is recorded as an
immutable commit id and changed only by editing this file.

## The transition that produced the current value

`b08844a0…` → `ab5f78bc…`, accepted after an exact old→new report:

    baseline blob sha256 before  54ad33b2e7bb38ef0beca091eb08321e0ee40ab8257cbdbded27381cd10c414f
    baseline blob sha256 after   01b135a423b86e47853eaf8754d3559ab5211034975c6e1757ce9fd4ae31386c
    entries before / after       5 / 6      (+1)
    files before / after         2 / 3
    added                        governance/midterm-panel-engine-release.json line 6
    removed                      none
    changed                      none
    filters added/removed/changed        none
    plugins added/removed/changed        none
    detect-secrets version       1.5.0 -> 1.5.0 (unchanged)

The single addition is `approved_engine_source_sha` in a file PR #34 created:
the file does not exist at `b08844a0…`, it was introduced by `1ae0f5cd` on the
panel branch, and the flagged value `c8ba2a72…` resolves to a real git COMMIT
object in this repository. An entropy detector cannot distinguish a commit id
from a credential, which is the correct default; JSON has no comment syntax, so
the disposition is recorded here rather than inline.

Nothing was regenerated. The `generated_at` stamp differs because PR #34 edited
the file, not because a scan rewrote it.
"""

from __future__ import annotations

import json
import subprocess

#: The accepted post-PR34 baseline. A 40-hex commit id, written as a literal so
#: that no ref lookup — and therefore no branch movement — can change it.
ACCEPTED_SECRET_BASELINE_COMMIT = \
    "ab5f78bc15ec7b7ce993ab16c0b4f24840c6991f"  # pragma: allowlist secret

BASELINE_PATH = ".secrets.baseline"


def reference_baseline_bytes(*, root,
                             commit: str = ACCEPTED_SECRET_BASELINE_COMMIT):
    """The accepted baseline's exact bytes, or None if the commit is absent.

    Read out of the git object database at a pinned commit, so a working tree
    that has been edited cannot be mistaken for the reference."""
    got = subprocess.run(  # noqa: S603
        ["git", "show", f"{commit}:{BASELINE_PATH}"],
        cwd=str(root), capture_output=True)
    if got.returncode != 0:
        return None
    return got.stdout


def entry_count(document: dict) -> int:
    """Total baseline entries across every file."""
    return sum(len(v) for v in document.get("results", {}).values())


def _configuration(document: dict) -> dict:
    """The parts that decide WHAT gets scanned, as comparable data.

    Kept separate from the entries because a filter change and an entry change
    fail for different reasons and a reader needs to know which happened."""
    return {
        "version": document.get("version"),
        "filters_used": sorted(
            json.dumps(f, sort_keys=True)
            for f in document.get("filters_used", [])),
        "plugins_used": sorted(
            json.dumps(p, sort_keys=True)
            for p in document.get("plugins_used", [])),
    }


def assert_identical_to_reference(current: bytes, reference: bytes) -> None:
    """Byte-identical. Not "no worse" — IDENTICAL.

    MC3 edited this file and silently dropped the `.venv/`, `__pycache__/` and
    cache exclusions, because detect-secrets keys filters by function path and a
    second entry REPLACES the first. Byte-identity is the only comparison that
    catches a replacement as well as an addition."""
    if current == reference:
        return
    now, was = json.loads(current), json.loads(reference)
    detail = []
    if entry_count(now) != entry_count(was):
        detail.append(f"entries {entry_count(was)} -> {entry_count(now)}")
    if _configuration(now) != _configuration(was):
        detail.append("filter/plugin/version configuration changed")
    if now.get("generated_at") != was.get("generated_at"):
        detail.append("generated_at differs (was the baseline REGENERATED?)")
    raise AssertionError(
        ".secrets.baseline differs from the accepted reference "
        f"{ACCEPTED_SECRET_BASELINE_COMMIT[:12]}…: "
        + ("; ".join(detail) if detail else "byte difference with no "
           "structural explanation"))


def assert_has_not_grown(current: dict, reference: dict) -> None:
    """The ratchet. An unbounded baseline is a slow-motion wildcard exclusion."""
    grew = entry_count(current) - entry_count(reference)
    if grew > 0:
        raise AssertionError(
            f"baseline grew by {grew} entries against the accepted reference "
            f"{ACCEPTED_SECRET_BASELINE_COMMIT[:12]}… — a new entry needs an "
            "explicit transition report and a new accepted reference, not a "
            "silent addition")


def assert_configuration_unchanged(current: dict, reference: dict) -> None:
    """Filters, plugins and the detect-secrets version, on their own.

    Byte-identity already implies this. It is asserted separately so that the
    protection is NAMED: if the identity check is ever relaxed to something
    weaker, the exclusion set does not quietly stop being defended with it."""
    now, was = _configuration(current), _configuration(reference)
    if now == was:
        return
    changed = [k for k in was if now.get(k) != was.get(k)]
    raise AssertionError(
        f"baseline scan configuration changed: {changed} — a filter or plugin "
        "change alters WHAT is scanned, which is a policy change and not a "
        "baseline update")
