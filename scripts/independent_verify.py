#!/usr/bin/env python3
"""Independent cross-vendor PR review panel (ported from RoseLohr/roses-food-blog,
scripts/regime/independent-verify.mjs — same mechanism, same roles).

Every pull request is attacked by a PANEL of verifier voices from a DIFFERENT
AI vendor than the one that writes this repo's code (this repo is written with
Anthropic models, so the panel runs on an OpenAI-compatible endpoint). Each
voice is a DIFFERENT model — model diversity beats mere lens diversity —
and each voice gets a distinct review lens.

ROLES (identical to the reference):
  * REQUIRED APPROVER — a named panelist (default ``gpt-5.6-sol``, "Sol") that
    MUST be resolved in the panel and MUST explicitly approve. A refutation of
    ANY confidence (even "low") is a Sol veto; a missing/fallback-replaced Sol
    or an invalid Sol vote blocks (fail-closed). Sol's approval must be PROVEN:
    a substantive own reason and (when a challenge is active) a valid own
    proof-of-check — it may not free-ride on the other voices' attestation.
  * INDEPENDENT CORROBORATORS — at least VERIFIER_MIN_OTHER_APPROVERS (default
    1) DISTINCT non-Sol models must also approve. Repeat votes of the same
    model never count twice.

INTEGRITY GATES (all fail-closed, in order):
  1. require_approvals() — the role gate above.
  2. attest_reasons()    — a majority of the GREEN votes must carry substantive,
                           mutually distinct reasons (anti canned-green).
  3. attest_proof()      — a majority of the GREEN votes must echo the per-run
                           random challenge as "<challenge>-<tier 1-9999>"
                           (anti hardcoded-green: a future "return green"
                           shortcut without a real endpoint round-trip cannot
                           carry a run-valid echo). Honest scope note: this does
                           NOT cryptographically prove an LLM round-trip — a
                           malicious endpoint could mirror the challenge. That
                           is the documented cross-vendor trust assumption,
                           compensated by the deterministic CI gate remaining
                           the sole merge authority.

NO-KEY MODE: without SECOND_VENDOR_API_KEY (or OPENAI_API_KEY) the panel is
inactive; the run prints the documented residual and exits 0 — visible, never
fake-green and never fake-blocking.

PRIVACY: only CODE leaves the origin. Images (raster AND vector), fonts,
binaries and data files are excluded from both the --stat overview and the
diff body via per-extension pathspecs (git pathspec globs do NOT brace-expand,
hence one exclude per extension).

ENV (same contract as the reference):
  SECOND_VENDOR_API_KEY / OPENAI_API_KEY   activates the panel
  VERIFIER_BASE_URL       default https://api.openai.com/v1
  VERIFIER_MODEL          single pin: this one model for ALL voices
  VERIFIER_PANEL_MODELS   comma list — one DIFFERENT model per voice
  VERIFIER_PANEL          voice count in single-pin mode (default 3, cap 64)
  VERIFIER_REQUIRED_APPROVER      required-approver model prefix (default gpt-5.6-sol)
  VERIFIER_MIN_OTHER_APPROVERS    independent corroborations required (default 1)

Usage:
  python scripts/independent_verify.py             verify the PR diff (exit != 0 on block)
  python scripts/independent_verify.py --selftest  exercise every pure decision function
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

KEY = os.environ.get("SECOND_VENDOR_API_KEY") or os.environ.get("OPENAI_API_KEY")
BASE = os.environ.get("VERIFIER_BASE_URL", "https://api.openai.com/v1")

# Preference order (new -> old): without an explicit VERIFIER_MODEL the panel
# resolves each wanted ID against the account's /v1/models list.
MODEL_PREFERENCE = [
    "gpt-5.6-sol", "gpt-5.6", "gpt-5.5", "gpt-5.1", "gpt-5",
    "gpt-4.1", "gpt-4o-2024-08-06", "gpt-4o",
]

# Broadly available, pinned fallback (NOT the newest/scarcest — a rejected model
# would turn every vote into an API error and block the control permanently).
FALLBACK_MODEL = "gpt-4o-2024-08-06"

# One DIFFERENT model per voice (override via VERIFIER_PANEL_MODELS).
DEFAULT_PANEL_MODELS = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]

# A real reason needs substance; "ok"/"1"/"n/a" cannot attest green.
MIN_REASON_LEN = 8

# ---------------------------------------------------------------- pure gates --


def decide(v: Any) -> dict[str, Any]:
    """Per-vote decision, fail-CLOSED: a reply without a boolean ``refuted``
    blocks; a refutation with confidence high OR medium blocks; only a
    low-confidence refutation passes (the required approver is stricter)."""
    if not isinstance(v, dict) or not isinstance(v.get("refuted"), bool):
        return {"block": True, "reason": "unparsable/no refuted field -> fail-closed"}
    if v["refuted"] and v.get("confidence") != "low":
        return {"block": True, "reason": v.get("reason") or "refutation (confidence >= medium)"}
    return {"block": False}


def model_matches(model_id: Any, want: Any) -> bool:
    """Does a resolved model ID count as the required approver? Exact ID or a
    DATED snapshot ``<want>-YYYY-MM-DD`` only — a variant suffix (-mini/-codex/
    -preview) is a DIFFERENT, possibly weaker model and must never impersonate
    the approver; a bare name prefix without separator does not count either."""
    if not model_id or not want:
        return False
    if model_id == want:
        return True
    return re.match(rf"^{re.escape(want)}-\d{{4}}-\d{{2}}-\d{{2}}$", model_id) is not None


def norm_reason(r: Any) -> str:
    """Whitespace-collapsed, trimmed, lowercased reason ('' for non-strings)."""
    return re.sub(r"\s+", " ", r).strip().lower() if isinstance(r, str) else ""


def has_substantive_reason(v: Any) -> bool:
    return len(norm_reason((v or {}).get("reason"))) >= MIN_REASON_LEN


def has_valid_proof(v: Any, challenge: str | None) -> bool:
    """Valid proof-of-check: '<challenge>-<tier 1-9999>' (no 0 / leading zero /
    >= 10000)."""
    proof = (v or {}).get("proof")
    if not isinstance(proof, str) or not challenge:
        return False
    pre = challenge + "-"
    return proof.startswith(pre) and re.match(r"^[1-9]\d{0,3}$", proof[len(pre):]) is not None


def _is_valid(x: Any) -> bool:
    return bool(x and x.get("ok") and isinstance(x.get("v"), dict)
                and isinstance(x["v"].get("refuted"), bool))


def require_approvals(votes: list[dict], models: list[str], required_approver: str,
                      min_others: Any = 1, challenge: str | None = None) -> dict[str, Any]:
    """The ROLE gate (pure, testable). Green ONLY if ALL hold:
      1) the required approver is RESOLVED in the panel (>= 1 voice actually
         runs on that model; a fallback replacement does not count), AND
      2) EVERY required-approver voice is valid and EXPLICITLY approves —
         a refutation of ANY confidence (even low) is a veto — and carries its
         own substantive reason plus (when a challenge is set) a valid own
         proof-of-check, AND
      3) at least ``min_others`` DISTINCT non-approver models approve; repeat
         votes of the same model never count as independent corroboration.
    NaN/garbage ``min_others`` degrades to 1 (never fail-open)."""
    try:
        mo = int(min_others)
        need = mo if mo >= 1 else 1
    except (TypeError, ValueError):
        need = 1
    req_idx = {i for i in range(len(votes)) if model_matches(models[i], required_approver)}
    if not req_idx:
        return {"block": True,
                "reason": f'required approver "{required_approver}" not resolved in the panel '
                          "(not enabled or replaced by fallback) -> fail-closed"}
    for i in req_idx:
        x = votes[i]
        if not _is_valid(x):
            return {"block": True,
                    "reason": f'required approver "{required_approver}" without a valid vote '
                              "(error/unparsable) -> fail-closed"}
        if x["v"]["refuted"] is not False:
            return {"block": True,
                    "reason": f'required approver "{required_approver}" does NOT approve '
                              f'(refuted, confidence={x["v"].get("confidence", "?")}) -> veto, fail-closed'}
        if not has_substantive_reason(x["v"]):
            return {"block": True,
                    "reason": f'required approver "{required_approver}" without a substantive own '
                              "reason -> suspected sham green, fail-closed"}
        if challenge and not has_valid_proof(x["v"], challenge):
            return {"block": True,
                    "reason": f'required approver "{required_approver}" without a valid own '
                              "proof-of-check (challenge echo) -> fail-closed"}
    independent = {models[i] for i, x in enumerate(votes)
                   if i not in req_idx and _is_valid(x) and x["v"]["refuted"] is False and models[i]}
    if len(independent) < need:
        return {"block": True,
                "reason": f'only {len(independent)} independent approving model(s) besides '
                          f'"{required_approver}" (< {need}) -> no independent corroboration, fail-closed'}
    return {"block": False,
            "reason": f'"{required_approver}" approves + {len(independent)} independent model '
                      f"approval(s) (>= {need})"}


def _green(votes: list[dict]) -> list[dict]:
    return [x for x in votes if _is_valid(x) and not decide(x["v"])["block"]]


def attest_reasons(votes: list[dict], panel_size: int) -> dict[str, Any]:
    """Anti sham-green gate: a MAJORITY of the green-carrying votes must have a
    substantive reason, and a MAJORITY of those must be mutually DISTINCT after
    normalization. Only green votes count (a refutation's reason must not
    inflate the release's distinctness)."""
    need = panel_size // 2 + 1
    normed = [s for s in (norm_reason(x["v"].get("reason")) for x in _green(votes))
              if len(s) >= MIN_REASON_LEN]
    if len(normed) < need:
        return {"block": True,
                "reason": f"only {len(normed)}/{panel_size} green votes with a real reason "
                          f"(< majority {need}) -> suspected sham green, fail-closed"}
    if len(set(normed)) < need:
        return {"block": True,
                "reason": f"only {len(set(normed))} distinct green reason(s) among {len(normed)} "
                          f"(< majority {need}) -> suspected sham green, fail-closed"}
    return {"block": False, "reason": f"{len(set(normed))} independent green reasons attested"}


def attest_proof(votes: list[dict], challenge: str, panel_size: int) -> dict[str, Any]:
    """Anti hardcoded-green gate: a MAJORITY of the green-carrying votes must
    echo the per-run random challenge as a valid proof-of-check."""
    need = panel_size // 2 + 1
    proven = [x for x in _green(votes) if has_valid_proof(x["v"], challenge)]
    if len(proven) < need:
        return {"block": True,
                "reason": f"only {len(proven)}/{panel_size} green votes with a valid proof-of-check "
                          f"(challenge echo) (< majority {need}) -> suspected hardcoded green, fail-closed"}
    return {"block": False, "reason": f"{len(proven)} proof-of-check (challenge echo) confirmed"}


# ------------------------------------------------------------- diff assembly --

# PRIVACY: only code goes to the foreign vendor. One exclude pathspec PER
# extension (git ':(glob)' does not brace-expand). data/ (runtime DB volume)
# excluded as a category.
EXCLUDE_EXTS = ["webp", "png", "jpg", "jpeg", "gif", "ico", "svg", "avif", "bmp", "tiff",
                "woff", "woff2", "ttf", "otf", "eot", "pdf", "geojson", "db", "rds", "xlsx"]
_EXCLUDES = [":(exclude,glob)data/**"] + [f":(exclude,glob)**/*.{e}" for e in EXCLUDE_EXTS]


def _sh(args: list[str]) -> str:
    try:
        return subprocess.run(  # noqa: S603 -- fixed git argv built from constants, no shell
            args, capture_output=True, text=True, timeout=120).stdout
    except Exception:
        return ""


def build_diff() -> str:
    """Full file overview (--stat) + code excerpt, base = merge-base with main."""
    base = _sh(["git", "merge-base", "origin/main", "HEAD"]).strip() or "HEAD~1"
    stat = _sh(["git", "diff", "--stat", f"{base}...HEAD", "--", "."] + _EXCLUDES)[:8000]
    body = _sh(["git", "diff", f"{base}...HEAD", "--", "."] + _EXCLUDES)[:50_000]
    if not stat.strip() and not body.strip():
        return ""
    return (f"# Changed files (complete overview):\n{stat}\n\n"
            f"# Code changes (excerpt; binaries/assets/data excluded):\n{body}")


# ------------------------------------------------------------------ prompts --

LENSES = [
    " Focus of this review: security holes and data protection.",
    " Focus of this review: correctness, broken invariants, fail-closed becoming fail-open.",
    " Focus of this review: a gate/control that stops firing; widened blast radius; "
    "and for this repository specifically: any change that alters a scored value, the frozen "
    "methodology artifact, its SHA-256 pin, or the golden fixture without declaring it.",
]


def build_system_prompt(challenge: str) -> str:
    return (
        "You are a hostile code reviewer from a DIFFERENT AI vendor. Goal: REFUTE that this "
        "diff is correct and safe. Look for REAL defects visible in the code: security hole, "
        "broken invariant, a gate/control that stops firing, fail-closed turned fail-open, "
        "widened blast radius, an undeclared change to a frozen methodology constant or golden "
        "fixture. The diff may be truncated; the --stat overview lists ALL files. NOT a defect: "
        "style/readability; 'could be more secure'; speculative attacks without a concrete "
        "path; hypothetical hardening; anything without nameable misbehavior. BOUNDARY: refute "
        "ONLY on a CONCRETE, reproducible defect with nameable misbehavior (wrong output, "
        "crash, a control demonstrably no longer firing, a really exploitable hole WITH an "
        "attack path). No concrete failure path -> refuted=false. Do NOT refute over the "
        "inherent cross-vendor trust assumption itself (that a malicious endpoint could fool "
        "the verifier including the challenge echo) — that is the DOCUMENTED residual, "
        "compensated by the deterministic CI gate; it is not a defect of THIS diff. "
        "Answer ONLY as JSON, no prose/markdown: "
        '{"refuted": boolean, "confidence": "high"|"medium"|"low", "reason": string, '
        '"proof": string}. ALWAYS fill reason (also for refuted=false), maximally terse/'
        "machine-like, abbreviations fine, no full sentences, but technically informative. "
        "Name ALL defects found — each EXTREMELY compact, separated by ' ; '; rather abbreviate "
        "each point harder than drop one. Upper bound ~800 chars. For refuted=true use the "
        "schema 'path/file:line — defect — misbehavior' per point. For refuted=false say in "
        "3-8 words WHAT was checked + why no defect. "
        "PROOF-OF-CHECK: for refuted=false, proof MUST be EXACTLY '" + challenge + "-<tier>' "
        "where <tier> is an integer 1-9999 you choose at random (e.g. '" + challenge + "-4213'). "
        "That proves you really executed this check; a missing/wrong proof invalidates a green. "
        "For refuted=true, proof is optional. refuted=true ONLY on a concrete, nameable defect."
    )


# ----------------------------------------------------------------- selftest --


def selftest() -> None:
    def expect(cond: bool, msg: str) -> None:
        if not cond:
            print(f"BLOCK selftest: {msg}", file=sys.stderr)
            sys.exit(1)

    # decide()
    expect(decide({"refuted": True, "confidence": "high"})["block"] is True, "high refutation must block")
    expect(decide({"refuted": True, "confidence": "medium"})["block"] is True, "medium refutation must block")
    expect(decide({})["block"] is True, "missing refuted field must fail-closed block")
    expect(decide(None)["block"] is True, "null/unparsable must fail-closed block")
    expect(decide({"refuted": False})["block"] is False, "no refutation must pass")
    expect(decide({"refuted": True, "confidence": "low"})["block"] is False, "low-confidence refutation passes")
    # model_matches()
    expect(model_matches("gpt-5.6-sol", "gpt-5.6-sol") is True, "exact ID matches")
    expect(model_matches("gpt-5.6-sol-2026-07-01", "gpt-5.6-sol") is True, "dated snapshot matches")
    expect(model_matches("gpt-5.6", "gpt-5.6-sol") is False, "shorter model must NOT match")
    expect(model_matches("gpt-5.6-solaris", "gpt-5.6-sol") is False, "prefix without separator must NOT match")
    expect(model_matches(None, "gpt-5.6-sol") is False, "missing model must NOT match")
    expect(model_matches("gpt-5.6-sol-mini", "gpt-5.6-sol") is False, "-mini variant must NOT match")
    expect(model_matches("gpt-5.6-sol-codex", "gpt-5.6-sol") is False, "-codex variant must NOT match")
    expect(model_matches("gpt-5.6-sol-preview", "gpt-5.6-sol") is False, "-preview variant must NOT match")
    # require_approvals() — votes[i] belongs to MDL[i]; Sol at index 1.
    MDL = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
    A = {"ok": True, "v": {"refuted": False, "reason": "reason long enough a"}}
    A2 = {"ok": True, "v": {"refuted": False, "reason": "reason long enough b"}}
    RF = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "real bug"}}
    E = {"ok": False, "reason": "API 500"}
    expect(require_approvals([A, A2, A], MDL, "gpt-5.6-sol", 1)["block"] is False, "Sol + 2 others approve -> green")
    expect(require_approvals([RF, A2, A], MDL, "gpt-5.6-sol", 1)["block"] is False, "Sol approves, 1 refutes, 1 approves -> green")
    expect(require_approvals([A, RF, A], MDL, "gpt-5.6-sol", 1)["block"] is True, "Sol REFUTES -> veto, block")
    expect(require_approvals([A, E, A], MDL, "gpt-5.6-sol", 1)["block"] is True, "Sol vote errored -> fail-closed block")
    expect(require_approvals([A, {"ok": True, "v": None}, A], MDL, "gpt-5.6-sol", 1)["block"] is True, "Sol unparsable -> block")
    expect(require_approvals([RF, A, RF], MDL, "gpt-5.6-sol", 1)["block"] is True, "no independent approval -> block")
    expect(require_approvals([A, A2, A], ["gpt-5.3-codex", "gpt-5.6", "gpt-4.1-mini"], "gpt-5.6-sol", 1)["block"] is True,
           "Sol not in panel (fallback) -> fail-closed block")
    expect(require_approvals([A, A2, A], ["gpt-5.3-codex", "gpt-5.6-sol-2026-07-01", "gpt-4.1-mini"], "gpt-5.6-sol", 1)["block"] is False,
           "Sol as dated snapshot counts -> green")
    expect(require_approvals([A, A2, RF], MDL, "gpt-5.6-sol", 2)["block"] is True, "MIN_OTHERS=2 with 1 approver -> block")
    expect(require_approvals([A, A2, A], MDL, "gpt-5.6-sol", 2)["block"] is False, "MIN_OTHERS=2 with 2 approvers -> green")
    RFLOW = {"ok": True, "v": {"refuted": True, "confidence": "low", "reason": "small doubt"}}
    expect(require_approvals([A, RFLOW, A], MDL, "gpt-5.6-sol", 1)["block"] is True, "Sol low-confidence refutation -> veto")
    SOLO = ["gpt-5.6-sol", "gpt-5.6-sol", "gpt-5.6-sol"]
    expect(require_approvals([A, A2, A], SOLO, "gpt-5.6-sol", 1)["block"] is True, "single-pin all-Sol: no independent corroboration -> block")
    expect(require_approvals([A, RF, A], SOLO, "gpt-5.6-sol", 1)["block"] is True, "single-pin: one Sol voice refutes -> veto")
    DUP = ["gpt-4.1-mini", "gpt-5.6-sol", "gpt-4.1-mini"]
    expect(require_approvals([A, A2, A], DUP, "gpt-5.6-sol", 1)["block"] is False, "1 distinct independent model suffices at MIN_OTHERS=1")
    expect(require_approvals([A, A2, A], DUP, "gpt-5.6-sol", 2)["block"] is True, "same model twice counts once -> < 2 -> block")
    expect(require_approvals([RF, A2, RF], MDL, "gpt-5.6-sol", float("nan"))["block"] is True, "NaN min_others -> need 1; none approve -> block")
    expect(require_approvals([A, A2, RF], MDL, "gpt-5.6-sol", float("nan"))["block"] is False, "NaN min_others -> need 1; codex approves -> green")
    CH2 = "abc123def456"
    sol_empty = {"ok": True, "v": {"refuted": False, "reason": "", "proof": f"{CH2}-7"}}
    sol_no_proof = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol"}}
    sol_good = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol", "proof": f"{CH2}-7"}}
    expect(require_approvals([A, sol_empty, A], MDL, "gpt-5.6-sol", 1, CH2)["block"] is True, "Sol without own substantive reason -> block")
    expect(require_approvals([A, sol_no_proof, A], MDL, "gpt-5.6-sol", 1, CH2)["block"] is True, "Sol without own valid proof -> block")
    expect(require_approvals([A, sol_good, A], MDL, "gpt-5.6-sol", 1, CH2)["block"] is False, "Sol with reason + proof + corroboration -> green")
    expect(require_approvals([A, sol_no_proof, A], MDL, "gpt-5.6-sol", 1)["block"] is False, "no challenge -> no proof required")
    # attest_reasons()
    def R(reason: Any, refuted: bool = False) -> dict:
        return {"ok": True, "v": {"refuted": refuted, "reason": reason}}
    g1, g2, g3 = "reason one aaaa", "reason two bbbb", "reason three cccc"
    expect(attest_reasons([R(g1), R(g2), R(g3)], 3)["block"] is False, "3 distinct green reasons must pass")
    expect(attest_reasons([R(g1), R(g1), R(g2)], 3)["block"] is False, "2/3 majority distinct suffices (one duplicate allowed)")
    expect(attest_reasons([R(g1), R(g1), R(g1)], 3)["block"] is True, "all-identical (canned) green reasons must block")
    expect(attest_reasons([R("Reason AAA XX"), R(" reason   aaa xx "), R("reason aaa xx")], 3)["block"] is True,
           "whitespace/case variants collapse -> distinct=1 -> block")
    expect(attest_reasons([R("a      b"), R("c      d"), R("e      f")], 3)["block"] is True,
           "whitespace padding (< MIN normalized) is not a real reason -> block")
    expect(attest_reasons([R(""), R(""), R("")], 3)["block"] is True, "empty reasons must fail-closed block")
    expect(attest_reasons([R(g1), R(g1), R("real bug here", True)], 3)["block"] is True,
           "canned green majority; the refutation's reason must NOT count -> block")
    expect(attest_reasons([R("ok"), R("ok"), E], 3)["block"] is True, "shortest token 'ok' is not a real reason -> block")
    expect(attest_reasons([R(1), R(2), E], 3)["block"] is True, "numeric reason (coercion) must NOT count -> block")
    expect(attest_reasons([R(g1), R(g2), E], 3)["block"] is False, "2/3 green majority with distinct reasons suffices")
    expect(attest_reasons([R(g1), R(g1), E], 3)["block"] is True, "2 green but only 1 distinct (< majority) -> block")
    expect(attest_reasons([R(g1), E, E], 3)["block"] is True, "only 1/3 green reasoned (< majority) -> fail-closed block")
    expect(attest_reasons([{"ok": True, "v": None}, R(g1), R(g2)], 3)["block"] is False, "unparsable vote ignored, rest distinct -> pass")
    expect(attest_reasons([R("reason solo aaaa")], 1)["block"] is False, "panel=1 with a real reason passes")
    # attest_proof()
    CH = "abc123def456"
    def PR(proof: str, refuted: bool = False) -> dict:
        return {"ok": True, "v": {"refuted": refuted, "reason": "reason long enough", "proof": proof}}
    expect(attest_proof([PR(f"{CH}-7"), PR(f"{CH}-42"), PR(f"{CH}-9")], CH, 3)["block"] is False, "3 valid echoes must pass")
    expect(attest_proof([PR(f"{CH}-7"), PR(f"{CH}-42"), E], CH, 3)["block"] is False, "2/3 majority of valid proofs suffices")
    expect(attest_proof([PR("no-echo-1"), PR("no-echo-2"), PR("no-echo-3")], CH, 3)["block"] is True, "wrong challenge must block")
    expect(attest_proof([PR(f"{CH}-7"), PR(""), PR("")], CH, 3)["block"] is True, "only 1/3 with valid proof -> block")
    expect(attest_proof([PR(f"{CH}-x"), PR(f"{CH}-y"), PR(f"{CH}-z")], CH, 3)["block"] is True, "non-numeric tier -> invalid proof -> block")
    expect(attest_proof([PR(f"{CH}-0"), PR(f"{CH}-0"), PR(f"{CH}-0")], CH, 3)["block"] is True, "tier 0 is invalid -> block")
    expect(attest_proof([PR(f"{CH}-10000"), PR(f"{CH}-10000"), PR(f"{CH}-10000")], CH, 3)["block"] is True, "tier >= 10000 invalid -> block")
    expect(attest_proof([PR(f"{CH}-9999"), PR(f"{CH}-1"), PR(f"{CH}-500")], CH, 3)["block"] is False, "boundary tiers 1 and 9999 valid -> pass")
    expect(attest_proof([PR(f"{CH}-7"), PR(f"{CH}-8"), PR("nope-1", True)], CH, 3)["block"] is False,
           "refuting vote needs no proof while green majority has valid proofs")
    expect(attest_proof([PR(f"{CH}-7")], CH, 1)["block"] is False, "panel=1 with valid proof passes")
    expect(attest_proof([PR(f"{CH}-7"), PR(f"{CH}-8"), PR(f"{CH}-9")], "", 3)["block"] is True, "empty challenge -> no proof valid -> fail-closed")
    print("   OK selftest: decide() + model_matches() + require_approvals() (required approver Sol "
          "+ corroboration) + attest_reasons() + attest_proof() correct.")


