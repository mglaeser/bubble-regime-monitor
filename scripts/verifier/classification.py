"""Risk ordering and control-bearing classification.

Ported from scripts/independent_verify.py on PR #23 with every hardening
intact, and adapted to byte-exact paths. Classification decides which changed
atoms MUST be covered before the panel may report green; getting it wrong is
how a gate change slips through unreviewed.

Paths are matched as text decoded with `surrogateescape`. Every pattern here
is ASCII, so a non-UTF-8 path simply fails to match — and every default in
this module is fail-closed (extensionless means CODE, unknown means
control-bearing when owner-routed), so failing to match never downgrades a
file's protection.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .gitdiff import ChangedFile, run_git

# Risk order: lower is reviewed earlier. (Panel finding, PR #23 part 1: the
# gate itself ranked below tests, so the hash manifest fell out of budget and
# the panel approved with "gate implementation truncated" — content-blind
# ordering means the highest-risk file can be the one nobody reviews.)
_RISK_ORDER: tuple[tuple[int, tuple[str, ...]], ...] = (
    (0, (".github/", "scripts/")),        # the gate and the panel itself
    (1, ("governance/constitution.md", "governance/accepted-residuals.json",
         "governance/mandate/manifest.json",
         "frozen_methodology.json", "audit/ratchet-baselines.json",
         ".secrets.baseline")),           # the law + the attestations
    (2, ("app/",)),                       # production code
    (3, ("migrations/", "Containerfile", "compose.yml", "deploy.sh",
         "pyproject.toml")),              # runtime/build surface
    (4, ("frozen_methodology.json",)),    # the scored artifact
    (5, ("tests/",)),                     # test code
    (6, ("governance/",)),                # remaining law prose
)
_RISK_DEFAULT = 7                         # docs, audit records, data blobs

# Matched case-INSENSITIVELY (own sweep: the privacy excludes are icase while
# this was case-sensitive, so `.SQL` was privacy-excluded AND classified
# non-code — dropped from the body and from the coverage block at once).
_CODE_SUFFIXES = (
    ".py", ".pyi", ".sh", ".bash", ".zsh", ".r", ".sql", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".conf", ".mk", ".make", ".cmake", ".gradle",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rb", ".rs", ".c",
    ".h", ".cc", ".cpp", ".hpp", ".java", ".kt", ".cs", ".php", ".pl", ".lua",
    ".ps1", ".bat", ".cmd", ".tf", ".tfvars", ".nix", ".proto",
    # systemd/init units arm host automation exactly like a script does
    ".service", ".timer", ".path", ".socket", ".mount", ".target")

# Extensionless files are CODE by default (fail-closed): entrypoint scripts,
# hooks and unit files routinely have no suffix, and misclassifying one hides
# it from the coverage block. Only these well-known text artefacts are exempt.
_EXTENSIONLESS_NON_CODE = frozenset({
    "LICENSE", "LICENCE", "NOTICE", "AUTHORS", "CONTRIBUTORS", "COPYRIGHT",
    "CHANGELOG", "README", "VERSION", "MANIFEST", ".gitignore",
    ".gitattributes", ".dockerignore", ".secrets.baseline"})

_CODE_NAMES = (
    "Makefile", "Dockerfile", "Containerfile", "compose.yml",
    "requirements.txt", "alembic.ini", ".pre-commit-config.yaml",
    # Executable manifests carry lifecycle scripts and dependency resolution,
    # so a change runs code at install/build time exactly like a script does
    # (round 19: a package.json lifecycle-script edit could be omitted from
    # review because ".json" reads as data).
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "composer.json", "Gemfile", "Gemfile.lock", "Cargo.toml", "Cargo.lock",
    "go.mod", "go.sum", "poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock",
    "Procfile", "Justfile", "justfile", "Rakefile", "renovate.json",
    ".npmrc", ".nvmrc", ".tool-versions")

# Not code, but load-bearing: the scored artefact and the gate's thresholds.
# The bulk audit records are deliberately absent — they are hash-attested AND
# semantically validated every CI run by mandate_gate, a deterministic control
# that does not depend on a model reading 94KB of JSON. The files below have
# no such semantic check: a lowered ratchet floor still satisfies the ratchet
# (measured >= floor), so these must be read by the panel.
_CONTROL_DATA = ("frozen_methodology.json",
                 "audit/ratchet-baselines.json",
                 ".secrets.baseline")

_CODEOWNERS_PATH = ".github/CODEOWNERS"

# ONE documented exception, carried over rather than silently dropped.
_CODEOWNERS_EXEMPT = ("audit/03-findings.json",
                      "audit/engagement-status.json",
                      "audit/00-check-catalogue.json")


def as_text(path: bytes) -> str:
    """Decode for pattern matching only. Never used for identity or equality."""
    return path.decode("utf-8", errors="surrogateescape")


def path_risk(path: bytes) -> int:
    """Lower = reviewed earlier. Ties keep git's own ordering (deterministic)."""
    text = as_text(path)
    for rank, prefixes in _RISK_ORDER:
        if any(text.startswith(pfx) for pfx in prefixes):
            return rank
    return _RISK_DEFAULT


