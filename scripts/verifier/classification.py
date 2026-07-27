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
from dataclasses import dataclass

from .gitdiff import ChangedFile

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
    ".service", ".timer", ".path", ".socket", ".mount", ".target",
    # git/CI hook payloads (B2: a *.hook file IS the executed script)
    ".hook")

# Files that are CODE by default unless positively known otherwise
# (fail-closed): extensionless entrypoint scripts, hooks and unit files, and
# DOTFILES — .bashrc/.zshrc/.envrc/.profile are executed by shells and
# direnv exactly like scripts (B2 fix: the old rule classified every
# single-dot dotfile as non-code, which hid .bashrc from the coverage
# block). Only these well-known text artefacts are exempt.
_KNOWN_NON_CODE = frozenset({
    "LICENSE", "LICENCE", "NOTICE", "AUTHORS", "CONTRIBUTORS", "COPYRIGHT",
    "CHANGELOG", "README", "VERSION", "MANIFEST", ".gitignore",
    ".gitattributes", ".dockerignore", ".gitkeep", ".mailmap",
    ".editorconfig"})

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
    """Every rule here fails CLOSED: unknown shapes classify as code.

    B2 hardening (mandate 6.5): Dockerfile.prod/Containerfile.ci-style
    variant names match by case-insensitive prefix (the old exact-name match
    silently declassified them), and dotfiles default to CODE — .bashrc,
    .zshrc, .envrc and .profile are executed, and the old rule made every
    single-dot dotfile non-code."""
    text = as_text(path)
    base = text.rsplit("/", 1)[-1]
    lowered = base.lower()
    if base in _CODE_NAMES:
        return True
    if lowered.startswith(("dockerfile", "containerfile")):
        return True
    if base in _KNOWN_NON_CODE:
        return False
    if "." not in base:
        return True                                # extensionless: fail closed
    if base.startswith(".") and base.count(".") == 1:
        return True                                # dotfile: fail closed
    return lowered.endswith(_CODE_SUFFIXES)


# CODEOWNERS FETCHING lives in verifier.codeowners (B2, mandate 6.4): rules
# are read from COMMITS via git object queries and unioned base+head. The
# old working-tree read that lived here is gone — a rules file on disk is
# not evidence of anything the commits say.


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


def control_evidence(path: bytes,
                     patterns: list[str]) -> tuple[bool, list[str], list[str]]:
    """(control_bearing, reasons, matched_codeowners_rules) for ONE path.

    There is deliberately NO exemption beyond the documented three. An
    earlier revision exempted the oversized operator-supplied mandate text so
    it could not permanently block the budget; the panel refused that twice,
    correctly — a hash written by the same change proves bytes, not
    legitimacy. This package resolves that conflict by SPLITTING oversized
    files instead of arguing for a carve-out."""
    text = as_text(path)
    reasons: list[str] = []
    matched: list[str] = []
    if text in _CONTROL_DATA:
        return True, ["control_data"], []
    if text in _CODEOWNERS_EXEMPT:
        return False, ["documented_codeowners_exemption"], []
    if is_code(path):
        reasons.append("code")
    if text.startswith("governance/"):
        reasons.append("governance_prefix")
    if text.startswith(".github/"):
        reasons.append("github_prefix")
    if text.rsplit("/", 1)[-1] == "CODEOWNERS":
        reasons.append("codeowners_file_itself")
    for pattern in patterns:
        if owned_by_codeowners(path, [pattern]):
            matched.append(pattern)
    if matched:
        reasons.append("codeowners_routed")
    return bool(reasons), reasons, matched


def is_control_bearing(path: bytes, patterns: list[str]) -> bool:
    return control_evidence(path, patterns)[0]


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


@dataclass(frozen=True)
class ClassificationRecord:
    """The classification of one changed record, with its evidence (6.5).

    Both endpoints are classified independently and the EFFECTIVE result is
    the union / higher-risk side, so a rename can never launder either
    direction. `reasons` is never empty when `control_bearing` is true —
    an unexplained control decision cannot be audited."""

    git_status: str
    new_side: dict
    old_side: dict | None
    effective_risk: int
    control_bearing: bool
    reasons: tuple[str, ...]
    matched_codeowners_rules: tuple[str, ...]

    def to_record(self) -> dict:
        return {
            "git_status": self.git_status,
            "new_side": self.new_side,
            "old_side": self.old_side,
            "effective_risk": self.effective_risk,
            "control_bearing": self.control_bearing,
            "reasons": list(self.reasons),
            "matched_codeowners_rules": list(self.matched_codeowners_rules),
        }


def _side(path: bytes, patterns: list[str]) -> tuple[dict, bool, list[str], list[str]]:
    control, reasons, matched = control_evidence(path, patterns)
    record = {
        "risk": path_risk(path),
        "is_code": is_code(path),
        "control_bearing": control,
        "reasons": list(reasons),
        "matched_codeowners_rules": list(matched),
    }
    return record, control, reasons, matched


def classify_record(entry: ChangedFile,
                    patterns: list[str]) -> ClassificationRecord:
    new_rec, new_ctl, new_reasons, new_matched = _side(entry.path, patterns)
    old_rec = None
    old_ctl, old_reasons, old_matched = False, [], []
    if entry.orig_path is not None:
        old_rec, old_ctl, old_reasons, old_matched = _side(
            entry.orig_path, patterns)
    reasons: list[str] = []
    for source, side_reasons in (("new", new_reasons), ("old", old_reasons)):
        for reason in side_reasons:
            tagged = f"{source}:{reason}"
            if tagged not in reasons:
                reasons.append(tagged)
    matched: list[str] = []
    for rule in (*new_matched, *old_matched):
        if rule not in matched:
            matched.append(rule)
    return ClassificationRecord(
        git_status=entry.status,
        new_side=new_rec,
        old_side=old_rec,
        effective_risk=risk_of_record(entry),
        control_bearing=new_ctl or old_ctl,
        reasons=tuple(reasons),
        matched_codeowners_rules=tuple(matched),
    )
