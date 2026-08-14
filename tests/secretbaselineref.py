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

#: The accepted baseline. A 40-hex commit id, written as a literal so that no
#: ref lookup — and therefore no branch movement — can change it.
#:
#: TRANSITION (schema-v2 engine re-binding). Previous reference dc60efbc4e88…
#:
#:     baseline blob sha256 before  87f536ad374774307885eeae5c4dfe1e0da60a84f8b6d29a4cd5416b3b123a20
#:     baseline blob sha256 after   4bc565aeed203caeedc7091a72ecdd3dee0b31232a6d2374db859fc6dfda9bde
#:     entries before / after       20 / 19      (-1)
#:     files before / after         4 / 4
#:     added                        governance/midterm-panel-engine-release.json
#:                                  lines 6, 8, 10, 11, 21, 24, 37
#:     removed                      governance/midterm-panel-engine-release.json
#:                                  old lines 6, 7, 8, 10, 11, 20, 23, 36
#:     moved, value unchanged       old 21 -> 22, old 22 -> 23
#:     filters added/removed/changed        none
#:     plugins added/removed/changed        none
#:     detect-secrets version       1.5.0 -> 1.5.0 (unchanged)
#:
#: This is the first transition that SHRINKS the baseline, and the reason is
#: the one fact worth carrying: the two role pins are now the same commit, so
#: nine of the ten values in this file are distinct where ten used to be.
#: `assert_has_not_grown` is satisfied by a shrink and would have said nothing;
#: `assert_identical_to_reference` is what requires this file to move, which is
#: the intended division of labour between them.
#:
#: Every entry is in the ONE file this re-binding rewrote, and each is
#: identified rather than counted. Seven values were replaced, one disappeared,
#: two survived unchanged and only shifted line:
#:
#:     line  6  approved_engine_source_sha         c8ba2a72… -> 5986c13d…, a git
#:                                                 COMMIT id; `main` after the
#:                                                 schema fix merged
#:     (old 7) approved_engine_protected_sha       27bfefb5… REMOVED as a
#:                                                 distinct value: it is now
#:                                                 5986c13d… too, and
#:                                                 detect-secrets records one
#:                                                 entry per distinct value per
#:                                                 file. The field is still
#:                                                 there; only the entry is gone
#:     line  8  approved_engine_artifact_sha256    b7059321… -> cf0b14d2…, the
#:                                                 released engine.tar.gz,
#:                                                 rebuilt from the two pinned
#:                                                 commits in this container to
#:                                                 the same digest before it was
#:                                                 recorded
#:     line 10  approved_engine_identity_sha256    14de0945… -> beb82a9f…, the
#:                                                 released engine-identity.json
#:     line 11  engine_release_binding_sha256      f2f961fd… -> 499c1e22…,
#:                                                 recomputed over the seven
#:                                                 binding fields by
#:                                                 midtermpanel.engine
#:     line 21  engine_source_sha256               9b3dae26… -> c1bd860e…
#:     line 22  runtime_lock_sha256                UNCHANGED, was line 21
#:     line 23  sbom_sha256                        UNCHANGED, was line 22
#:     line 24  provenance_sha256                  54123398… -> 388b975f…
#:     line 37  actions_artifact_zip_sha256        4dd3c679… -> fa1f32aa…, the
#:                                                 Actions ZIP wrapper
#:
#: `approved_engine_artifact_sha256` still appears twice in the file — once as a
#: binding field and once inside the restatement — and is one entry for the same
#: reason the two role pins are now one.
#:
#: None is a credential. An entropy detector cannot distinguish a 64-hex
#: content digest or a 40-hex commit id from a 64-hex token, which is the
#: correct default; JSON has no comment syntax, so the disposition is recorded
#: here rather than inline. Nothing was regenerated beyond the `generated_at`
#: stamp, which moves because the scanned file changed.
ACCEPTED_SECRET_BASELINE_COMMIT = \
    "b047c3d61b4999e8f1b387a2d933d1ad787bd8bd"  # pragma: allowlist secret

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
