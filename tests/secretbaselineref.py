"""The accepted secret baseline, in one place: the entries as a literal, the scan as a pinned commit.

## What is pinned

Two things about `.secrets.baseline` do not change without a reviewer seeing it:

  * the ENTRIES it holds: `ACCEPTED_ENTRIES`, every entry by file, detector
    type and hash, a literal in this module. `assert_accepted_entries`
    requires equality.
  * the SCAN CONFIGURATION (filters, plugins, detect-secrets version): the
    baseline's at `ACCEPTED_SECRET_BASELINE_COMMIT`, a pinned commit id.
    `assert_configuration_unchanged` requires equality.

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

## Why the entries are a literal and no longer the pinned commit's

The pinned commit was the reference for the entries too: the committed baseline
had to have "not grown" against it (`assert_has_not_grown`), and a
byte-identity check (`assert_identical_to_reference`) was to make this file
move on every other change. Executed on 9f79c7a:

    baseline at the pinned commit b047c3d (2026-08-14)   19 entries in 4 files
    committed baseline                                   4 entries in 1 file
    committed + 1, + 2, + 15 new entries                 accepted by the growth check
    committed + 16                                       refused ("baseline grew by 1 entries")
    the identity check on the committed baseline         fails ("entries 19 -> 4"); its one consumer,
                                                         tests/test_verifier_mc4_passc.py, was retired on
                                                         2026-08-17, and since then it ran on mutated
                                                         copies of the reference only

The room for 15 entries opened when files left the repository together with
their entries (19 -> 5 on 2026-08-17, 5 -> 4 with #149): a shrink satisfied the
growth check and nothing made the commit follow. Moving the commit would close
the room once and leave its cause: merges are squashed, so a change cannot name
its own merge commit, the reference can only follow one pull request late, and
every shrink until then is room again. A literal moves in the same diff as the
baseline, and equality refuses the first entry added, the first one removed and
the first one exchanged for another.

Both functions are deleted. The commit stays the reference for the scan
configuration, which has not changed since it was pinned.

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

#: The commit whose baseline is the reference for the scan configuration (and
#: was the reference for the entries: see the module docstring). A 40-hex
#: commit id, written as a literal so that no ref lookup — and therefore no
#: branch movement — can change it.
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
#:
#: HISTORICAL NOTE: the `midtermpanel` package named above was retired when the
#: review lane was simplified to scripts/independent_verify.py. The record is
#: left as written — it states correctly what recomputed those digests at the
#: time, and a governance ledger that edits its own history is worth less than
#: one carrying a stale package name. Two things it says no longer hold. The
#: entries it accepted left .secrets.baseline with the files they were in
#: (19 entries -> 5 on 2026-08-17, 5 -> 4 with #149). And the two functions it
#: names, `assert_has_not_grown` and `assert_identical_to_reference`, are
#: deleted: the entries are pinned by `ACCEPTED_ENTRIES` below, and this commit
#: is the reference for the scan configuration only.
ACCEPTED_SECRET_BASELINE_COMMIT = \
    "b047c3d61b4999e8f1b387a2d933d1ad787bd8bd"  # pragma: allowlist secret

BASELINE_PATH = ".secrets.baseline"


def reference_baseline_bytes(*, root,
                             commit: str = ACCEPTED_SECRET_BASELINE_COMMIT):
    """The baseline's exact bytes at the pinned commit, or None if the commit is absent.

    Read out of the git object database at a pinned commit, so a working tree
    that has been edited cannot be mistaken for the reference."""
    got = subprocess.run(  # noqa: S603
        ["git", "show", f"{commit}:{BASELINE_PATH}"],
        cwd=str(root), capture_output=True)
    if got.returncode != 0:
        return None
    return got.stdout


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


#: What the committed baseline may hold, entry by entry: under each file the
#: detector type and the `hashed_secret` of every record. File, type and hash
#: are what the scan matches a secret to an entry by (detect-secrets 1.5.0:
#: `PotentialSecret` compares `filename`, `secret_hash` and `type`), so an
#: entry exchanged for another in the same file is a different literal.
#: `line_number` is not named: it decides nothing in the scan, and the hook
#: rewrites it itself when a fixture moves.
#:
#: A literal, so that a change to it is in the diff a reviewer reads. The four
#: are the SHA-1 digests of key fixtures of the admin-key and read-access tests
#: in tests/test_audit_v331.py, as `.secrets.baseline` records them; a digest
#: is credential-shaped to the scan, hence the pragmas.
ACCEPTED_ENTRIES: dict[str, list[tuple[str, str]]] = {
    "tests/test_audit_v331.py": [
        # the shipped placeholder key, which must not authenticate
        ("Secret Keyword", "2d1c3689e7808257bc4766b7a041f4ab23f18221"),  # pragma: allowlist secret
        # the made-up key the tests configure as the real one
        ("Secret Keyword", "ae8d7f6c6ba6a60c610084db45a951794b1c3805"),  # pragma: allowlist secret
        # the wrong key, refused with 401
        ("Secret Keyword", "a4b48a81cdab1e1a5dd37907d6c85ca1c61ddc7c"),  # pragma: allowlist secret
        # the non-ASCII key, refused with 401
        ("Secret Keyword", "7859fe48ea5b2d07394321d3d1b6c80e14a9abb8"),  # pragma: allowlist secret
    ],
}


def entries_by_file(document: dict) -> dict[str, list[tuple[str, str]]]:
    """Each file's records as (type, hashed_secret), sorted.

    A list and not a set, so that a record written twice counts twice."""
    return {path: sorted((entry["type"], entry["hashed_secret"])
                         for entry in found)
            for path, found in document.get("results", {}).items()}


# `assert_has_not_grown` and `assert_identical_to_reference` stood here. The
# function below replaces both and is stricter than they were on the committed
# file. Executed on 9f79c7a, where the pinned commit holds 19 entries in 4
# files and the committed baseline 4 in 1:
#
#     assert_has_not_grown, the one entry check applied to the committed file
#         committed + 1, + 2, + 15 new entries     accepted
#         committed + 16                           refused
#     assert_identical_to_reference
#         on the committed file                    fails ("entries 19 -> 4"):
#                                                  no test applied it there, it
#                                                  ran on mutated copies of the
#                                                  reference only
#
# Equality with the literal refuses the first entry added, the first one
# removed and the first one exchanged, and there is no reference behind it that
# can go stale. What byte-identity was introduced for, a filter entry that
# silently REPLACES another, is a configuration change:
# `assert_configuration_unchanged` below refuses it, and
# tests/test_secret_gate_policy.py applies it to the committed file.
def assert_accepted_entries(current: dict) -> None:
    """The baseline holds exactly `ACCEPTED_ENTRIES`: the same entries, no
    entry more, none fewer.

    An added entry needs a reviewer, and so does one exchanged for another in
    the same file: the scan accepts whatever secret the new hash belongs to. A
    removed one leaves the literal in the same change, so that no room for a
    later addition opens."""
    now = entries_by_file(current)
    accepted = {path: sorted(found) for path, found in ACCEPTED_ENTRIES.items()}
    if now != accepted:
        raise AssertionError(
            f"baseline entries {now} are not the accepted {accepted}: an added "
            "or exchanged entry needs review; a removed one has to leave "
            "ACCEPTED_ENTRIES in tests/secretbaselineref.py in the same change")


def assert_configuration_unchanged(current: dict, reference: dict) -> None:
    """Filters, plugins and the detect-secrets version, against the pinned commit.

    They decide WHAT is scanned. The entries are compared separately
    (`assert_accepted_entries`), so a filter change and an entry change fail
    under their own names."""
    now, was = _configuration(current), _configuration(reference)
    if now == was:
        return
    changed = [k for k in was if now.get(k) != was.get(k)]
    raise AssertionError(
        f"baseline scan configuration changed: {changed} — a filter or plugin "
        "change alters WHAT is scanned, which is a policy change and not a "
        "baseline update")