def is_code(path: bytes) -> bool:
    text = as_text(path)
    base = text.rsplit("/", 1)[-1]
    if base in _CODE_NAMES:
        return True
    if "." not in base:
        return base not in _EXTENSIONLESS_NON_CODE
    if base.startswith(".") and base.count(".") == 1:
        return False                               # dotfile, no real suffix
    return base.lower().endswith(_CODE_SUFFIXES)


def _parse_codeowners(raw: str) -> list[str]:
    out: list[str] = []
    for line in raw.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        pattern = stripped.split()[0]
        if pattern:
            out.append(pattern)
    return out


def _codeowners_at(rev: str | None, *, cwd=None) -> str:
    if rev is None:
        try:
            root = Path(cwd) if cwd else Path.cwd()
            return (root / _CODEOWNERS_PATH).read_text(errors="replace")
        except OSError:
            return ""
    return run_git(["git", "show", f"{rev}:{_CODEOWNERS_PATH}"],
                   cwd=cwd).decode("utf-8", errors="replace")


def codeowners_patterns(merge_base_rev: str | None, *, cwd=None) -> list[str]:
    """The UNION of the owner rules at the merge base and at HEAD.

    Reading HEAD alone would let the same change that touches an owner-routed
    file also DELETE its CODEOWNERS rule, declassifying it in the very diff
    that needs the protection. Reading the base alone would miss newly
    protected paths. The union is the safe direction for a coverage gate."""
    merged: list[str] = []
    for rev in (merge_base_rev, None):
        for pattern in _parse_codeowners(_codeowners_at(rev, cwd=cwd)):
            if pattern not in merged:
                merged.append(pattern)
    return merged


def owned_by_codeowners(path: bytes, patterns: list[str]) -> bool:
    """Whether a CODEOWNERS rule routes this path to an owner.

    The repository already declares which paths are governing; maintaining a
    SECOND hand-written list here is the "which paths did we remember" failure
    that keeps recurring (panel round 30: CLAUDE.md, GOVERNANCE_FREEZE_RULE.md
    and three PIN evidence packs were all owner-routed and none was
    control-bearing, so budget eviction could drop them with no block)."""
    text = as_text(path)
    for pattern in patterns:
        pat = pattern.rstrip("/")
        if pat in ("*", "/*"):
            return True
        pat = pat.lstrip("/")
        if not pat:
            continue
        if fnmatch.fnmatch(text, pat) or fnmatch.fnmatch(text, pat + "/*"):
            return True
        # A directory rule owns everything beneath it.
        if text.startswith(pat + "/"):
            return True
        if fnmatch.fnmatch(os.path.basename(text), pat):
            return True
    return False


def is_control_bearing(path: bytes, patterns: list[str]) -> bool:
    """Files whose UNREVIEWED omission must block, beyond executable code.

    There is deliberately NO exemption. An earlier revision exempted the
    oversized operator-supplied mandate text so it could not permanently block
    the budget; the panel refused that twice, correctly — a hash written by the
    same change proves bytes, not legitimacy. This package resolves that
    conflict by SPLITTING oversized files instead of arguing for a carve-out."""
    text = as_text(path)
    if text in _CONTROL_DATA:
        return True
    if text in _CODEOWNERS_EXEMPT:
        return False
    return (is_code(path)
            or text.startswith("governance/")
            or text.startswith(".github/")
            or text.rsplit("/", 1)[-1] == "CODEOWNERS"
            or owned_by_codeowners(path, patterns))


def control_bearing_record(entry: ChangedFile, patterns: list[str]) -> bool:
    """EITHER endpoint of a rename/copy being control-bearing makes the change
    control-bearing: renaming a gate to a docs path must not launder it."""
    if is_control_bearing(entry.path, patterns):
        return True
    return bool(entry.orig_path
                and is_control_bearing(entry.orig_path, patterns))


def risk_of_record(entry: ChangedFile) -> int:
    """A renamed file keeps the HIGHER risk (lower rank) of its two paths."""
    rank = path_risk(entry.path)
    if entry.orig_path:
        rank = min(rank, path_risk(entry.orig_path))
    return rank
