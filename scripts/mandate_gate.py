#!/usr/bin/env python3
"""The mandate gate (§9.10 of governance/mandate/part1.md) — the machine that
keeps the due-diligence mandate true after every engagement ends.

Runs in CI on every change (blocking) and on the weekly cadence. Subcommands:

  status     Validate audit/03-findings.json against audit/00-check-catalogue.json,
             recompute audit/engagement-status.json, fail closed on drift, on any
             PASS with a null standing_control, on any open blocker-band finding
             not in the accepted-residuals register, and on governance hash
             mismatches. `--write` regenerates the status file (repair lane).
  ratchet    Enforce audit/ratchet-baselines.json: floors may not fall, ceilings
             may not rise. `--measure` prints current values without enforcing.
  calibrate  S12 seeded-defect calibration: re-prove, with scratch files, that
             the gates still catch each class in the calibration corpus, and
             that the two NOT-APPLICABLE classes are still structurally absent.
  surface    Regenerate audit/00-audit-surface.json (the audit denominator).
             `--check` fails on drift instead of writing.
  all        status + ratchet + calibrate + surface --check (the CI entry point).

Design rules: stdlib only (subprocess for the real gate tools), deterministic,
fail-closed — a check that cannot run is a failed check, not a skipped one.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "audit"
GOV = ROOT / "governance"

BLOCKER_BANDS = ("STOP-SHIP", "BLOCKER-1", "BLOCKER-2")
ALL_BANDS = BLOCKER_BANDS + ("MUST-FIX", "SHOULD-FIX", "PLAN", "ASSESS")
OPEN_VERDICTS = ("FAIL", "PARTIAL", "NO-EVIDENCE")
# §5: the ONLY legal verdicts. Anything else fails the build (adversarial
# audit 2026-07-25, critical: there was no whitelist, so "Fail"/null/"WAIVED"
# fell out of the open-blocker loop, the PASS-control check and the N/A check
# simultaneously — a one-character edit made a STOP-SHIP invisible).
CANONICAL_VERDICTS = ("PASS", "FAIL", "PARTIAL", "NO-EVIDENCE", "NOT-APPLICABLE")


def record_fingerprint(rec: dict) -> str:
    """Identity of a decision record, for freshness comparison."""
    return json.dumps({k: rec.get(k) for k in
                       ("metric", "change", "finding_id", "reason",
                        "authorised_by")}, sort_keys=True)


def reintroduced_fingerprints(rel_path: str, extract) -> set[str]:
    """Records that were PRESENT, then REMOVED, then present again.

    Panel finding (PR #23 round 23): one-step comparison let a record be
    deleted in commit N and re-added verbatim in commit N+1 — against N it
    reads as new, replaying a historic authorisation for a fresh weakening.

    "Seen anywhere before" is the WRONG test, though: a record legitimately
    persists alongside the state it authorises, so that rule flags every
    honest record (it flagged A-01 and all 32 founding acceptances here).
    Re-introduction is the actual signature of a replay.

    The window is the file's FULL history (panel round 27). It began at the
    comparison point, so an absence OLDER than the branch point was invisible:
    a record removed on the base branch months ago reads as absent-then-present
    inside the window — never present-then-absent-then-present — and could be
    re-added verbatim to replay that historic authorisation for a new
    weakening. Widening the window only adds the ability to see older
    absences; it does NOT revert to "seen anywhere before", so a record that
    has legitimately persisted since founding still never trips."""
    point = comparison_point()
    if not point:
        return set()
    # rev-list over the PATH: only commits that touched the register are
    # examined, so full history costs a handful of blob reads, not one per
    # repository commit.
    revs = subprocess.run(["git", "rev-list", "--reverse", "HEAD", "--",
                           f":(literal){rel_path}"],
                          cwd=ROOT, capture_output=True, text=True)
    if revs.returncode != 0:
        _fail(f"cannot enumerate the history of {rel_path} "
              f"({revs.stderr.strip()[:200]}) — replay detection would be "
              "silently disabled; fail closed (ensure fetch-depth: 0)")
    shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                             cwd=ROOT, capture_output=True, text=True)
    if shallow.stdout.strip() == "true" and os.environ.get("GITHUB_ACTIONS"):
        _fail("shallow clone: the history behind the comparison point cannot "
              "be read, so a replayed decision record would be invisible — "
              "check out with fetch-depth: 0")
    commits = [c for c in revs.stdout.split() if c] or [point]
    seen: set[str] = set()
    gone: set[str] = set()
    replayed: set[str] = set()
    for commit in commits:
        blob = subprocess.run(["git", "show", f"{commit}:{rel_path}"],
                              cwd=ROOT, capture_output=True, text=True)
        present: set[str] = set()
        if blob.returncode == 0:
            try:
                data = json.loads(blob.stdout)
            except json.JSONDecodeError:
                data = None
            if data is not None:
                present = {record_fingerprint(r) for r in (extract(data) or [])
                           if isinstance(r, dict)}
        replayed |= present & gone          # back after having been removed
        gone |= seen - present              # disappeared at this commit
        seen |= present
    return replayed


def is_fresh(rec: dict, previous_records) -> bool:
    """True when this exact record did NOT exist at the comparison point.

    Panel rounds 17-18: every record type could be RETAINED and re-used to
    authorise a later, separate weakening — the record is a decision about
    one change, not a standing licence. One mechanism, applied to all three
    record types, rather than a per-type patch each round."""
    prev = {record_fingerprint(r) for r in (previous_records or [])
            if isinstance(r, dict)}
    return record_fingerprint(rec) not in prev


def valid_weakening_record(rec, change: str | None = None) -> bool:
    """A weakening record must be structured, substantive and attributable.

    Panel finding (PR #23 round 16): the catalogue cross-check accepted ANY
    dict, so `weakening_record: {}` licensed a founding-band downgrade."""
    if not isinstance(rec, dict):
        return False
    reason = rec.get("reason")
    who = rec.get("authorised_by")
    if not (isinstance(reason, str) and len(reason.strip()) >= 20):
        return False
    if not (isinstance(who, str) and who.strip()):
        return False
    if change is not None and str(rec.get("change", "")).strip() != change:
        return False
    return True


def const_str(node):
    """The string an AST expression provably evaluates to, or None.

    Only LITERAL composition is folded — that is the point. `"a" + "b"`,
    f-strings of literals, and their nesting resolve; a Name, a call, or a
    subscript does not. Callers decide what an unresolvable value means: the
    tools check treats it as "not the tools literal", the route inventory
    treats it as "an endpoint I cannot read" and fails closed. Shared by the
    calibration tools-key check and the surface route extractor so the two
    cannot drift (they did, across panel rounds 27/32/33)."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = const_str(node.left)
        right = const_str(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):                # f-string of literals
        parts = []
        for v in node.values:
            s = const_str(v)
            if s is None:
                return None
            parts.append(s)
        return "".join(parts)
    # f"a{'b'}" — the interpolated piece is a FormattedValue wrapping the
    # expression; a conversion (!r) or format spec changes the result, so
    # those are left unresolved.
    if isinstance(node, ast.FormattedValue):
        if node.conversion in (-1, None) and node.format_spec is None:
            return const_str(node.value)
        return None
    return None


def transition_matches(change: str, old, new) -> bool:
    """True when `change` is exactly the transition old->new (tokens compared,
    not substrings — "120 -> 210" must not satisfy 20->21). Space-tolerant
    around the arrow. Module-level so the ratchet check and its test share it
    (review P1.5-c)."""
    m = re.match(r"\s*(\S+)\s*->\s*(\S+)", change)
    return bool(m and m.group(1) == str(old) and m.group(2) == str(new))


def tool_enabling(tree) -> bool:
    """True if an AST names the model `tools` field in ANY form — a `tools=`
    call kwarg, a `{"tools": …}` dict key, or any constant that folds to
    "tools" (`payload["to" + "ols"]`). The invariant is that app code must not
    name the field at all. Module-level so the calibration check AND its tests
    exercise the SAME detector (panel review P1.5-c: the tests had copied a
    regex and never ran this)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if any(k.arg == "tools" for k in node.keywords):
                return True
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and k.value == "tools":
                    return True
        if const_str(node) == "tools":
            return True
    return False


_TENANCY_ENTITIES = (r"user|tenant|owner|account|customer|org|organisation|"
                     r"organization|workspace|member|subject|principal")
_ORM_MARKERS = ("__tablename__", "mapped_column", "relationship(",
                "ForeignKey(", "Table(", "declarative_base", "create_table")
# The weaker `<entity>_id` / tenancy-FK / tenancy-relationship signal, plus a
# tenancy class declaration; meaningful only inside an ORM-bearing module.
_TENANCY_COLUMN = re.compile(
    rf"\b({_TENANCY_ENTITIES})[_]?id\b"
    r"|ForeignKey\(\s*[\"'](users|accounts|tenants|orgs|organisations|"
    r"organizations|customers|members|workspaces)\."
    rf"|^\s*class\s+({_TENANCY_ENTITIES})[A-Za-z]*\b"
    rf"|relationship\(\s*[\"']({_TENANCY_ENTITIES})[A-Za-z]*[\"']",
    re.IGNORECASE | re.MULTILINE)
# A per-user/tenant TABLE created (raw SQL or create_table()) or a tenancy
# model declared — multi-tenancy evidence that needs no ORM corroboration.
_STRONG_TENANCY = re.compile(
    rf"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?"
    rf"({_TENANCY_ENTITIES})[A-Za-z_]*\b"
    rf"|create_table\(\s*[\"']({_TENANCY_ENTITIES})[A-Za-z_]*"
    rf"|^\s*class\s+({_TENANCY_ENTITIES})[A-Za-z]*\s*\(",
    re.IGNORECASE | re.MULTILINE)


def has_orm_markers(src: str) -> bool:
    return any(m in src for m in _ORM_MARKERS)


def declares_strong_tenancy(src: str) -> bool:
    return bool(_STRONG_TENANCY.search(src))


def declares_tenancy_column(src: str) -> bool:
    return bool(_TENANCY_COLUMN.search(src))


def weakening_records(raw) -> list:
    """Normalise `weakening_record` to a list of candidate records.

    A finding that is re-banded AND closed in one change needs one record per
    transition (panel round 24), so the field accepts either a single dict or
    a list of them. EVERY reader must go through this: a list-shaped field
    read as a dict silently reduces to "no record" — which blocks a legitimate
    change at the catalogue cross-check, and, on the prior side of the
    freshness comparison, makes a RETAINED list look fresh."""
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def effective_band(finding: dict) -> str:
    """Escalations applied, fail-closed (2026-07-25 audit finding: the
    free-text escalated_band 'STOP-SHIP (A-01+A-39 both fail)' matched no
    band constant, so an open FAIL silently escaped the blocker gate). A
    band value must START WITH a known band token; anything unparseable is
    a gate failure, never a silently ignored record."""
    def _parse(raw) -> str | None:
        for band in ALL_BANDS:
            if str(raw).startswith(band):
                return band
        return None

    base = _parse(finding["band"])
    if base is None:
        _fail(f"{finding['id']} has unparseable band {finding['band']!r} — a "
              "record the gate cannot band is a record it cannot gate")
    raw_esc = finding.get("escalated_band")
    if not raw_esc:
        return base
    esc = _parse(raw_esc)
    if esc is None:
        _fail(f"{finding['id']} has unparseable escalated_band {raw_esc!r}")
    # Panel finding (PR #23 round 11): the escalated band was taken verbatim,
    # so `band: STOP-SHIP` + `escalated_band: MUST-FIX` DOWNGRADED a finding
    # out of the blocker gate. An escalation may only ever raise severity.
    return min(base, esc, key=ALL_BANDS.index)


def _fail(msg: str) -> None:
    print(f"MANDATE-GATE FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        _fail(f"required artifact missing: {path.relative_to(ROOT)}")
    except json.JSONDecodeError as exc:
        _fail(f"unparseable artifact {path.relative_to(ROOT)}: {exc}")


def _base_ref() -> str:
    return os.environ.get("GITHUB_BASE_REF") or "main"


def _resolve(ref: str) -> str | None:
    r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                       cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() or None


def _is_shallow() -> bool:
    r = subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                       cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() == "true"


def comparison_point() -> str | None:
    """The commit this change should be judged AGAINST.

    Panel findings (PR #23 round 12): comparing to `origin/main` on a
    push-to-main compares the new state to ITSELF, so every weakening check
    silently passed; and an unresolvable ref returned None, which skipped the
    checks entirely rather than blocking. On a pull request the reference is
    the MERGE BASE (the branch point, not the moving tip); elsewhere it is the
    parent commit."""
    if _is_shallow() and os.environ.get("GITHUB_ACTIONS") == "true":
        _fail("shallow checkout: the weakening checks need real history — "
              "set fetch-depth: 0 (fail-closed rather than skip them)")
    base = os.environ.get("GITHUB_BASE_REF")
    if base:
        for ref in (f"origin/{base}", base):
            if _resolve(ref):
                mb = subprocess.run(["git", "merge-base", ref, "HEAD"],
                                    cwd=ROOT, capture_output=True, text=True)
                if mb.returncode == 0 and mb.stdout.strip():
                    return mb.stdout.strip()
        # In CI a declared base that cannot be resolved is a broken checkout,
        # not a licence to skip the weakening checks.
        if os.environ.get("GITHUB_ACTIONS") == "true":
            _fail(f"cannot resolve base ref {base!r} — refusing to run the "
                  "weakening checks against no reference (fail-closed)")
        return None
    # Panel finding (PR #23 round 14): HEAD~1 judges a multi-commit push
    # against its own already-weakened parent — weaken in commit N-2, and the
    # only CI run compares the tip to the weakened commit and passes. The
    # pre-push SHA is the state that was actually reviewed before.
    before = (os.environ.get("MANDATE_PUSH_BEFORE") or "").strip()
    if before and set(before) != {"0"}:
        resolved = _resolve(before)
        if resolved:
            return resolved
        if os.environ.get("GITHUB_ACTIONS") == "true":
            _fail(f"pre-push SHA {before!r} is not present in this checkout — "
                  "the weakening checks cannot be run against it (fail-closed; "
                  "ensure fetch-depth: 0)")
    if before and set(before) == {"0"}:
        # Branch CREATION: there is no prior state on this branch, so HEAD~1 is
        # this push's own earlier commit and a weakening hidden there would
        # compare against itself (round 21). The default branch is the state
        # that was actually reviewed before this branch existed.
        for ref in ("origin/main", "main"):
            if _resolve(ref):
                mb = subprocess.run(["git", "merge-base", ref, "HEAD"],
                                    cwd=ROOT, capture_output=True, text=True)
                if mb.returncode == 0 and mb.stdout.strip():
                    return mb.stdout.strip()
        if os.environ.get("GITHUB_ACTIONS") == "true":
            _fail("new branch and no resolvable default branch to compare "
                  "against — refusing to run the weakening checks against no "
                  "reference (fail-closed)")
    return _resolve("HEAD~1")


# HONEST LIMIT (panel rounds 11-13, recorded rather than papered over): every
# register below is writable by the same identity that writes the code it
# governs, so these checks RAISE THE COST of laundering a weakening — they
# force it to be explicit, attributable and diffable — but they cannot make it
# impossible. Only B-35 write separation (branch protection + CODEOWNERS
# review, an open operator action) removes the class. Do not read a green gate
# as proof that no weakening occurred; read it as proof that none occurred
# WITHOUT a recorded, reviewable decision.
def previous_version(rel_path: str) -> dict | None:
    """The comparison point's copy of a JSON artifact, or None if the file did
    not exist there (a genuinely new artifact has nothing to weaken)."""
    point = comparison_point()
    if not point:
        return None
    # Panel finding (PR #23 round 14): treating ANY git failure as "the file
    # did not exist" meant a shallow clone or a truncated history silently
    # disabled every weakening check. Absence and unreadability must be
    # distinguished: ls-tree answers presence, and it only fails when the
    # history itself cannot be read.
    # :(literal) — `--` stops option parsing but NOT pathspec magic, so a name
    # that is itself a pathspec would answer a different question than "does
    # this file exist here" (panel round 26, found in the panel script; these
    # paths are gate constants rather than attacker-supplied, but the property
    # should hold locally instead of resting on every caller).
    listed = subprocess.run(["git", "ls-tree", "--name-only", point, "--",
                             f":(literal){rel_path}"],
                            cwd=ROOT, capture_output=True, text=True)
    if listed.returncode != 0:
        _fail(f"cannot read history at the comparison point {point[:12]} for "
              f"{rel_path} ({listed.stderr.strip()[:200]}) — refusing to skip "
              "the weakening checks (fail-closed; ensure fetch-depth: 0)")
    if not listed.stdout.strip():
        return None                      # genuinely absent at that commit
    r = subprocess.run(["git", "show", f"{point}:{rel_path}"],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        _fail(f"{rel_path} is listed at {point[:12]} but unreadable — "
              "fail-closed rather than assume it is new")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        _fail(f"{rel_path} is unparseable at the comparison point — cannot "
              "verify whether this change weakens it")
    return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracked(rel: str) -> bool:
    """True when `rel` is a git-tracked path (exact, literal)."""
    r = subprocess.run(["git", "ls-files", "--error-unmatch", "--",
                        f":(literal){rel}"], cwd=ROOT,
                       capture_output=True, text=True)
    return r.returncode == 0


def _part_source_ok(part: dict) -> bool:
    """A manifest part's source is present, safe, in-repo, and hash-matched.

    R-02 (iter-3): the prior check did `ROOT / part['path']`, and pathlib
    DISCARDS ROOT when the right-hand side is absolute — so an absolute path to
    a matching-hash file OUTSIDE the repository reported the volume COMPLETE.
    A registered source must be a RELATIVE, traversal-free, repository-contained,
    git-tracked, regular (non-symlink) file whose bytes hash to the recorded
    64-hex sha."""
    path = part.get("path")
    sha = part.get("sha256")
    if not path or not isinstance(path, str):
        return False
    if os.path.isabs(path) or ".." in Path(path).parts:
        return False
    if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        return False
    fp = ROOT / path
    try:
        fp.resolve().relative_to(ROOT.resolve())          # containment
    except ValueError:
        return False
    if fp.is_symlink() or not fp.is_file():
        return False
    if not _tracked(path):
        return False
    return _sha256(fp) == sha


def _combined_mandate_failures(manifest: dict) -> list[str]:
    """Validate the concatenated mandate.md against the manifest (R-03).

    R-03 (iter-3): the manifest records `combined_mandate_sha256`, but
    compute_status never read governance/mandate.md, never checked that digest,
    and never proved the file is the concatenation of the attested parts. A
    stale/forged mandate.md — or one silently dropping a part's text — passed
    unnoticed. This reconstructs the concatenation the way §8 / the manifest's
    concatenation_rule specifies (header + each in-repo part verbatim, in
    parts[] order) and fails closed on any divergence.

    Returns a list of human-readable failure reasons (empty == OK). The header
    is NOT invented: only the property the rule guarantees is checked — every
    in-repo part's exact bytes appear in mandate.md, in manifest order, and the
    whole file's digest equals the attested combined sha."""
    reasons: list[str] = []
    sha = manifest.get("combined_mandate_sha256")
    if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        return ["governance/mandate/manifest.json combined_mandate_sha256 is "
                "missing or not a 64-hex digest — the concatenated mandate "
                "text must be attested, not asserted"]
    rel = "governance/mandate.md"
    fp = ROOT / rel
    try:
        fp.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return [f"{rel} escapes the repository root"]
    if fp.is_symlink() or not fp.is_file():
        return [f"{rel} is absent or not a regular file, but the manifest "
                "attests a combined_mandate_sha256 for it"]
    if not _tracked(rel):
        return [f"{rel} is not git-tracked — an untracked concatenation cannot "
                "be attested"]
    if _sha256(fp) != sha:
        reasons.append(f"{rel} sha256 does not match combined_mandate_sha256 in "
                       "the manifest — regenerate the concatenation and re-attest "
                       "(Article XIII)")
    # Reconstruction proof: each in-repo part's ACTUAL bytes must appear in
    # mandate.md, in the manifest's parts[] order (the concatenation order).
    # This is deliberately independent of the part's manifest sha256 (that is
    # R-02 / _part_source_ok's job, surfaced through volume status) so the two
    # checks don't double-count the same tamper; here we only need the part file
    # to be a safe, present, tracked repository source whose real content the
    # combined text actually carries.
    blob = fp.read_bytes()
    cursor = 0
    for part in manifest.get("parts", []):
        p_path = part.get("path")
        if not p_path:                       # NOT-IN-REPO part (e.g. Part 2)
            continue
        name = part.get("name")
        if os.path.isabs(p_path) or ".." in Path(p_path).parts:
            reasons.append(f"manifest part {name!r} path is absolute or escapes "
                           "the repo — refusing to reconstruct from it")
            continue
        pf = ROOT / p_path
        try:
            pf.resolve().relative_to(ROOT.resolve())
        except ValueError:
            reasons.append(f"manifest part {name!r} path escapes the repository")
            continue
        if pf.is_symlink() or not pf.is_file() or not _tracked(p_path):
            reasons.append(f"manifest part {name!r} source is absent, a symlink, "
                           "or untracked, so mandate.md cannot be proven to "
                           "contain it")
            continue
        idx = blob.find(pf.read_bytes(), cursor)
        if idx < 0:
            reasons.append(f"mandate.md does not contain part {name!r} verbatim "
                           "in concatenation order — the combined text is not the "
                           "attested parts (a part's text was dropped, reordered, "
                           "or altered)")
        else:
            cursor = idx + len(pf.read_bytes())
    return reasons


# ---------------------------------------------------------------- status ----


def compute_status() -> dict:
    catalogue = _load(AUDIT / "00-check-catalogue.json")
    findings = _load(AUDIT / "03-findings.json")
    accepted = _load(GOV / "accepted-residuals.json")
    manifest = _load(GOV / "mandate" / "manifest.json")

    cat_ids = [c["id"] for c in catalogue["checks"]]
    if len(cat_ids) != len(set(cat_ids)):
        _fail("check catalogue contains duplicate ids")
    if catalogue["registered_check_count"] != len(cat_ids):
        _fail("catalogue registered_check_count does not match its own checks")

    # The audit denominator is pinned by the manifest, not by the catalogue's
    # own self-report (adversarial audit 2026-07-25: catalogue+findings could
    # be shrunk as a matched pair to drop checks from the universe).
    required = manifest.get("required_check_ids")
    if not isinstance(required, list) or not required:
        _fail("governance/mandate/manifest.json has no required_check_ids — "
              "the audit denominator must be pinned, not inferred")
    prev_manifest = previous_version("governance/mandate/manifest.json")
    if prev_manifest is None and comparison_point():
        # Round-13 fixed this for the acceptance register but not here: a
        # register absent at the comparison point is only benign for a
        # genuinely new artifact, and the manifest is not new.
        print("[mandate-gate] note: no manifest at the comparison point — "
              "treating the pinned universe as newly established")
    if prev_manifest:
        prev_required = set(prev_manifest.get("required_check_ids") or [])
        prev_cat = previous_version("audit/00-check-catalogue.json")
        if prev_cat:
            prev_bands = {c["id"]: c.get("founding_band")
                          for c in (prev_cat.get("checks") or [])}
            for c in catalogue["checks"]:
                was = prev_bands.get(c["id"])
                now = c.get("founding_band")
                if not was or not now or was == now:
                    continue
                if ALL_BANDS.index(now) > ALL_BANDS.index(was):
                    _fail(f"{c['id']}'s FOUNDING band was softened in the "
                          f"catalogue ({was}->{now}) — the founding catalogue "
                          "is immutable (§9.9). Softening it silently "
                          "pre-downgrades every future finding on that check.")
        dropped = sorted(prev_required - set(required))
        if dropped:
            _fail(f"checks were REMOVED from the pinned universe: {dropped} — "
                  "the founding catalogue is immutable (§9.9). A check made "
                  "moot by architecture is retired to NOT-APPLICABLE with a "
                  "justification; it is never deleted. Refreshing the manifest "
                  "hash in the same change does not authorise a deletion.")
    if set(required) != set(cat_ids):
        missing = sorted(set(required) - set(cat_ids))
        extra = sorted(set(cat_ids) - set(required))
        _fail(f"catalogue does not match the manifest's pinned check universe "
              f"— missing: {missing} extra: {extra} (founding 119 are "
              "immutable; additions are Article XIII amendments)")

    f_ids = [f["id"] for f in findings]
    if len(f_ids) != len(set(f_ids)):
        _fail("findings contain duplicate ids")
    if set(f_ids) != set(cat_ids):
        missing = sorted(set(cat_ids) - set(f_ids))
        extra = sorted(set(f_ids) - set(cat_ids))
        _fail(f"findings/catalogue mismatch — missing: {missing} extra: {extra}")

    for f in findings:
        if f.get("verdict") not in CANONICAL_VERDICTS:
            _fail(f"{f.get('id', '<no id>')} has non-canonical verdict "
                  f"{f.get('verdict')!r} — legal values are "
                  f"{CANONICAL_VERDICTS}. A verdict the gate cannot read is a "
                  "finding it cannot gate (adversarial audit 2026-07-25).")
        if not isinstance(f.get("band"), str):
            _fail(f"{f.get('id', '<no id>')} has no string `band`")

    # §3/§5: a PASS with no standing control is PARTIAL, and per §5 the
    # control must be structured with a non-null `demonstrated` — a control
    # nobody has watched block something is a control being hoped about.
    for f in findings:
        if f["verdict"] == "PASS":
            sc = f.get("standing_control")

            def _substantive(v) -> bool:
                # bools/ints/lists are not descriptions (adversarial audit
                # 2026-07-25: {"mechanism": true} satisfied a truthiness test)
                return isinstance(v, str) and len(v.strip()) >= 12

            if not isinstance(sc, dict) or not _substantive(sc.get("mechanism")) \
                    or not _substantive(sc.get("demonstrated")):
                _fail(f"{f['id']} is PASS without a structured "
                      "standing_control carrying non-null mechanism and "
                      "demonstrated — per the mandate that verdict is "
                      "PARTIAL, always (§5)")
        if f["verdict"] == "NOT-APPLICABLE":
            # Bare truthiness accepted na_justification=true (round 16); §4
            # requires an argued justification referencing the architecture.
            j = f.get("na_justification")
            if not (isinstance(j, str) and len(j.strip()) >= 20):
                _fail(f"{f['id']} is NOT-APPLICABLE without a substantive "
                      "na_justification — §4 requires it to be ARGUED against "
                      "the actual architecture, never assumed")

    open_blockers: list[str] = []
    band_counts = {b: 0 for b in BLOCKER_BANDS}
    for f in findings:
        band = effective_band(f)
        if f["verdict"] in OPEN_VERDICTS and band in BLOCKER_BANDS:
            open_blockers.append(f["id"])
            band_counts[band] += 1

    # CRITICAL (own adversarial sweep, 2026-07-25): audit/03-findings.json —
    # the ONLY register that carries the verdicts and bands the blocker set is
    # computed from — was never compared against the comparison point. Its sole
    # defence was manifest findings_sha256, which the same commit legitimately
    # refreshes. Re-banding every open blocker down (or closing its verdict)
    # therefore erased all 32 blockers and flipped production_eligible to true
    # while the gate printed "status: OK". Reproduced end-to-end.
    prev_findings = previous_version("audit/03-findings.json")
    if prev_findings:
        by_id = {f["id"]: f for f in findings}
        prev_recs_by_id = {f["id"]: weakening_records(f.get("weakening_record"))
                           for f in prev_findings}
        # Replay detection over FULL history, the same mechanism the ratchet
        # and acceptance registers use (panel round 29). The one-step check
        # below compares against the comparison point only, so the sequence
        # "weaken with a record / re-tighten and delete the record / weaken
        # again re-adding the same record" passed: at the second weakening the
        # record is absent from the comparison point, hence 'fresh'. One
        # authorisation, spent twice.
        #
        # This is re-INTRODUCTION (present -> absent -> present), not "seen
        # anywhere before" — which is the rule that wrongly flags a record
        # legitimately persisting alongside the state it authorises, as it
        # flagged A-01 and all 32 founding acceptances when tried.
        historic_weak = reintroduced_fingerprints(
            "audit/03-findings.json",
            lambda d: [r for f in (d or [])
                       for r in weakening_records(f.get("weakening_record"))])
        for pf in prev_findings:
            cur = by_id.get(pf["id"])
            if cur is None:
                continue                      # deletions: pinned-universe check
            was = effective_band(pf)
            if was not in BLOCKER_BANDS or pf.get("verdict") not in OPEN_VERDICTS:
                continue
            now = effective_band(cur)
            weaker = ALL_BANDS.index(now) > ALL_BANDS.index(was)
            closed = cur["verdict"] not in OPEN_VERDICTS
            if not (weaker or closed):
                continue
            # BOTH a re-band and a verdict closure can happen in one change.
            # Taking whichever came first left the other unauthorised (panel
            # round 24), so every weakening that occurred needs its own record.
            required_changes = []
            if weaker:
                required_changes.append(f"{was}->{now}")
            if closed:
                required_changes.append(f"{pf['verdict']}->{cur['verdict']}")
            recs = weakening_records(cur.get("weakening_record"))
            priors = prev_recs_by_id.get(pf["id"], [])
            for change in required_changes:
                match = next((r for r in recs
                              if valid_weakening_record(r, change)), None)
                if match is None:
                    _fail(f"{pf['id']} was WEAKENED out of the blocker set "
                          f"({change}) with no structured `weakening_record` "
                          "(change, reason, authorised_by) — refreshing "
                          "findings_sha256 in the same change is not that "
                          "decision (§9.1). When a finding is both re-banded "
                          "AND closed, each transition needs its own record; "
                          "weakening_record may be a list.")
                if not is_fresh(match, priors):
                    _fail(f"{pf['id']} carries a weakening_record RETAINED "
                          f"from the comparison point for {change} — a record "
                          "authorises one change, not every later repeat of it")
                if record_fingerprint(match) in historic_weak:
                    _fail(f"{pf['id']} carries a weakening_record for {change} "
                          "that was REMOVED and later re-added verbatim — the "
                          "same authorisation cannot be spent on a second "
                          "weakening. Deleting a record during a re-tighten "
                          "and restoring it at the next downgrade defeats the "
                          "comparison-point check; record a new decision.")

    # CRITICAL 2: the catalogue carries the authoritative founding_band for all
    # 119 checks and was compared on ID SETS ONLY, so a band downgrade in the
    # findings left check_catalogue_sha256 valid and the two records silently
    # contradicting each other.
    cat_by_id = {c["id"]: c for c in catalogue["checks"]}
    for f in findings:
        founding = cat_by_id.get(f["id"], {}).get("founding_band")
        if not founding:
            continue
        if ALL_BANDS.index(effective_band(f)) > ALL_BANDS.index(founding):
            change = f"{founding}->{effective_band(f)}"
            if not any(valid_weakening_record(r, change)
                       for r in weakening_records(f.get("weakening_record"))):
                _fail(f"{f['id']} is banded {effective_band(f)} but the "
                      f"immutable catalogue records {founding} — a check's "
                      "founding band may not be softened in the findings "
                      "without a structured weakening_record (§9.9)")

    accepted_ids = set(accepted["accepted_open_findings"])
    prev_acc = previous_version("governance/accepted-residuals.json")
    # A register that does not exist at the comparison point makes EVERY entry
    # new (panel finding, round 13: prev=None skipped the check entirely, so a
    # freshly-introduced register could waive every open blocker with bare ids).
    # Shape is load-bearing: every reader below filters with
    # `isinstance(r, dict)`, so a register whose acceptance_records is itself a
    # MAPPING iterates its keys — plain strings — and every check silently sees
    # zero records. That reads as "no orphans, nothing to authorise" rather
    # than as the malformed register it is.
    #
    # Read the RAW value first (panel round 32): `... or []` normalises every
    # FALSY malformed value — {}, 0, "" — to an empty list before the shape
    # check can object, so exactly the values a tamper would use slipped past
    # the guard added to catch them.
    raw_records = accepted.get("acceptance_records")
    if raw_records is not None and not isinstance(raw_records, list):
        _fail("acceptance_records must be a LIST of records, not "
              f"{type(raw_records).__name__} — any other shape makes every "
              "per-record check vacuously pass")
    records = raw_records or []
    # ORPHAN PRUNE (round 17): a record for an id that is no longer accepted
    # must be removed. Retaining it let an id be closed and later RE-accepted
    # with the stale record satisfying the check — a second acceptance on a
    # first decision.
    #
    # UNCONDITIONAL (panel round 25): this was nested under `if newly`, so the
    # half of the exploit that CREATES the orphan — a change that only REMOVES
    # an acceptance id and keeps its record — never ran the check at all. The
    # prune is an invariant of the register, not a condition on new
    # acceptances, so it is evaluated whenever the register is read.
    orphans = sorted({r.get("finding_id") for r in records
                      if isinstance(r, dict)
                      and r.get("finding_id") not in accepted_ids})
    if orphans:
        _fail("acceptance_records exist for findings that are not "
              f"currently accepted: {orphans} — prune them. A retained "
              "record silently pre-authorises a future re-acceptance.")
    if prev_acc is not None or accepted_ids:
        newly = accepted_ids - set((prev_acc or {}).get("accepted_open_findings") or [])
        if newly:
            # Panel finding (PR #23 round 12): searching for the id anywhere in
            # _meta let a contributor self-accept by dropping the string into
            # any unrelated field. The record must be STRUCTURED and explicit.
            # FRESHNESS: the record authorising a NEW acceptance must itself be
            # new; one carried over from the comparison point is not a decision
            # about this change.
            prev_records = {r.get("finding_id")
                            for r in ((prev_acc or {}).get("acceptance_records") or [])
                            if isinstance(r, dict)}
            historic_acc = reintroduced_fingerprints(
                "governance/accepted-residuals.json",
                lambda d: d.get("acceptance_records", []))
            recorded = {r.get("finding_id") for r in records
                        if isinstance(r, dict)
                        and isinstance(r.get("reason"), str)
                        and len(r["reason"].strip()) >= 20
                        and isinstance(r.get("authorised_by"), str)
                        and r["authorised_by"].strip()
                        and r.get("finding_id") not in prev_records
                        and record_fingerprint(r) not in historic_acc}
            unrecorded = sorted(newly - recorded)
            if unrecorded:
                _fail("newly accepted blocker-band findings with no structured "
                      f"acceptance record: {unrecorded} — each needs an entry "
                      "in `acceptance_records` with finding_id, reason and "
                      "authorised_by. Accepting a blocker is an operator "
                      "decision; a matching manifest hash is not that "
                      "decision (§9.1)")
    unaccepted = sorted(set(open_blockers) - accepted_ids)
    if unaccepted:
        _fail(f"open blocker-band findings NOT in the accepted-residuals "
              f"register: {unaccepted} — a NEW blocker cannot be waved "
              "through; fix it or the operator must accept it by decision "
              "record in governance/accepted-residuals.json")
    stale = sorted(accepted_ids - set(open_blockers))
    if stale:
        _fail(f"accepted-residuals register lists findings that are no longer "
              f"open blockers: {stale} — prune the register (a closed "
              "acceptance left in place is a future bypass)")

    # STANDING INVARIANT, both directions (panel round 32). Round 25 made the
    # orphan prune unconditional — a RECORD whose id is no longer accepted
    # blocks — but left the mirror case conditional on `newly`: an id that was
    # already accepted at the comparison point could have its record DELETED
    # and nothing looked, because nothing was newly accepted. All 32 waived
    # blockers could lose their justification while the gate printed OK.
    #
    # An acceptance is a standing operator decision, so the record backing it
    # must exist for as long as the acceptance does — not merely at the moment
    # it is granted. Freshness (below) still governs NEW acceptances; this
    # governs every one of them, every run.
    undocumented = sorted(
        fid for fid in accepted_ids
        if not any(isinstance(r, dict)
                   and r.get("finding_id") == fid
                   and isinstance(r.get("reason"), str)
                   and len(r["reason"].strip()) >= 20
                   and isinstance(r.get("authorised_by"), str)
                   and r["authorised_by"].strip()
                   for r in records))
    if undocumented:
        _fail("accepted blocker-band findings whose acceptance_record is "
              f"missing or malformed: {undocumented} — an acceptance is a "
              "standing decision and its record must stand with it. Deleting "
              "the justification while keeping the waiver leaves an open "
              "blocker suppressed by nothing at all.")

    # Governance hash attestation (Article XI / B-35). The attested set
    # includes the gate's own authority files — the ratchet baselines and the
    # accepted-residuals register — because a register the measured thing can
    # hand-edit is a diary, not a gate (§9.8; tamper-test finding 2026-07-25).
    const_path = GOV / "constitution.md"
    const_hash = _sha256(const_path)
    if manifest["constitution_sha256"] != const_hash:
        _fail("constitution.md hash does not match governance/mandate/"
              "manifest.json — amendments must go through the gate and "
              "bump the attested hash (Article XIII)")
    # The concatenated mandate TEXT is now in-repo and attested: validate that
    # governance/mandate.md matches combined_mandate_sha256 AND is the verbatim
    # concatenation of the attested parts (R-03). Any in-repo part registered
    # in the manifest is proven present via _part_source_ok inside this check.
    mandate_fail = _combined_mandate_failures(manifest)
    if mandate_fail:
        _fail("mandate text attestation failed: " + "; ".join(mandate_fail))
    attested = {
        "findings_sha256": AUDIT / "03-findings.json",
        "check_catalogue_sha256": AUDIT / "00-check-catalogue.json",
        "ratchet_baselines_sha256": AUDIT / "ratchet-baselines.json",
        "accepted_residuals_sha256": GOV / "accepted-residuals.json",
    }
    for key, path in attested.items():
        if manifest.get(key) != _sha256(path):
            _fail(f"{path.relative_to(ROOT)} hash mismatch vs manifest — "
                  "changing this file is an Article XIII amendment: update "
                  f"{key} in governance/mandate/manifest.json in the same "
                  "change (loosening anything is automatically a finding)")

    evidenced = sum(1 for f in findings if f["verdict"] != "NO-EVIDENCE")
    pending = sorted(f["id"] for f in findings if f["verdict"] == "NO-EVIDENCE")

    def _volume_complete(tracks: tuple[str, ...]) -> str:
        # A volume is COMPLETE only when BOTH its evidence coverage AND its
        # mandate SOURCE TEXT are complete (panel review P0.1). The prior
        # version checked only that every check carried an evidence-backed
        # verdict, so Track C reported COMPLETE while its Part 2 instruction
        # text is absent (manifest part path/sha are null) — conflating
        # "we produced verdicts" with "the instructions we judged against are
        # present and attested". The text half is derived entirely from the
        # already-attested manifest: the volume's part must have a non-null
        # path, a tracked file, and a digest that matches the recorded sha.
        # (production_eligible remains separate; §8 requires both.)
        vol = [f for f in findings if f["id"].split("-")[0] in tracks]
        evidenced_ok = all(f["verdict"] != "NO-EVIDENCE" for f in vol)
        # POSITIVE ESTABLISHMENT (review OA-12-F01 / P0.6): the prior version
        # initialised text_ok=True and only cleared it while iterating matching
        # parts, so ZERO matching parts passed vacuously — a required volume
        # whose manifest registration was dropped or whose tracks were
        # misspelled would report COMPLETE with no text at all. Require exactly
        # the parts that claim this volume's tracks to be present and attested,
        # and require at least one.
        matching = [p for p in manifest.get("parts", [])
                    if set(p.get("tracks") or []) & set(tracks)]
        # R-01 (iter-3): require EXACT COVERAGE, not mere intersection. The
        # prior version accepted a part claiming tracks=["A"] as completing the
        # A/B volume, so Part 1 read COMPLETE with Track B's source unregistered.
        # Every requested track must be claimed by some present, attested part.
        covered = set().union(*[set(p.get("tracks") or []) for p in matching]) \
            if matching else set()
        text_ok = set(tracks) <= covered and all(
            _part_source_ok(p) for p in matching)
        return "COMPLETE" if (evidenced_ok and text_ok) else "IN_PROGRESS"

    part1_status = _volume_complete(("A", "B"))
    part2_status = _volume_complete(("C",))
    # R-04 (iter-3): production_eligible previously ignored volume completeness,
    # so a state with every finding evidenced and the constitution RATIFIED but
    # Part 2's Track C source text unregistered (part2_status IN_PROGRESS) could
    # read production_eligible=true — clearing traffic past checks whose mandate
    # text is not even present. §8 requires BOTH volumes complete before a
    # clearance is possible. This is a necessary (not sufficient) gate; the
    # open-blocker and evidence conditions still stand alongside it.
    production_eligible = (
        not open_blockers
        and evidenced == len(cat_ids)
        and accepted.get("constitution_state") == "RATIFIED"
        and part1_status == "COMPLETE"
        and part2_status == "COMPLETE"
    )
    return {
        "catalogue_version": catalogue["catalogue_version"],
        "registered_check_count": len(cat_ids),
        "active_check_count": len(cat_ids),
        "present_check_count": len(f_ids),
        "evidenced_check_count": evidenced,
        "pending_check_ids": pending,
        "part1_status": part1_status,
        "part2_status": part2_status,
        "phase": "STANDING_REGIME",
        "highest_open_band": next((b for b in BLOCKER_BANDS
                                   if band_counts[b]), "MUST-FIX-OR-BELOW"),
        "open_stop_ship_count": band_counts["STOP-SHIP"],
        "open_blocker_1_count": band_counts["BLOCKER-1"],
        "open_blocker_2_count": band_counts["BLOCKER-2"],
        "open_blocker_ids_accepted_by_operator": sorted(open_blockers),
        # Founding-engagement fact, not an ongoing clearance (review 1.3/3.5):
        # the security scope was audited at the July 2026 engagement (Track A
        # security checks A-08/A-13/A-26 etc. carry founding verdicts). It is
        # renamed to remove the misleading present-tense implication that the
        # security posture is continuously re-verified — it is NOT, and B-06
        # (unrotated credentials) remains an open STOP-SHIP in that scope.
        "security_scope_audited_at_founding": True,
        "constitution_state": accepted.get("constitution_state"),
        "production_eligible": production_eligible,
        "production_note": accepted.get("production_note", ""),
        "mandate_manifest_hash": _sha256(GOV / "mandate" / "manifest.json"),
        "constitution_hash": const_hash,
    }


def cmd_status(write: bool) -> None:
    status = compute_status()
    out = AUDIT / "engagement-status.json"
    rendered = json.dumps(status, indent=2, sort_keys=True) + "\n"
    if write:
        out.write_text(rendered)
        print(f"wrote {out.relative_to(ROOT)}")
        return
    if not out.exists():
        _fail("audit/engagement-status.json missing — run "
              "`mandate_gate.py status --write` and commit it")
    if out.read_text() != rendered:
        _fail("audit/engagement-status.json drifted from the computed state — "
              "regenerate with `status --write` and commit (the file is a "
              "computed property, never hand-edited)")
    print("status: OK — engagement state consistent; "
          f"production_eligible={status['production_eligible']} "
          f"(open blockers, operator-accepted: "
          f"{len(status['open_blocker_ids_accepted_by_operator'])})")


# --------------------------------------------------------------- ratchet ----


def _tracked_py_files() -> list[Path]:
    """Every git-tracked *.py file, at any depth (R-06).

    R-06 (iter-3): the ratchet counters iterated a hand-maintained glob list
    (`app/**/*.py`, `scripts/*.py`, `tests/**/*.py`). `ROOT.glob` expands `**`
    to include zero directories, so app/ and tests/ top files were covered — but
    `migrations/**/*.py` and `docs/harnesses/*.py` (11 tracked files) matched NO
    glob and escaped the emoji/noqa/type_ignore ratchets ENTIRELY. A new
    top-level package, a migration, or a harness could carry emojis or lint
    suppressions with the ceiling none the wiser — a silent fail-open (the
    literal marker is spelled out below to avoid self-matching). The denominator
    is now
    the AUTHORITATIVE tracked set from `git ls-files`, so coverage cannot drift
    as the tree grows (CLAUDE.md 'derive the set from git ls-files')."""
    r = subprocess.run(["git", "ls-files", "-z", "--", "*.py"],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        _fail("git ls-files failed while enumerating tracked *.py files for the "
              "ratchet denominator — refusing to measure a ceiling from an "
              "unknown file set: " + (r.stderr or "")[-300:])
    return [ROOT / rel for rel in r.stdout.split("\0") if rel]


def _count_grep(pattern: str) -> int:
    n = 0
    rx = re.compile(pattern)
    for p in _tracked_py_files():
        if p.is_file():
            for line in p.read_text(errors="replace").splitlines():
                if rx.search(line):
                    n += 1
    return n


_EMOJI_RX = re.compile("[\\U0001f300-\\U0001faff\\u2600-\\u27bf]")


def _emoji_count() -> int:
    # Article XIV alarm dilution: emojis are reserved for constitutional
    # alerts; any emoji in source dilutes the only unmissable signal. Scanned
    # over the full git-tracked *.py set (R-06) so no directory escapes the ban.
    n = 0
    for p in _tracked_py_files():
        n += len(_EMOJI_RX.findall(p.read_text(errors="replace")))
    return n


def measure_ratchets() -> dict[str, int]:
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True)
    # Panel finding (PR #23, first run that could actually SEE this file): the
    # return code was ignored, so a suite with collection/import errors still
    # printed "N tests collected" and greened the ratchet whenever N happened
    # to reach the floor — a broken suite reading as a healthy one.
    if collected.returncode != 0:
        _fail("pytest collection FAILED (rc="
              f"{collected.returncode}) — a suite that cannot be collected "
              "cannot measure a floor: "
              + (collected.stdout or collected.stderr or "")[-600:])
    if re.search(r"error(s)? during collection|^ERROR ", collected.stdout,
                 re.MULTILINE | re.IGNORECASE):
        _fail("pytest reported collection errors — refusing to measure a "
              "ratchet from a partially-collected suite")
    m = re.search(r"(\d+) tests? collected", collected.stdout)
    if not m:
        _fail("could not measure collected test count (pytest --collect-only)")
    return {
        "test_count_floor": int(m.group(1)),
        # The three source-suppression ceilings all scan the full git-tracked
        # *.py set (R-06); type_ignore previously also excluded tests/, another
        # coverage gap now closed.
        "noqa_ceiling": _count_grep(r"#\s*noqa"),
        "type_ignore_ceiling": _count_grep(r"#\s*type:\s*ignore"),
        "emoji_in_source_ceiling": _emoji_count(),
    }


def cmd_ratchet(measure_only: bool) -> None:
    current = measure_ratchets()
    if measure_only:
        print(json.dumps(current, indent=2))
        return
    # The baseline file is attested (tamper-test finding 2026-07-25: a
    # hand-loosened floor passed silently). Verify even when ratchet runs
    # standalone, not only via status.
    manifest = _load(GOV / "mandate" / "manifest.json")
    if manifest.get("ratchet_baselines_sha256") != _sha256(
            AUDIT / "ratchet-baselines.json"):
        _fail("audit/ratchet-baselines.json hash mismatch vs manifest — "
              "baseline changes are Article XIII amendments (update "
              "ratchet_baselines_sha256 in the same change; loosening is "
              "automatically a finding)")
    baselines = _load(AUDIT / "ratchet-baselines.json")
    prev = previous_version("audit/ratchet-baselines.json")
    if prev:
        records = baselines.get("_meta", {}).get("decision_records", []) or []
        prev_records = (prev.get("_meta", {}) or {}).get("decision_records", []) or []
        historic = reintroduced_fingerprints(
            "audit/ratchet-baselines.json",
            lambda d: (d.get("_meta", {}) or {}).get("decision_records", []))

        def _recorded(name, old, new) -> bool:
            # The record must match THIS change, not merely mention the metric
            # once (own-test finding while fixing round 11: a single historic
            # record would otherwise license every future loosening of it).
            for r in records:
                if not isinstance(r, dict) or r.get("metric") != name:
                    continue
                # Substring matching let "120 -> 210" authorise an actual
                # 20 -> 21 loosening (round 21). transition_matches parses the
                # transition and compares the TOKENS exactly — module-level so
                # the test drives the SAME parser (review P1.5-c).
                if not transition_matches(str(r.get("change", "")), old, new):
                    continue
                reason = str(r.get("reason", "")).strip()
                who = str(r.get("authorised_by", "")).strip()
                # Structured and attributable, not a bare string (round 13):
                # a fieldless record was enough to license a loosening.
                # §9.1: "the loosening is itself a finding" — the record must
                # declare it (round 17: a well-formed record without
                # is_finding satisfied the gate while violating the rule).
                if (len(reason) >= 20 and who
                        and r.get("is_finding") is True
                        and record_fingerprint(r) not in historic
                        and is_fresh(r, prev_records)):
                    return True
            return False

        for name, old in (prev.get("ratchets") or {}).items():
            new = baselines["ratchets"].get(name)
            if new is None:
                _fail(f"ratchet {name} was REMOVED from the register — "
                      "deleting a ratchet is the strongest possible loosening")
            weaker = (new < old) if name.endswith("_floor") else (new > old)
            if weaker and not _recorded(name, old, new):
                _fail(f"ratchet {name} was LOOSENED ({old} -> {new}) with no "
                      "decision record naming it in _meta.decision_records — "
                      "per §9.1 a loosening requires a recorded decision AND "
                      "is automatically a finding. Updating the manifest hash "
                      "in the same change does not substitute for one.")
    errors = []
    for name, base in baselines["ratchets"].items():
        cur = current.get(name)
        if cur is None:
            errors.append(f"ratchet {name} has a baseline but no measurement")
        elif name.endswith("_floor") and cur < base:
            errors.append(f"{name}: {cur} < floor {base} (may not fall)")
        elif name.endswith("_ceiling") and cur > base:
            errors.append(f"{name}: {cur} > ceiling {base} (may not rise)")
    if errors:
        _fail("ratchet regression — " + "; ".join(errors) +
              ". Tightening is a normal change; loosening requires a decision "
              "record in the baseline file AND is automatically a finding "
              "(§9.1). Floors that improved should be re-baselined upward.")
    print(f"ratchet: OK — {len(baselines['ratchets'])} ratchets hold "
          f"(current: {current})")


# ------------------------------------------------------------- calibrate ----

_UUID_LIT = re.compile(
    r"""=\s*["'][0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"""
    r"""[0-9a-fA-F]{4}-[0-9a-fA-F]{12}["']""")
# Name denylist widened after the 2026-07-25 adversarial audit (auth/bearer/
# pat/cookie/session/credential were invisible); value shapes now also cover
# hyphen-less 32-hex and long base64-ish literals in any assignment.
_CRED_NAME = re.compile(
    r"(token|api[-_]?key|secret|passwd|password|auth|bearer|"
    r"credential|session|cookie)", re.IGNORECASE)
# NB: no "pat" alternative — it matched path_note etc; real PATs (ghp_…) are
# caught by the entropy check below instead.
# Value-shape detection only on an ASSIGNMENT right-hand side, and only for
# literals with real credential entropy — a long CamelCase XBRL tag or an SQL
# fragment is not a secret (false positives found while hardening, 2026-07-25).
_ASSIGN_RHS = re.compile(r"""=\s*["']([^"']{16,})["']""")


def _looks_random(v: str) -> bool:
    if " " in v or "/" in v.strip("/") and "." in v:
        return False
    hexish = re.fullmatch(r"[0-9a-fA-F]{32,}", v)
    if hexish:
        return True
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]{24,}", v):
        return False
    # require mixed case AND digits: entropy, not an identifier
    return (any(c.isdigit() for c in v) and any(c.islower() for c in v)
            and any(c.isupper() for c in v))


def scan_credential_shapes(paths) -> list[str]:
    """The gate detect-secrets misses (calibration finding, 2026-07-25):
    `token` is not in its keyword denylist and hyphenated UUIDs defeat both
    entropy detectors — and UUID tokens are exactly this app's real credential
    format.

    AST-based for the credential-NAMED assignment path (panel review P1.2):
    the old regex required the credential name to sit immediately before `=`,
    so a PEP 526 annotated assignment — `api_key: str = "…"`, the form this
    codebase actually uses — slipped past it because the `: str ` annotation
    broke the adjacency. The AST sees the target name and the value literal
    regardless of annotation, covers Assign / AnnAssign / walrus, and FAILS
    CLOSED on a file that will not parse (an unscannable file is a failed
    scan, not a clean one). The value-shape detectors (exact UUID, high
    entropy) stay line-based and name-independent, so a secret with no
    credential-ish variable name is still caught anywhere it appears.
    `pragma: allowlist secret` on the line is honoured for reviewed
    placeholders (e.g. the fail-closed admin-key default)."""
    hits = []
    for p in paths:
        text = p.read_text(errors="replace")
        lines = text.splitlines()
        flagged: set[int] = set()
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            hits.append(f"{p}: cannot parse ({exc.msg}) — credential scan "
                        "fails closed rather than skipping an unreadable file")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = ([node.target] if node.target else []), node.value
            elif isinstance(node, ast.NamedExpr):
                targets, value = [node.target], node.value
            else:
                continue
            if not (isinstance(value, ast.Constant)
                    and isinstance(value.value, (str, bytes))
                    and len(value.value) >= 8):
                continue
            for t in targets:
                name = getattr(t, "id", None) or getattr(t, "attr", "")
                if _CRED_NAME.search(name):
                    flagged.add(node.lineno)
        for i, line in enumerate(lines, 1):
            rhs = _ASSIGN_RHS.search(line)
            if _UUID_LIT.search(line) or (rhs and _looks_random(rhs.group(1))):
                flagged.add(i)
        for i in sorted(flagged):
            line = lines[i - 1] if i - 1 < len(lines) else ""
            if "allowlist secret" in line:
                continue
            hits.append(f"{p}:{i}: {line.strip()[:80]}")
    return hits


def _ruff_root_config() -> tuple[Path | None, str]:
    """The explicit Ruff configuration ROOTED IN THIS REPOSITORY, or None+why.

    CI regression (2026-07-26, Python 3.12 job): `_ruff_s110_live` asked ruff
    whether S110 fires and took the bare answer at face value, so with NO
    repository config the verdict came from ruff's BUILT-IN DEFAULTS — which
    differ by ruff version (locally S110 did not fire, on the CI runner it did,
    so a test asserting the absent-config case passed locally and failed in CI)
    and could equally come from a config file in a PARENT directory that the
    repository does not contain. A control whose verdict is owned by the tool's
    defaults, or by a file outside the repository, is not a repository control
    at all. So an explicit config at ROOT is now REQUIRED, and its absence is
    reported rather than silently resolved by whatever ruff would have used.

    Discovery mirrors ruff's own precedence (`.ruff.toml`, then `ruff.toml`,
    then `pyproject.toml` carrying a `[tool.ruff]` TABLE) but is deliberately
    NOT recursive: a parent-only config is REFUSED, never inherited. Malformed
    TOML and a pyproject without `[tool.ruff]` are refusals too — both leave the
    rule set undetermined, which is the fail-closed direction."""
    import tomllib
    for name in (".ruff.toml", "ruff.toml"):
        fp = ROOT / name
        if fp.is_file():
            try:
                tomllib.loads(fp.read_text(errors="replace"))
            except tomllib.TOMLDecodeError as exc:
                return None, (f"{name} at the repository root is malformed TOML "
                              f"({exc}) — the effective rule set is undetermined")
            return fp, ""
    pp = ROOT / "pyproject.toml"
    if not pp.is_file():
        return None, ("no .ruff.toml, ruff.toml or pyproject.toml at the "
                      "repository root (a config in a PARENT directory is "
                      "deliberately NOT inherited — it is not part of this "
                      "repository and cannot be one of its controls)")
    try:
        cfg = tomllib.loads(pp.read_text(errors="replace"))
    except tomllib.TOMLDecodeError as exc:
        return None, (f"pyproject.toml at the repository root is malformed TOML "
                      f"({exc}) — the effective rule set is undetermined")
    tool = cfg.get("tool")
    if not (isinstance(tool, dict) and isinstance(tool.get("ruff"), dict)):
        return None, ("pyproject.toml at the repository root carries no "
                      "[tool.ruff] table, so this repository declares no Ruff "
                      "configuration of its own")
    return pp, ""


def _ruff_s110_live() -> tuple[bool, str]:
    """True when the repo's OWN, fully-merged ruff config makes S110 fire on
    the app/ production surface (P1-01, review OA-12-F02 / P1.3).

    Runs the real ruff with the REPOSITORY config (no `--isolated`) against a
    swallowed-exception snippet fed on stdin under an app/-SHAPED filename, so
    the exact per-file rules that apply to `app/*.py` apply here. This replaces
    a pyproject `select`/`ignore` PARSER that read only those two keys and so
    FALSE-BLOCKED a config expressing S110 via `extend-select` (and was blind to
    `extend-ignore`, per-file-ignores and `exclude`). Because it evaluates
    ruff's merged config, deleting 'S', ignoring S110, adding an `app/**`
    per-file-ignore, or excluding app/ all make the diagnostic vanish and the
    check fail.

    Fails CLOSED on every uncertainty, and each in its own right: no explicit
    repository config (`_ruff_root_config`), a ruff that could not run, and any
    result that is not a real S110 DIAGNOSTIC all read as "not live" — a dead
    control, never a catch. Returns (fires, detail-for-the-failure-message)."""
    cfg, why = _ruff_root_config()
    if cfg is None:
        return False, ("no explicit Ruff configuration rooted in this "
                       f"repository: {why} — an S110 verdict resting on ruff's "
                       "built-in defaults, or on a config file the repository "
                       "does not contain, is not a deliberate repository "
                       "control and must never read as one")
    probe = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--force-exclude",
         "--output-format", "json",
         "--stdin-filename", "app/_s110_probe.py", "-"],
        input="def f():\n    try:\n        g()\n    except Exception:\n"
              "        pass\n",
        capture_output=True, text=True, cwd=ROOT)
    # An actual S110 DIAGNOSTIC, not the substring anywhere in the output. A
    # ruff that FAILS to run (exit 2 — e.g. an unknown rule selector) prints the
    # offending rule NAME to stderr and nothing to stdout, so the previous
    # `"S110" in stdout + stderr` test could read "S110" straight out of an
    # error message and report a DEAD control as live. Structured output makes
    # a diagnostic the only thing that can satisfy the check, and an
    # unparseable/absent result fails closed.
    try:
        diags = json.loads(probe.stdout)
    except (json.JSONDecodeError, TypeError):
        return False, ("ruff emitted no parseable JSON diagnostics — the "
                       f"probe could not run (exit={probe.returncode}, "
                       f"err={probe.stderr.strip()[:160]!r})")
    fires = any(isinstance(d, dict) and d.get("code") == "S110" for d in diags)
    detail = (f"under {cfg.name}: a per-file-ignore, extend-ignore, or an app/ "
              f"exclusion has silenced the CI gate; exit={probe.returncode}, "
              f"err={probe.stderr.strip()[:120]!r}")
    return fires, detail