# --------------------------------------------------------------- API plumbing --


def _http_json(url: str, payload: dict | None = None, timeout: int = 180) -> tuple[int, Any]:
    req = urllib.request.Request(  # noqa: S310 -- operator-configured https endpoint (VERIFIER_BASE_URL)
        url, headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
        data=json.dumps(payload).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- fixed https base
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode()[:300]
        except Exception:
            body = "(no body)"
        return exc.code, body
    except Exception as exc:
        return 0, str(exc)[:300]


def pick_for_pref(ids: list[str], p: str) -> str | None:
    """Exact ID first, else ONLY a dated snapshot p-YYYY-MM-DD (never a -mini/
    -nano variant); among snapshots the NEWEST."""
    if p in ids:
        return p
    dated = sorted(i for i in ids if re.match(rf"^{re.escape(p)}-\d{{4}}-\d{{2}}-\d{{2}}$", i))
    return dated[-1] if dated else None


def fetch_model_ids() -> list[str] | None:
    status, data = _http_json(f"{BASE}/models")
    if status == 200 and isinstance(data, dict):
        return [m.get("id") for m in data.get("data", []) if m.get("id")]
    return None


def resolve_panel_models(panel_size: int, wanted: list[str]) -> list[str]:
    if os.environ.get("VERIFIER_MODEL"):
        return [os.environ["VERIFIER_MODEL"]] * panel_size
    ids = fetch_model_ids()
    if not ids:
        print(f"[independent-verify] /v1/models unavailable -> fallback model {FALLBACK_MODEL} for all voices.")
        return [FALLBACK_MODEL] * panel_size
    newest = FALLBACK_MODEL
    for p in MODEL_PREFERENCE:
        hit = pick_for_pref(ids, p)
        if hit:
            newest = hit
            break
    out = []
    for want in wanted:
        hit = pick_for_pref(ids, want)
        if hit:
            out.append(hit)
        else:
            print(f'[independent-verify] WARNING: panel model "{want}" not enabled on this account '
                  f"-> fallback {newest} (adjust VERIFIER_PANEL_MODELS).")
            out.append(newest)
    return out


def parse_verdict(content: str) -> Any:
    if not content:
        return None
    try:
        return json.loads(content)
    except ValueError:
        m = re.search(r"\{[\s\S]*\}", content)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


def _extract_responses_text(data: Any) -> str:
    if isinstance(data, dict) and isinstance(data.get("output_text"), str) and data["output_text"]:
        return data["output_text"]
    parts = []
    for item in (data or {}).get("output", []) if isinstance(data, dict) else []:
        for c in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
    return "".join(parts)


# Transient = a retry can help: network error (no status), rate-limit/timeout/
# conflict and 5xx. 401 counts as transient too (observed flaky LB behavior in
# the reference). Deterministic client errors (400/403/404) are never retried.
def is_transient(status: int) -> bool:
    return status in (0, 401, 408, 409, 429) or status >= 500


def attempt_once(model: str, sys_prompt: str, user_prompt: str) -> dict:
    status, data = _http_json(f"{BASE}/chat/completions", {
        "model": model,
        "messages": [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user_prompt}],
    })
    if status == 200 and isinstance(data, dict):
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        v = parse_verdict(content or "")
        return {"ok": True, "v": v, "decision": decide(v)}
    # Some models only support the Responses API — the 404 says so; switch over.
    if status == 404 and isinstance(data, str) and re.search(r"v1/responses|responses endpoint", data, re.I):
        s2, d2 = _http_json(f"{BASE}/responses",
                            {"model": model, "instructions": sys_prompt, "input": user_prompt})
        if s2 == 200:
            v = parse_verdict(_extract_responses_text(d2))
            return {"ok": True, "v": v, "decision": decide(v)}
        return {"ok": False, "status": s2, "reason": f"Responses API {s2}: {str(d2)[:300]}"}
    return {"ok": False, "status": status, "reason": f"API {status}: {str(data)[:300]}"}