def find_vacuous_test_asserts(paths) -> list[str]:
    """`assert True`-style assertions in tests: pass unconditionally, so the
    test tests nothing (seeded-defect class 4; ruff's S101 exempts tests/)."""
    rx = re.compile(r"^\s*assert\s+(True|1|\"[^\"]+\"|'[^']+')\s*(#.*)?$")
    hits = []
    for p in paths:
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if rx.match(line):
                hits.append(f"{p}:{i}")
    return hits


def collect_imports(paths) -> set[str]:
    """Top-level module names imported by real import statements (AST, not
    regex: docstring prose like 'from Stooq' was being read as an import)."""
    import ast
    names: set[str] = set()
    for p in paths:
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names - {"app", "tests"}


def declared_dependencies() -> set[str]:
    """Distribution names from pyproject's dependency ARRAYS only.

    Panel finding (PR #23 round 11): scanning the whole file for any quoted
    bare token treated the project name, descriptions, classifiers and URLs
    as "declared", so a hallucinated import sharing one of those names was
    silently accepted as real."""
    try:
        import tomllib
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(errors="replace"))
    except Exception as exc:                      # unreadable pyproject: fail closed
        _fail(f"cannot parse pyproject.toml for the dependency check: {exc}")
    specs: list[str] = []
    project = data.get("project") or {}
    specs += list(project.get("dependencies") or [])
    for extra in (project.get("optional-dependencies") or {}).values():
        specs += list(extra)
    out = set()
    for spec in specs:
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9._-]*)", str(spec))
        if m:
            out.add(m.group(1).lower().replace("_", "-"))
    return out


def unresolvable_imports(module_names) -> list[str]:
    import importlib.util
    return [m for m in module_names
            if importlib.util.find_spec(m) is None]


def cmd_calibrate() -> None:
    """Six-class corpus from audit/02-calibration.md, §9.3: prove the gates
    still catch what they exist to catch. A gate that cannot run fails."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)

        # 1 — hard-coded credential, in this app's REAL credential shape
        cred = tdir / "seeded_cred.py"
        cred.write_text(
            'sipgate_token = "4ed251b5-8a9c-4b3e'  # pragma: allowlist secret -- seeded fixture
            '-9f21-7c6d5e4a3b21"\n')
        if not scan_credential_shapes([cred]):
            failures.append("class 1 (uuid credential) NOT caught by the "
                            "credential-shape scanner")
        # and the scanner must be clean on the actual repo — over EVERY
        # TRACKED .py file (panel round 25 + the follow-on sweep).
        #
        # This was a hand-listed set of trees (app/, scripts/), which is the
        # "which directories did we remember" failure mode: tests/ was missing
        # and is the one tree where nothing else looks at all — pyproject gives
        # tests/** a blanket ruff-S per-file-ignore, so S105/S106 never fire,
        # and detect-secrets has the documented UUID-token blind spot that this
        # very class exists to cover. migrations/ and docs/harnesses/ were
        # missing too. Deriving the set from `git ls-files` retires the whole
        # class: a new top-level package is covered the day it is added.
        # Deliberate throwaway fixtures stay legal by carrying an explicit
        # `pragma: allowlist secret` on the line — a visible, reviewable
        # opt-out rather than a silent tree-wide exemption.
        listing = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=ROOT,
                                 capture_output=True, text=True)
        if listing.returncode != 0:
            failures.append("git ls-files failed — the credential scan cannot "
                            "establish which files to scan, and an unknown "
                            "denominator is a failed check, not a clean one")
            repo_py = []
        else:
            repo_py = [ROOT / f for f in listing.stdout.split("\0") if f]
            if not repo_py:
                failures.append("git ls-files returned no Python files — "
                                "refusing to report a clean credential scan "
                                "over an empty file set")
        live = scan_credential_shapes(repo_py)
        if live:
            failures.append("credential-shape scanner fired on live source: "
                            + "; ".join(live[:5]))

        # 2 — swallowed exception -> ruff S110 (the CI lint gate)
        swallow = tdir / "seeded_swallow.py"
        swallow.write_text("def f():\n    try:\n        g()\n"
                           "    except Exception:\n        pass\n")
        r = subprocess.run([sys.executable, "-m", "ruff", "check",
                            "--select", "S110", "--isolated", str(swallow)],
                           capture_output=True, text=True)
        # Fail-closed BOTH ways (self-audit finding): exit 0 means the defect
        # slipped through, but a nonzero exit with no S110 in the output means
        # ruff itself failed to run — which must read as a failed gate, never
        # as a catch.
        if r.returncode == 0 or "S110" not in (r.stdout + r.stderr):
            failures.append("class 2 (swallowed exception) NOT caught by ruff "
                            f"S110 (exit={r.returncode})")
        # ...and the REPO's own config must still make S110 LIVE on the app/
        # production surface. A LIVE-CONFIG probe (review OA-12-F02 / P1.3, and
        # P1-01) is the authoritative way to prove this: it runs the real ruff
        # with the REPO config (no --isolated) against an app/-SHAPED filename
        # via --stdin-filename, so the exact per-file rules that apply to
        # app/*.py apply here. It supersedes an earlier pyproject `select`/
        # `ignore` PARSER: that parser read only `select`/`ignore` and so
        # FALSE-BLOCKED a config expressing the same rule via `extend-select`
        # (and was blind to extend-ignore, per-file-ignores, and exclude). The
        # probe evaluates ruff's real, fully-merged config, covering every one
        # of those forms — deleting 'S', ignoring S110, adding an app/**
        # per-file-ignore, or excluding app/ all make the diagnostic vanish and
        # fail this check.
        # Fail closed both ways: S110 present under the live config is the only
        # pass; its absence (suppressed, excluded, or ruff errored) is a dead
        # control, never a silent catch.
        fires, detail = _ruff_s110_live()
        if not fires:
            failures.append(
                "class 2 live-config probe: ruff under the repository's own "
                "config does NOT flag S110 on an app/-shaped swallowed "
                f"exception ({detail})")

        # 3 — non-existent dependency -> import-resolution check
        if unresolvable_imports(["reqwests_http"]) != ["reqwests_http"]:
            failures.append("class 3 (hallucinated package) NOT caught by the "
                            "import-resolution check")
        # and every real top-level app import must resolve (the standing gate)
        top = collect_imports(ROOT.glob("app/**/*.py"))
        declared = declared_dependencies()
        # A module that is DECLARED in pyproject but absent here is an
        # environment gap (optional/heavy deps), not a hallucination; an
        # UNDECLARED unresolvable import is the seeded-class-3 signal.
        missing = [m for m in unresolvable_imports(sorted(top))
                   if m.lower().replace("_", "-") not in declared]
        if missing:
            failures.append("live app imports neither resolvable nor declared "
                            f"in pyproject: {missing}")

        # 4 — vacuous assertion in a test
        vac = tdir / "test_seeded_vac.py"
        vac.write_text("def test_something():\n    assert True\n")
        if not find_vacuous_test_asserts([vac]):
            failures.append("class 4 (vacuous assertion) NOT caught")
        live_vac = find_vacuous_test_asserts(list(ROOT.glob("tests/**/*.py")))
        if live_vac:
            failures.append("vacuous assertions in the live suite: "
                            + "; ".join(live_vac[:5]))

    # 5 — untrusted text -> tool call: N/A by structure; re-validate (§9.9):
    # the LLM judgment path must pass NO tools to the API.
    # Class 5 is checked on the AST, not the text: a source scan fires on
    # prose (this repo's own comment about the invariant tripped it) and can
    # be hidden by string-building. The AST sees real kwargs and real dict
    # keys only. The complementary ban on ** expansion at model call sites
    # is what makes this complete — with no dynamic kwargs, every tool-enabling
    # form is a literal the AST can see (round 22).
    import ast as _ast

    # tool_enabling, the tenancy detectors, and const_str now live at module
    # level (panel rounds 27/33 + review P1.5-c) so this check and its tests
    # exercise the same code. Alias kept for the closures below.
    _const_str = const_str

    _LLM_SDKS = ("anthropic", "openai")
    # A module reaches a model either through a vendor SDK or by addressing the
    # endpoint directly over HTTP. Scoping by import alone (round 27) covered
    # only the first: a module using httpx against api.anthropic.com is exactly
    # as capable of enabling tools and imported no SDK, so the ban did not
    # apply to it (panel round 29). Endpoint hosts and paths are the other half
    # of "can reach a model".
    _LLM_ENDPOINTS = ("api.anthropic.com", "api.openai.com",
                      "/v1/messages", "/v1/complete", "/v1/chat/completions",
                      "/v1/responses", "anthropic.com/v1", "openai.com/v1")

    def _reaches_a_model(tree) -> bool:
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                if any(a.name.split(".")[0] in _LLM_SDKS for a in node.names):
                    return True
            if isinstance(node, _ast.ImportFrom):
                if (node.module or "").split(".")[0] in _LLM_SDKS:
                    return True
            # any literal naming a model endpoint, however it is assembled
            if isinstance(node, (_ast.Constant, _ast.BinOp, _ast.JoinedStr)):
                s = _const_str(node)
                if s and any(e in s for e in _LLM_ENDPOINTS):
                    return True
        return False

    for path in ROOT.glob("app/**/*.py"):
        try:
            tree = _ast.parse(path.read_text(errors="replace"))
        except SyntaxError:
            failures.append(f"{path.relative_to(ROOT)} does not parse — the "
                            "no-tool invariant cannot be checked")
            continue
        if tool_enabling(tree):
            failures.append(
                f"class 5 N/A no longer holds: {path.relative_to(ROOT)} passes "
                "a `tools` argument or key — the no-tool-call architecture "
                "assumption is void; re-run A-10/C-06/C-07")

        # Both DYNAMIC-ARGUMENT bans below are scoped to modules that can REACH
        # a model. A **kwargs expansion or a computed mapping key is only a
        # tool-enabling risk when the call actually goes to a model (panel
        # round 33): the **kwargs ban keyed on method NAMES (post/put/...), so
        # it fired on every HTTP client in the app — an ordinary
        # `httpx.post(url, **opts)` to a price feed falsely froze releases.
        # Dynamic mapping writes are likewise ordinary in 16 non-model modules;
        # a check that fires on all of them is one the operator waves through.
        # The `tools` LITERAL ban above still covers app/ in full — only these
        # two dynamic-construction bans need the narrower scope.
        #
        # STATED LIMIT (do not read these as complete): a module addressing the
        # endpoint purely through config (no SDK import, no literal host), or
        # one that computes an argument here and hands it to a different module
        # that makes the call, is outside them — both need dataflow the AST
        # pass does not do. Recorded in audit/10.
        if not _reaches_a_model(tree):
            continue

        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, _ast.Attribute) else getattr(fn, "id", "")
            # The SDK entry points plus the raw-HTTP forms underneath them
            # (round 25): the ban only covered the SDK names, so the same
            # dynamic kwarg expanded into client.post(...)/request(...) —
            # the transport the SDK itself uses — was unrestricted.
            if name not in ("create", "stream", "generate", "complete",
                            "post", "put", "patch", "request", "send"):
                continue
            if any(k.arg is None for k in node.keywords):
                failures.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} expands **kwargs "
                    f"into a model call ({name}) — forbidden, because a "
                    "dynamically-built kwarg can enable tool use without any "
                    "literal appearing in source. Pass keywords explicitly so "
                    "the no-tool invariant stays checkable.")

        # Constant folding closes `"to" + "ols"`, but not `"".join([...])` or
        # chr() arithmetic, and enumerating obfuscations is the losing game
        # this check keeps playing. So in the module that actually talks to a
        # model, every mapping key must be a visible literal: you may READ
        # x[i], you may not WRITE a key you computed. A key the AST cannot
        # read is not a key anyone reviewing this file can read either.
        for node in _ast.walk(tree):
            dynamic = None
            if isinstance(node, (_ast.Assign, _ast.AugAssign)):
                targets = (node.targets if isinstance(node, _ast.Assign)
                           else [node.target])
                for tgt in targets:
                    if (isinstance(tgt, _ast.Subscript)
                            and _const_str(tgt.slice) is None
                            and not isinstance(tgt.slice, _ast.Constant)):
                        dynamic = "subscript assignment"
            if isinstance(node, _ast.Dict):
                for k in node.keys:
                    if (k is not None and _const_str(k) is None
                            and not isinstance(k, _ast.Constant)):
                        dynamic = "dict key"
            if dynamic:
                failures.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} builds a mapping "
                    f"key dynamically ({dynamic}) in a module that calls a "
                    "model API — forbidden, because a computed key can name "
                    "`tools` without any readable literal, which makes the "
                    "no-tool invariant uncheckable rather than false")

    # 6 — cross-tenant ownership: N/A by structure; re-validate: still no
    # per-user tables (single-tenant).
    #
    # EVERY ORM-bearing module, not just app/models.py (panel round 25): the
    # claim is "no per-user TABLES anywhere", but the scan read one file, so a
    # tenancy model declared in any other app module — app/auth.py, a
    # models/ package, anywhere — preserved the N/A verdict untouched. Scope
    # is now "declares ORM structure" rather than "is named models.py"; that
    # keeps prose and plain helpers out (they cannot define a table) while
    # closing the file-location escape.
    # Tenancy detectors are module-level (declares_strong_tenancy /
    # has_orm_markers / declares_tenancy_column) so the tests exercise the same
    # regexes this loop does (review P1.5-c). migrations/ is in scope because a
    # migration is where a table is actually created; a STRONG signal (raw-SQL
    # CREATE TABLE, create_table(), or a tenancy class) stands alone, while the
    # weak <entity>_id column signal needs ORM context to avoid false positives.
    _tenancy_scope = sorted(ROOT.glob("app/**/*.py")) + \
        sorted(ROOT.glob("migrations/**/*.py"))
    for path in _tenancy_scope:
        src = path.read_text(errors="replace")
        rel = path.relative_to(ROOT)
        if declares_strong_tenancy(src):
            failures.append("class 6 N/A no longer holds: a per-user/tenant "
                            f"TABLE is declared or created in {rel} — "
                            "re-run C-01")
            continue
        if not has_orm_markers(src):
            continue
        if declares_tenancy_column(src):
            failures.append("class 6 N/A no longer holds: per-user/tenant "
                            f"tables appeared in {rel} — re-run C-01")

    if failures:
        _fail("seeded-defect calibration (S12) — " + " | ".join(failures) +
              " — a gate that stopped catching its seeded defect is a FAILED "
              "gate and freezes releases (§9.3)")
    print("calibrate: OK — 4 seedable classes caught; 2 N/A classes "
          "structurally re-validated; live source clean")


# --------------------------------------------------------------- surface ----


def cmd_surface(check: bool = False) -> None:
    """Regenerate the audit surface, or (check=True) prove the committed copy
    still matches it.

    The surface is the audit DENOMINATOR — every coverage claim divides by
    `files_tracked`. Nothing verified it, and the repo consequently carried a
    copy reading `files_tracked: 2` with two fixture module names, leaked from
    a test that patched ROOT before the tests were made hermetic. A stale
    denominator does not fail loudly; it makes partial coverage look total.
    So it is now a computed property with a drift check, exactly like
    audit/engagement-status.json."""
    # -z and an rc check (round 23): whitespace splitting mangled paths with
    # spaces and a git failure produced an EMPTY audit denominator that still
    # reported success.
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                             capture_output=True, text=True)
    if listing.returncode != 0:
        _fail(f"git ls-files failed ({listing.stderr.strip()[:200]}) — refusing "
              "to write an audit surface from an unknown file set")
    tracked = [f for f in listing.stdout.split("\0") if f]
    if not tracked:
        _fail("git ls-files returned no files — an empty audit denominator "
              "would make every coverage claim vacuous")
    # Every HTTP method, any decorator object, anywhere under app/ (panel
    # round 27). The pattern was `@router.(get|post|put|delete)` under
    # app/routers/ only, so a PATCH endpoint, a HEAD/OPTIONS handler, an
    # @app.get in main.py or a router defined outside that one directory was
    # live traffic the audit denominator did not know existed. The methods
    # come from the ASGI verb set rather than a remembered subset.
    # The decorator target may be any dotted expression (panel round 29):
    # `@api.router.get(...)` is an ordinary FastAPI form and `@(\w+)` matched
    # only a bare name, so those endpoints were live traffic missing from the
    # denominator.
    _VERBS = {"get", "post", "put", "delete", "patch",
              "head", "options", "trace", "api_route"}
    # EFFECTIVE mounted paths, not decorator-local ones (review 1.7/3.8): the
    # surface recorded score's routes as "" and "/history" when they mount at
    # /api/v1/score and /api/v1/score/history. Derive each module's router
    # prefix statically from `APIRouter(prefix="…")` and prepend it. This app
    # adds no further prefix at include_router() time; if one is ever added,
    # the guard below fails closed rather than silently understating the path.
    _prefix_by_module: dict[str, str] = {}
    for p in sorted(ROOT.glob("app/**/*.py")):
        m = re.search(r"""APIRouter\([^)]*prefix\s*=\s*(["'])([^"']*)\1""",
                      p.read_text(errors="replace"))
        if m:
            _prefix_by_module[str(p.relative_to(ROOT))] = m.group(2)
    main_src = (ROOT / "app" / "main.py").read_text(errors="replace") \
        if (ROOT / "app" / "main.py").exists() else ""
    if re.search(r"include_router\([^)]*\bprefix\s*=", main_src):
        _fail("app/main.py mounts a router with an include_router(prefix=…) "
              "the surface generator does not compose — the recorded route "
              "paths would understate the real API; extend the prefix "
              "derivation before regenerating the surface")
    routes = []
    for p in sorted(ROOT.glob("app/**/*.py")):
        # PARSED, not pattern-matched. Every regex here has been wrong in a
        # different way: positional-only missed `path=`; anchoring on the
        # paren missed `path=` after another kwarg; a fixed character window
        # read the NEXT decorator's `path=` and mis-assigned it; and requiring
        # one-or-more characters skipped `@router.get("")` -- a real endpoint
        # mounted at the router prefix -- then captured garbage from the
        # following quote. The decorator is Python, so the parser answers all
        # of it exactly, including multi-line forms.
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except SyntaxError as exc:
            _fail(f"{p.relative_to(ROOT)} does not parse ({exc}) — refusing to "
                  "write a route inventory that silently omits its endpoints")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                fn = dec.func
                if not isinstance(fn, ast.Attribute) or fn.attr not in _VERBS:
                    continue
                rel = p.relative_to(ROOT)
                # The path is the `path=` keyword or the first positional, and
                # may be composed from literals — `PREFIX + "/x"` is a real
                # endpoint (panel round 33). Fold it; a value that will not fold
                # to a literal (a bare Name, a call) is one this scan cannot
                # read, and a route it cannot read must FAIL, not be silently
                # dropped from the denominator — the exact fail-open the
                # positional-only version had.
                path_node = None
                for kw in dec.keywords:
                    if kw.arg == "path":
                        path_node = kw.value
                if path_node is None and dec.args:
                    path_node = dec.args[0]
                if path_node is None:
                    _fail(f"{rel}:{dec.lineno} @{fn.attr} has no path argument "
                          "— cannot record the endpoint; refusing a surface "
                          "that silently omits it")
                route = const_str(path_node)
                if route is None:
                    _fail(f"{rel}:{dec.lineno} @{fn.attr} route path is not a "
                          "readable literal (it is computed at runtime) — the "
                          "audit surface cannot record an endpoint it cannot "
                          "read; make the path a literal or extend the folder")
                # api_route declares its verbs in methods=[...]; collapsing it
                # to one "ANY" entry understated the method-level surface (panel
                # round 33). Emit one route per declared method; FastAPI
                # defaults to GET when methods= is absent. A non-literal
                # methods= is unreadable and fails closed for the same reason.
                if fn.attr == "api_route":
                    methods_node = next(
                        (kw.value for kw in dec.keywords if kw.arg == "methods"),
                        None)
                    if methods_node is None:
                        methods = ["GET"]
                    elif isinstance(methods_node, (ast.List, ast.Tuple, ast.Set)):
                        methods = [const_str(e) for e in methods_node.elts]
                        if any(m is None for m in methods):
                            _fail(f"{rel}:{dec.lineno} api_route methods= holds "
                                  "a non-literal element — cannot enumerate the "
                                  "endpoint's verbs; make them literals")
                        methods = [m.upper() for m in methods]
                    else:
                        _fail(f"{rel}:{dec.lineno} api_route methods= is not a "
                              "readable list literal — the surface cannot record "
                              "which verbs this endpoint serves")
                else:
                    methods = [fn.attr.upper()]
                effective = _prefix_by_module.get(str(rel), "") + route
                for method in methods:
                    routes.append({"method": method, "path": effective,
                                   "module": str(rel)})
    surface = {
        "generated_by": "scripts/mandate_gate.py surface",
        "files_tracked": len(tracked),
        "python_modules": sorted(f for f in tracked if f.endswith(".py")),
        "routes": sorted(routes, key=lambda r: (r["path"], r["method"])),
        # The three real APScheduler jobs registered in app/scheduler.py
        # (review 1.7): the prior list said "4-hourly recompute" + a host
        # watchdog, omitting breadth_refresh and the conditional daily_sms and
        # mislabelling a host-level systemd unit as an in-process job.
        "scheduled_jobs": [
            "recompute — composite recompute, APScheduler CronTrigger "
            "02/06/10/14/18/22 UTC (app/scheduler.py)",
            "breadth_refresh — breadth cache sweep, CronTrigger 01/13 UTC "
            "(app/scheduler.py)",
            "daily_sms — SMS report, CronTrigger at sms_daily_hour/minute, "
            "registered only when SMS_ENABLED (app/scheduler.py)",
            "deploy watchdog — HOST systemd unit, NOT an in-process scheduler "
            "job (docs/AUTO_DEPLOY.md)"],
        "data_stores": ["SQLite (DB_URL; snapshots, indicator_readings, "
                        "falsification_outcomes append-only, dashboard_feed, "
                        "daily_close, breadth caches)"],
        "model_providers": ["Anthropic (judgment note; generator vendor)",
                            "OpenAI (independent verifier panel; second vendor)"],
        # Twelve Data and Polygon added (review 1.7): both are live network
        # egress in app/sources/prices.py and breadth.py (api.twelvedata.com,
        # api.polygon.io) that the hand-authored list omitted.
        "egress": ["FRED/ALFRED", "SEC EDGAR", "Shiller xls", "Stooq/price",
                   "Twelve Data (prices/breadth)", "Polygon (breadth grouped-daily)",
                   "CNN Fear&Greed", "sipgate SMS", "Anthropic API",
                   "OpenAI API (CI panel)"],
        "identities": ["operator (mglaeser) — human-in-command",
                       "GitHub Actions runner (CI gates)",
                       "container app user (runtime)"],
        "policy_bundle": [".github/workflows/ci.yml",
                          ".github/workflows/independent-verify.yml",
                          "scripts/independent_verify.py",
                          "scripts/mandate_gate.py",
                          "governance/ (CODEOWNERS-protected)"],
        "workflows": sorted(f for f in tracked if f.startswith(".github/")),
        "prompts": ["app/engine/judgment.py (judgment note; numbers-only invariant)",
                    "app/notify/ (SMS text)"],
    }
    # Validate the hand-authored path claims against REALITY, not just against
    # the generator (panel review P2.7): the surface carried `app/llm.py`, a
    # file that does not exist — the real model call is app/engine/judgment.py.
    # Drift-check only proved JSON==generator, so a dangling path survived
    # indefinitely. Every path-shaped token in prompts/policy_bundle must
    # resolve to a real file or directory, or the surface fails closed.
    #
    # Guarded on the gate script's own presence: this is only meaningful
    # against a real checkout, and the unit tests point ROOT at a synthetic
    # tree that deliberately omits the inventoried files. In CI, ROOT is the
    # real repo (Path(__file__).parents[1]) so the guard always holds and the
    # validation runs.
    if (ROOT / "scripts" / "mandate_gate.py").exists():
        claimed_paths = []
        for entry in surface["prompts"] + surface["policy_bundle"]:
            token = entry.split()[0]                 # "app/x.py (note)" -> path
            if "/" in token or token.endswith((".py", ".yml")):
                claimed_paths.append(token)
        missing = [t for t in claimed_paths if not (ROOT / t).exists()]
        if missing:
            _fail(f"audit surface names path(s) that do not exist: {missing} — "
                  "the inventory must describe the real repository, not a "
                  "stale hand-edited claim (fix the surface generator)")
    out = AUDIT / "00-audit-surface.json"
    rendered = json.dumps(surface, indent=2, sort_keys=True) + "\n"
    if check:
        try:
            committed = out.read_text()
        except OSError:
            _fail("audit/00-audit-surface.json is missing — the audit "
                  "denominator must be committed; run `surface` and commit it")
        if committed != rendered:
            _fail("audit/00-audit-surface.json drifted from the computed "
                  "surface — regenerate with `surface` and commit (the file "
                  "is a computed property, never hand-edited). Tracked files: "
                  f"committed={json.loads(committed).get('files_tracked')!r} "
                  f"computed={len(tracked)}")
        print(f"surface: OK — {len(tracked)} tracked files, {len(routes)} routes")
        return
    out.write_text(rendered)
    print(f"wrote {out.relative_to(ROOT)} ({len(tracked)} tracked files, "
          f"{len(routes)} routes)")


# ------------------------------------------------------------------ main ----


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd == "status":
        cmd_status(write="--write" in args)
    elif cmd == "ratchet":
        cmd_ratchet(measure_only="--measure" in args)
    elif cmd == "calibrate":
        cmd_calibrate()
    elif cmd == "surface":
        cmd_surface(check="--check" in args)
    elif cmd == "all":
        cmd_status(write=False)
        cmd_ratchet(measure_only=False)
        cmd_calibrate()
        cmd_surface(check=True)
        print("mandate-gate: ALL OK")
    else:
        print(f"unknown subcommand {cmd!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