def verify_once(model: str, sys_prompt: str, user_prompt: str) -> dict:
    last: dict = {"ok": False, "reason": "no attempt executed"}
    for a in range(1, 4):
        last = attempt_once(model, sys_prompt, user_prompt)
        if last.get("ok"):
            return last
        if last.get("status") and not is_transient(last["status"]):
            return last            # deterministic error -> no retry
        if a < 3:
            time.sleep(0.5 * a)
    return last


# --------------------------------------------------------------------- main --


def main() -> int:
    if "--selftest" in sys.argv:
        selftest()
        return 0

    if not KEY:
        print("[independent-verify] RESIDUAL: no second-vendor key (SECOND_VENDOR_API_KEY or "
              "OPENAI_API_KEY) provisioned.\n"
              "  The independent cross-vendor review panel is NOT active. Compensation: the\n"
              "  deterministic CI gate remains the sole merge authority. To activate: set the\n"
              "  secret (see docs/INDEPENDENT_REVIEW_PANEL.md).")
        return 0   # no fake block; the residual is documented and visible

    d = build_diff()
    if not d.strip():
        print("[independent-verify] No diff to review. Green.")
        return 0

    env_panel = [s.strip() for s in os.environ.get("VERIFIER_PANEL_MODELS", "").split(",") if s.strip()]
    wanted = env_panel or DEFAULT_PANEL_MODELS      # empty/','-input must not empty the panel
    if os.environ.get("VERIFIER_MODEL"):
        try:
            n = int(os.environ.get("VERIFIER_PANEL", ""))
            panel = min(64, n) if n >= 1 else 3
        except ValueError:
            panel = 3
    else:
        panel = max(1, len(wanted))
    required_approver = (os.environ.get("VERIFIER_REQUIRED_APPROVER") or "gpt-5.6-sol").strip()
    try:
        mo = int(os.environ.get("VERIFIER_MIN_OTHER_APPROVERS", ""))
        min_others = mo if mo >= 1 else 1
    except ValueError:
        min_others = 1

    models = resolve_panel_models(panel, wanted)
    print(f"[independent-verify] Panel ({len(models)} voices, one model each): {', '.join(models)}"
          + (" (VERIFIER_MODEL pinned)" if os.environ.get("VERIFIER_MODEL") else ""))
    print(f'[independent-verify] Required approver: "{required_approver}" must approve + >= {min_others} other(s).')

    challenge = secrets.token_hex(9)   # fresh 18-hex challenge per run
    sys_base = build_system_prompt(challenge)
    user_prompt = "DIFF (overview + code excerpt):\n\n" + d

    votes = [verify_once(models[i], sys_base + LENSES[i % len(LENSES)], user_prompt)
             for i in range(panel)]
    for i, x in enumerate(votes):
        if not x.get("ok"):
            print(f"  Verifier {i + 1}/{panel} ({models[i]}): error ({x.get('reason')})")
            continue
        v = x.get("v") or {}
        # json.dumps escapes all control chars -> single-line, injection-safe log
        reason = json.dumps(v.get("reason"))[:1000] if v.get("reason") else '"(no reason given)"'
        print(f"  Verifier {i + 1}/{panel} ({models[i]}): refuted={v.get('refuted')} "
              f"confidence={v.get('confidence')} — reason: {reason}")

    verdict = require_approvals(votes, models, required_approver, min_others, challenge)
    if verdict["block"]:
        print(f"BLOCK required-approver gate: {verdict['reason']}", file=sys.stderr)
        return 1
    attest = attest_reasons(votes, panel)
    if attest["block"]:
        print(f"BLOCK integrity gate (sham green): {attest['reason']}", file=sys.stderr)
        return 1
    proof = attest_proof(votes, challenge, panel)
    if proof["block"]:
        print(f"BLOCK proof-of-check gate: {proof['reason']}", file=sys.stderr)
        return 1
    print(f"[independent-verify] Cross-vendor panel confirms (required approver: {verdict['reason']}; "
          f"{attest['reason']}; {proof['reason']}). Green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
