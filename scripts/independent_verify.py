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
  1a. attest_consistency() — a GREEN vote must not itself report a defect. Two
                           channels, in this order: (a) the DECLARED DEFECT
                           LEDGER, a required typed ``defects: string[]`` in the
                           verdict schema — refuted=false with a non-empty
                           ledger is a self-contradicting vote and blocks; this
                           channel reads len(), never prose, so no phrasing
                           defeats it. (b) a demoted PROSE TRIPWIRE over the
                           vote's own reason — a heuristic, known-evadable,
                           retained only because the ledger is only as good as
                           the model that fills it.
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
  VERIFIER_REQUIRE_DEFECT_LIST    a GREEN vote MUST carry a "defects" ledger (default
                                  off; turn on only once the run logs show every
                                  configured voice emitting the field, then it is on
                                  for good — see docs/INDEPENDENT_REVIEW_PANEL.md)

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
def normalize_base(raw: str) -> str:
    """Trailing slashes off. Every call site builds f"{BASE}/models" and
    friends, so a base ending in "/" produces "//models", which this gateway
    answers 404 — a configuration slip that would otherwise surface as an
    unexplained panel outage. Verified live: ".../v1/models" 200,
    ".../v1//models" 404."""
    return (raw or "").strip().rstrip("/")


# `or`, not .get(default): the workflow passes VERIFIER_BASE_URL from a repo
# VARIABLE, and GitHub Actions injects an EMPTY STRING when the variable is
# unset — .get(name, default) would keep "" and every request would crash with
# "unknown url type" (observed live on PR #21). The reference JS used ||,
# which is empty-string-safe; this is the Python equivalent.
BASE = normalize_base(os.environ.get("VERIFIER_BASE_URL")) or "https://api.openai.com/v1"


def auth_header() -> dict[str, str]:
    """The configured gateway's auth header. Defaults to the OpenAI convention
    ``Authorization: Bearer``. VERIFIER_AUTH_HEADER names a DIFFERENT header for
    gateways that reserve Authorization for upstream forwarding — verified live
    against inference.klee.me, whose providers.openai runs authMode="forward"
    and answers Bearer with 401 "opencodex API key required" while accepting
    X-OpenCodex-API-Key. Same `or`-not-`.get(default)` reason as BASE above:
    Actions injects an EMPTY STRING for an unset repo variable."""
    name = (os.environ.get("VERIFIER_AUTH_HEADER") or "").strip()
    if name and name.lower() != "authorization":
        return {name: KEY or ""}
    return {"Authorization": f"Bearer {KEY}"}

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


def approving_models(votes: list[dict], models: list[str]) -> list[str]:
    """Models with a VALID, APPROVING voice. An errored voice never approves, so
    an outage shrinks this list -- which is the point."""
    return [models[i] for i, x in enumerate(votes)
            if _is_valid(x) and x["v"]["refuted"] is False and models[i]]


def require_distinct_voices(models: list[str]) -> dict[str, Any]:
    """Every voice must be a DIFFERENT configured id.

    Independence here is a property of the panel's composition, not of anything
    this script can inspect: each voice is an inference-server GROUP that
    rotates over its own members, and the server reports the group id back as
    the model, never the member that served the call. Probed live -- a request
    to `combo/SOTA-A` answers with `"model": "combo/SOTA-A"`.

    So the operator attests that the groups are independent, and this gate
    enforces the one part that IS checkable: that two voices are not the same
    group. Two identical ids are one opinion counted twice, which is precisely
    the shape the required-approver gate exists to prevent.

    RESIDUAL, and it cannot be closed from here: if two groups share a member,
    rotation can land both voices on one model. Nothing in the API distinguishes
    that from genuine agreement. It is bounded by how the groups are built."""
    seen = [m for m in models if m]
    dupes = sorted({m for m in seen if seen.count(m) > 1})
    if dupes:
        return {"block": True,
                "reason": f"the panel lists the same voice more than once ({', '.join(dupes)}) "
                          "-- one opinion counted twice is not corroboration, fail-closed"}
    return {"block": False,
            "reason": f"{len(seen)} distinct voices ({', '.join(seen)})"}


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
    """Votes that CARRY a green: explicit approvals only (refuted is False).
    Round-8 panel finding: the previous filter ("everything decide() does not
    block") also admitted LOW-CONFIDENCE REFUTATIONS, so a dissenter's
    reason/proof could satisfy the attestation majorities while an actual
    approving corroborator rode through canned and unattested. A low-confidence
    refutation neither blocks nor attests."""
    return [x for x in votes if _is_valid(x) and x["v"]["refuted"] is False]


def strict_any_refutation(votes: list[dict], models: list[str]) -> dict[str, Any]:
    """OPT-IN strict mode (VERIFIER_STRICT_ANY_REFUTATION=true): block when ANY
    valid voice refutes with confidence high/medium — not only the required
    approver. OFF by default to stay mechanism-identical to the reference,
    whose documented semantics green on Sol + one corroborator even if a third
    voice refutes (that property was flagged by the panel itself — Sol veto on
    PR #21 — and is hereby operator-selectable)."""
    for i, x in enumerate(votes):
        if _is_valid(x) and decide(x["v"])["block"]:
            return {"block": True,
                    "reason": f"strict mode: {models[i]} refutes "
                              f'(confidence={x["v"].get("confidence", "?")}) -> fail-closed'}
    return {"block": False, "reason": "strict mode: no high/medium refutation"}


# --------------------------------------------------- declared defect ledger --
#
# WHY THIS FIELD EXISTS — read before touching anything below it.
#
# THREE predecessors of this gate tried to INFER A FACT FROM A STRING, and the
# panel refuted every one of them with an input its author had not imagined:
#   * vendor_key()    — a vendor from a model id's leading token. "gpt-5.6-sol"
#                       -> "gpt" but "openai/gpt-4.1-mini" -> "openai", so ONE
#                       vendor in two spellings satisfied a CROSS-vendor gate.
#   * vendor_tokens() — vendor identity from token-set disjointness. "nvidia/"
#                       is a HOST prefix, so Meta and DeepSeek read as one
#                       vendor. Wrong in both directions.
#   * defect_claims() — a defect CLAIM from a negator within three words. Also
#                       wrong in both directions, because the negator negates a
#                       different word than the defect one:
#                         "without fixing auth bypass"               -> no claim
#                         "not fixed: privilege escalation in admin" -> no claim
#                         "none of the auth paths bypass the check"  -> claim
#
# The common shape is not a bad word list. It is that a STRING WRITTEN BY THE
# REVIEWER was asked a question only the REVIEWER can answer. So the verdict
# schema now MAKES THE REVIEWER ANSWER IT in a typed field, and the gate reads
# the field instead of parsing the prose:
#
#     "defects": string[]   one entry per concrete defect found, [] iff none
#     refuted == False  AND  len(defects) > 0   ->  self-contradicting, BLOCK
#
# That decision is an integer comparison over a value the model itself declared.
# There is no vocabulary to miss, no negation to scope, no phrasing to shape:
# its input domain is {list of strings} + {everything else} and both halves are
# handled explicitly, so it has no "unimagined input" class at all.
#
# WHAT IT DOES NOT DO, stated here so nobody reads it as more than it is: a
# model that finds a defect, writes clean prose and declares [] is invisible to
# this channel. It converts a ONE-channel failure into a TWO-channel one (the
# model must now get the same fact wrong twice, in two representations, having
# been told they must agree) — it does not eliminate it. That residual is why
# the prose tripwire below is retained rather than deleted.

# Ledger entries a model writes when it means "empty". CLOSED set, and safe in
# the P1 direction because none of these strings can name a defect.
_LEDGER_NULLS = frozenset({
    "", "-", "--", "n/a", "na", "nil", "null", "none", "nothing", "empty", "[]",
    "no defect", "no defects", "no defects found", "none found", "nothing found",
    "no issue", "no issues", "no issues found", "no findings", "no concerns",
})

# Accepted spellings of the ledger key, in priority order. A model that answers
# the schema with "Defects" or "findings" HAS answered it; treating that as an
# absent field would silently disarm the gate on a capitalisation slip.
_LEDGER_KEYS = ("defects", "defect", "findings", "defects_found")


def _ledger_fields(v: Any) -> list[Any]:
    """EVERY accepted spelling of the ledger present on the vote.

    All of them, not the first one: a vote carrying both ``"defects": []`` and
    ``"findings": ["auth bypass"]`` has declared a defect, and reading only the
    higher-priority key would let the other one hide it."""
    if not isinstance(v, dict):
        return []
    return [v[k] for k in v
            if isinstance(k, str) and k.strip().lower() in _LEDGER_KEYS]


def declared_defects(v: Any) -> tuple[str, list[str]]:
    """Read a vote's MACHINE-DECLARED defect ledger.

    Returns ("ok", items) | ("missing", []) | ("malformed", []). Total on every
    input: no parsing, no inference, no natural language. ``items`` is the
    union of the declared lists with the enumerated empty-sentinels removed."""
    raws = _ledger_fields(v)
    if not raws:
        return ("missing", [])
    items: list[str] = []
    for raw in raws:
        # A bare string is a schema error, but a bare string that IS one of the
        # enumerated empty-sentinels ('"defects": "none"') cannot name a defect,
        # so reading it as [] is safe in the P1 direction and removes the most
        # likely rollout wedge. Any OTHER non-list fails closed.
        if isinstance(raw, str):
            if re.sub(r"\s+", " ", raw).strip().lower() in _LEDGER_NULLS:
                continue
            return ("malformed", [])
        if not isinstance(raw, list):
            return ("malformed", [])
        for entry in raw:
            if not isinstance(entry, str):
                return ("malformed", [])
            s = re.sub(r"\s+", " ", entry).strip()
            if s.lower() not in _LEDGER_NULLS:
                items.append(s)
    return ("ok", items)


# ------------------------------------------------------- prose tripwire ------
#
# SECONDARY, DEMOTED, AND HONESTLY LABELLED: everything from here to
# defect_claims() IS a heuristic and a determined writer evades it. It is kept
# unchanged — deliberately NOT rewritten — because three rewrites of exactly
# this rule have now been refuted, each shipping a fresh BYPASS in exchange for
# the false positives it fixed. The known holes are documented on
# defect_claims() itself. It is the belt to the ledger's braces, not the
# mechanism.

# Words that name a DEFECT rather than describe a review. Deliberately narrow:
# each denotes a concrete failure, not a hedge ("could", "consider", "prefer"),
# so ordinary approving prose does not trip the gate.
#
# INFLECTIONS: the list is matched in the ordinary inflections of the SAME
# terms ("bypassed", "injections", "deadlocks"), which the earlier bare-\b form
# missed. Widening a term is MONOTONE — it can only turn a PASS into a BLOCK,
# never the reverse — so it trades P2 for P1 in the direction this file
# resolves conflicts. Adding a NEW concept here is not monotone in the same
# way and is a separate, reviewed decision.
_DEFECT_WORDS = re.compile(
    r"\b(bypass\w*|fail-open\w*|fails? open|unauthenticated|"
    r"inject(?:ions|ion|ed|able|s)|vulnerab\w*|exploitab\w*|"
    r"race conditions?|deadlock\w*|regress\w*|data loss(?:es)?|"
    r"privilege escalations?)\b", re.I)

# A defect word is only a defect CLAIM when it is not negated. "no concrete
# regression" is an explicit all-clear, and scoring it as a claim blocked a
# panel on run 32181953531 in which all three voices had approved -- the gate
# refused a unanimous green because one reason contained the word "regression"
# immediately after the word "no".
#
# The window is deliberately three words. A negation anywhere in a long reason
# must NOT launder a real claim later in it, which is why this is a per-match
# check and why every match has to be negated for the vote to pass.
_NEGATORS = re.compile(r"\b(no|not|none|never|without|free of|absent|zero|nothing|n't)\b", re.I)


def defect_claims(reason: str) -> list[str]:
    """Defect words in ``reason`` that are NOT negated in their own context.

    DEMOTED HEURISTIC — the second channel of attest_consistency(), never the
    mechanism. Its measured holes, kept in the source so no future reader
    mistakes it for a decision procedure:

        defect_claims("without fixing auth bypass")               == []
        defect_claims("merged without addressing the injection")  == []
        defect_claims("not fixed: privilege escalation in admin") == []
        defect_claims("none of the auth paths bypass the check")  == ["bypass"]

    Three attempts to repair those (a scope-terminator list, an anchored
    all-clear recogniser, a published reserved-word contract) were each refuted
    by the panel with a NEW bypass the author had not imagined — an unbounded
    left-scan that let "no blockers and an auth bypass on /admin" through, an
    anchor that quantified the wrong noun in "no injection sanitisation", and a
    published wordlist that missed "auth is bypassed". The three-word window is
    crude, but it is BOUNDED, and boundedness is what stops one all-clear from
    laundering a claim later in the same string. DO NOT rewrite this rule to
    win back false positives; move the finding into the ledger instead."""
    claims = []
    for m in _DEFECT_WORDS.finditer(reason or ""):
        preceding = " ".join((reason[max(0, m.start() - 40):m.start()]).split()[-3:])
        if not _NEGATORS.search(preceding):
            claims.append(m.group(0))
    return claims


def attest_consistency(votes: list[dict], models: list[str],
                       require_ledger: bool = False) -> dict[str, Any]:
    """A GREEN vote that itself reports a defect is not an approval — it is a
    model that analysed correctly and then set the boolean wrong. decide() reads
    only ``refuted``, so without this gate that vote counts toward the quorum
    AND supplies a substantive, distinct reason that helps attest_reasons pass.

    Found adversarially: a panelist returned refuted=false with a reason that
    named two concrete defects.

    THE LADDER, per green vote, first hit wins, every rung fail-CLOSED:
      1. a ``defects`` ledger that is present but is not a list of strings
         -> the schema was not answered, BLOCK.
      2. NO ledger at all -> BLOCK only under VERIFIER_REQUIRE_DEFECT_LIST
         (``require_ledger``). Tolerated by default ON PURPOSE: a voice that
         has not seen the new prompt would otherwise block every unanimous
         green, which is the failure mode that gets a gate deleted. Rung 3
         still applies to it, so tolerating absence costs nothing that the
         file did not already cost before the ledger existed.
      3. ledger non-empty -> the model declared a defect and greened anyway,
         BLOCK. THIS RUNG IS THE MECHANISM: two declarations about one fact,
         compared as values, no prose read, nothing inferred.
      4. ledger empty (or absent) but the vote's own prose names an uncleared
         defect word -> BLOCK. Demoted heuristic, evadable by construction;
         retained because P1 outranks P2 and rung 3 is only as good as the
         model that fills the ledger.

    WHERE P1 AND P2 CONFLICT, P1 WINS, at every rung. A false block costs one
    re-vote; a false pass costs a merged defect that three voices signed."""
    for i, x in enumerate(votes):
        if not _is_valid(x) or x["v"]["refuted"] is not False:
            continue
        state, declared = declared_defects(x["v"])
        if state == "malformed":
            return {"block": True,
                    "reason": f'{models[i]}: refuted=false and its "defects" ledger is not a '
                              "JSON array of strings -> schema not answered, fail-closed"}
        if state == "missing" and require_ledger:
            return {"block": True,
                    "reason": f'{models[i]}: refuted=false without the required "defects" '
                              "ledger while VERIFIER_REQUIRE_DEFECT_LIST=true "
                              "-> unverifiable green, fail-closed"}
        if declared:
            return {"block": True,
                    "reason": f'{models[i]}: refuted=false but its own defects ledger declares '
                              f'{len(declared)} defect(s) ("{declared[0][:120]}") '
                              "-> the boolean contradicts the vote's own findings, fail-closed"}
        claims = defect_claims(norm_reason(x["v"].get("reason")))
        if claims:
            return {"block": True,
                    "reason": f'{models[i]}: refuted=false with an empty defects ledger, but '
                              f'its own reason names a defect ("{claims[0]}") '
                              "-> inconsistent vote, fail-closed"}
    return {"block": False,
            "reason": f"{len(_green(votes))} green vote(s): empty defect ledger, "
                      "no defect claim in their prose"}


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
# Data-file classes added after the panel's own round-3 finding (root-level
# customers.csv/backup.sql would have gone to the vendor). NOTE: .json stays
# reviewable ON PURPOSE — frozen_methodology.json IS the methodology and must
# be visible to the panel. A denylist is inherently incomplete; that residual
# is documented in docs/INDEPENDENT_REVIEW_PANEL.md.
EXCLUDE_EXTS = ["webp", "png", "jpg", "jpeg", "gif", "ico", "svg", "avif", "bmp", "tiff",
                "woff", "woff2", "ttf", "otf", "eot", "pdf", "geojson", "db", "rds", "xlsx",
                "csv", "tsv", "sql", "jsonl", "ndjson", "parquet", "feather", "sqlite",
                "sqlite3", "dump", "bak", "pickle", "pkl", "npz", "npy"]
# icase: pathspecs are case-sensitive by default — an uppercase .PNG/.SVG would
# otherwise reach the vendor (found by the panel itself: Sol veto on PR #21).
_EXCLUDES = [":(exclude,icase,glob)data/**"] + [f":(exclude,icase,glob)**/*.{e}" for e in EXCLUDE_EXTS]


class DiffError(RuntimeError):
    """A REQUIRED diff command failed — the panel must BLOCK, never green on
    the resulting emptiness (round-6 panel finding: text=True decode errors and
    git failures were silently converted to empty output)."""


class ProviderConfigError(RuntimeError):
    """The endpoint is misconfigured, so no panel can be assembled. Distinct
    from a vote failure: this must name the CAUSE, because the previous
    behaviour (fall back to one pinned model for every voice) turned a wrong
    BASE URL into three identical, unexplained vote errors."""


def _sh(args: list[str], *, required: bool = False) -> str:
    """Run a git command; decode with errors="replace" so invalid UTF-8 becomes
    VISIBLE replacement characters instead of an empty (falsely reviewable)
    diff. required=True turns any failure into a blocking DiffError."""
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed git argv built from constants, no shell
            args, capture_output=True, timeout=120)
    except Exception as exc:
        if required:
            raise DiffError(f"{' '.join(args[:3])}...: {exc}") from exc
        return ""
    out = proc.stdout.decode("utf-8", errors="replace")
    if required and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:200]
        raise DiffError(f"{' '.join(args[:3])}... exited {proc.returncode}: {err}")
    return out


def base_branch() -> str:
    """The PR's target branch (GITHUB_BASE_REF; empty on non-PR events -> main).
    Round-4 panel finding: a hard-coded origin/main mis-based the diff for PRs
    targeting any other branch, letting them green unreviewed."""
    return os.environ.get("GITHUB_BASE_REF") or "main"


def review_range() -> tuple[str, str]:
    """(merge_base, head) for the review.

    The head comes EXPLICITLY from VERIFIER_HEAD_SHA, because the job runs from
    the DEFAULT BRANCH (pull_request_target) where HEAD *is* main: without this
    merge-base(main, HEAD) diffs main against itself, yields an empty diff and
    goes permanently FAKE-GREEN. Reproduced before this guard existed, so the
    empty-diff path is fail-CLOSED whenever a candidate sha was supplied.

    The sha is validated as 40-hex and must not be an ancestor of the base --
    an ancestor means there is nothing to review, which in a PR context is a
    fault, not an approval."""
    head = (os.environ.get("VERIFIER_HEAD_SHA") or "").strip() or "HEAD"
    base = (os.environ.get("VERIFIER_BASE_BRANCH") or "").strip() or base_branch()
    if head != "HEAD" and not re.fullmatch(r"[0-9a-f]{40}", head):
        raise DiffError(f"VERIFIER_HEAD_SHA is not a 40-hex sha: {head!r}")
    mb = _sh(["git", "merge-base", f"origin/{base}", head], required=True).strip()
    if not mb:
        raise DiffError(f"empty merge-base for origin/{base}...{head}")
    if mb == _sh(["git", "rev-parse", head], required=True).strip():
        raise DiffError(
            f"candidate {head[:12]} is an ancestor of origin/{base} — nothing to "
            f"review; in a PR context that is a fault, fail-closed")
    return mb, head


def diff_commands(merge_base: str, head: str = "HEAD") -> dict[str, list[str]]:
    """The three diff invocations. The NAME-STATUS list carries ALL changed
    paths WITHOUT excludes (round-4 panel finding: filtering the authoritative
    list let an excluded-only PR return an empty diff and auto-green with zero
    votes) — paths are not content; the privacy excludes protect CONTENTS and
    stay on stat/body."""
    return {
        "names": ["git", "diff", "--name-status", f"{merge_base}...{head}"],
        "stat": ["git", "diff", "--stat", f"{merge_base}...{head}", "--", "."] + _EXCLUDES,
        "body": ["git", "diff", f"{merge_base}...{head}", "--", "."] + _EXCLUDES,
    }


def truncate_marked(text: str, cap: int, label: str) -> str:
    """Cap text with an EXPLICIT marker — silent truncation let unreviewed
    changes green (panel round-3 finding); a cut must be visible to reviewers."""
    if len(text) <= cap:
        return text
    return (text[:cap] +
            f"\n[{label} TRUNCATED — {len(text) - cap} of {len(text)} bytes omitted]")


def build_diff() -> str:
    """COMPLETE changed-file list (--name-status) + capped stat + capped body,
    every cap explicitly marked; base = merge-base with main."""
    # No HEAD~1 fallback (round-7 panel finding): a failed merge-base on a
    # multi-commit PR would silently shrink the review to the TIP commit only.
    # checkout runs with fetch-depth: 0, so origin/<base> is always present in
    # CI; a merge-base failure is a real fault and must BLOCK.
    mb, head = review_range()
    cmds = diff_commands(mb, head)
    names = _sh(cmds["names"], required=True)
    stat = _sh(cmds["stat"], required=True)
    body = _sh(cmds["body"], required=True)
    if not names.strip() and not body.strip():
        return ""
    return (f"# COMPLETE changed-file list (authoritative — ALL changed paths, including "
            f"files whose CONTENT is privacy-excluded; contents of excluded classes are "
            f"never sent):\n"
            f"{truncate_marked(names, 100_000, 'FILE LIST')}\n\n"
            f"# Diffstat:\n{truncate_marked(stat, 8_000, 'DIFFSTAT')}\n\n"
            f"# Code changes (binaries/assets/data excluded):\n"
            f"{truncate_marked(body, 50_000, 'DIFF BODY')}")


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
        "attack path). The changed-FILE LIST is COMPLETE; diffstat/body may be truncated, "
        "and any truncation is explicitly marked — if a marked truncation hides files you "
        "would need to see to approve, say so instead of approving blind. "
        "No concrete failure path -> refuted=false. Do NOT refute over the "
        "inherent cross-vendor trust assumption itself (that a malicious endpoint could fool "
        "the verifier including the challenge echo) — that is the DOCUMENTED residual, "
        "compensated by the deterministic CI gate; it is not a defect of THIS diff. "
        "Answer ONLY as JSON, no prose/markdown: "
        '{"refuted": boolean, "confidence": "high"|"medium"|"low", "reason": string, '
        '"defects": string[], "proof": string}. '
        'DEFECT LEDGER: "defects" is REQUIRED on every answer and is a JSON array of '
        "strings — ONE ENTRY PER CONCRETE DEFECT you found, each in the schema "
        "'path/file:line — defect — misbehavior', and the EMPTY array [] if you found "
        'none. Every defect you can name anywhere in your answer MUST also appear in '
        '"defects", and refuted MUST be true whenever "defects" is non-empty. Never '
        "leave a finding out because you judged it minor: if you can name it, list it "
        "and refute on it. ALWAYS fill reason (also for refuted=false), maximally terse/"
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
    CH2 = "selftest-challenge"
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
    CH = "selftest-challenge"
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
    # attest_consistency() -- the DECLARED LEDGER is the mechanism; the prose
    # tripwire is the demoted second channel. GV() omits the ledger (the
    # tolerated pre-rollout shape); GL() declares one.
    def GV(reason: Any, refuted: bool = False) -> dict:
        return {"ok": True, "v": {"refuted": refuted, "reason": reason, "confidence": "high"}}

    def GL(reason: Any, refuted: bool = False, defects: Any = ()) -> dict:
        return {"ok": True, "v": {"refuted": refuted, "reason": reason,
                                  "confidence": "high", "defects": list(defects)}}
    M3 = ["m-a", "m-b", "m-c"]
    # -- declared_defects(): total on every input, nothing inferred
    expect(declared_defects({"defects": []}) == ("ok", []), "an empty ledger reads as ok/[]")
    expect(declared_defects({"defects": ["a.py:1 — x — y"]})[1] == ["a.py:1 — x — y"],
           "a declared defect is returned verbatim")
    expect(declared_defects({})[0] == "missing", "an absent ledger is missing")
    expect(declared_defects(None)[0] == "missing", "a non-dict vote has no ledger")
    expect(declared_defects({"defects": "none"}) == ("ok", []),
           "a bare-string sentinel ledger reads as empty -- it cannot name a defect")
    expect(declared_defects({"defects": "auth bypass"})[0] == "malformed",
           "any OTHER bare string is an unanswered schema, fail-closed")
    expect(declared_defects({"defects": [], "findings": ["auth bypass"]})[1] == ["auth bypass"],
           "every accepted key is read -- one must not hide behind another")
    expect(declared_defects({"defects": None})[0] == "malformed", "a null ledger is malformed")
    expect(declared_defects({"defects": [{"f": 1}]})[0] == "malformed",
           "non-string entries are malformed")
    expect(declared_defects({"defects": ["none", "", " N/A "]}) == ("ok", []),
           "enumerated empty-sentinels are an empty ledger, not defects")
    expect(declared_defects({"Defects": ["x"]}) == ("ok", ["x"]),
           "a capitalisation slip must not silently disarm the ledger")
    expect(declared_defects({"findings": ["x"]}) == ("ok", ["x"]),
           "an accepted alias key is still an answered schema")
    # -- rung 3: the ledger itself. No prose is read on this rung.
    _d = attest_consistency([GL("looks fine to me",
                                defects=["api.py:7 — authz skipped — any user reads /admin"]),
                             GL("docs only"), GL("fine")], M3)
    expect(_d["block"] is True and "ledger declares" in _d["reason"],
           "refuted=false with a NON-EMPTY declared ledger is the inconsistency -> block")
    expect(attest_consistency([GL("all good, nothing of note", defects=["x.py:1 — y — z"]),
                               GL("docs only"), GL("fine")], M3)["block"] is True,
           "the ledger blocks even when the prose is spotless -- no prose parsing involved")
    expect(attest_consistency([GL("fail-open on missing header", True, defects=["h.py:3 — fo"]),
                               GL("docs only"), GL("no issue found")], M3)["block"] is False,
           "a REFUTING vote may declare defects -- that is its job")
    expect(attest_consistency([GL("docs only change", defects=["none"]), GL("docs only"),
                               GL("fine")], M3)["block"] is False,
           "a sentinel-only ledger is an empty ledger, not a declared defect")
    # -- rungs 1 and 2: schema posture
    expect(attest_consistency([{"ok": True, "v": {"refuted": False, "reason": "docs only",
                                                  "defects": "auth bypass on /admin"}},
                               GL("docs only"), GL("fine")], M3)["block"] is True,
           "a malformed (non-array) ledger fails closed, with or without the env switch")
    expect(attest_consistency([GV("docs only change"), GV("docs only"), GV("fine")],
                              M3)["block"] is False,
           "an ABSENT ledger is tolerated by default -- a pre-rollout voice must not block")
    expect(attest_consistency([GV("docs only change"), GV("docs only"), GV("fine")],
                              M3, require_ledger=True)["block"] is True,
           "VERIFIER_REQUIRE_DEFECT_LIST=true makes an absent ledger an unverifiable green")
    expect(attest_consistency([E, GL("docs only"), GL("no issue")], M3,
                              require_ledger=True)["block"] is False,
           "an errored vote has no ledger to read")
    expect(attest_consistency([GV("checked auth paths, no issue"), GV("docs only change"),
                               GV("reviewed diff, behaviour unchanged")], M3)["block"] is False,
           "ordinary approving reasons must pass")
    expect(attest_consistency([GV("checked auth paths"), GV("auth bypass when key is None"),
                               GV("docs only")], M3)["block"] is True,
           "green vote naming a bypass must fail-closed")
    expect(attest_consistency([GV("fail-open on missing header", True), GV("docs only"),
                               GV("no issue found")], M3)["block"] is False,
           "a REFUTING vote may name a defect -- that is its job")
    expect(attest_consistency([GV("could be more defensive; consider hardening"), GV("ok looks fine"),
                               GV("no problems seen")], M3)["block"] is False,
           "hedging words are not defect claims")
    expect(attest_consistency([E, GV("docs only"), GV("no issue")], M3)["block"] is False,
           "an errored vote is not inspected for consistency")
    expect(attest_consistency([GV("security gates reviewed; no concrete regression"),
                               GV("docs only"), GV("looks fine")], M3)["block"] is False,
           "a NEGATED defect word is an all-clear, not a defect claim (run 32181953531)")
    expect(attest_consistency([GV("no bypass here, but there is an injection in the parser"),
                               GV("docs only"), GV("fine")], M3)["block"] is True,
           "one negation must not launder a real claim later in the same reason")
    expect(defect_claims("no regression, no bypass") == [],
           "every match negated -> no claims")
    expect(defect_claims("regression in the dispatcher") == ["regression"],
           "an unnegated defect word is still a claim")
    # Ordinary inflections of the SAME published terms are matched too. Widening
    # a term is monotone (PASS -> BLOCK only), which is the direction this file
    # resolves P1/P2 conflicts in.
    expect(defect_claims("auth is bypassed when key is None") == ["bypassed"],
           "an inflected defect word is still a claim")
    expect(defect_claims("deadlocks on shutdown remain") == ["deadlocks"],
           "a plural defect word is still a claim")
    expect(defect_claims("two sql injections in the parser remain") == ["injections"],
           "a plural defect word is still a claim")
    expect(defect_claims("no bypasses, no regressions") == [],
           "monotone widening does not un-negate an all-clear")
    # THE SHARED ATTACK CORPUS, scored through the REAL gate in the two modes
    # that matter. Mode B: the voice fills the ledger honestly -> 7/7 blocked,
    # and the boolean never has to be read out of prose at all.
    _CORPUS_BLOCK = [
        "auth bypass when key is None and a race condition on refresh",
        "without fixing auth bypass",
        "merged without addressing the injection",
        "not fixed: privilege escalation in admin",
        "no bypass here, but there is an injection in the parser",
        "looks good; minor: unauthenticated /readyz leaks the key",
        "approved, though the deadlock on shutdown remains",
    ]
    _CORPUS_PASS = [
        "security gates reviewed; no concrete regression",
        "docs only change",
        "reviewed diff, behaviour unchanged",
        "could be more defensive; consider hardening",
        "checked auth paths, no issue",
        "no regression, no bypass, no injection",
    ]
    for _r in _CORPUS_BLOCK:
        expect(attest_consistency([GL(_r, defects=[_r]), GL("docs only"), GL("fine")],
                                  M3)["block"] is True,
               f"P1, declared ledger: a green that declares its finding must block: {_r!r}")
    for _r in _CORPUS_PASS:
        expect(attest_consistency([GL(_r), GL("docs only"), GL("fine")], M3)["block"] is False,
               f"P2: an ordinary approval must not block: {_r!r}")
    # Mode C: the voice HIDES the finding (declares [] and writes the defect in
    # prose). Only the demoted tripwire is left, and it catches 4 of 7. These
    # three are its DOCUMENTED, UNREPAIRED holes -- pinned so that a future
    # rewrite of defect_claims() has to confront them rather than rediscover
    # them, and so that nobody reports 7/7 for a mode that measures 4/7.
    for _r in ("auth bypass when key is None and a race condition on refresh",
               "no bypass here, but there is an injection in the parser",
               "looks good; minor: unauthenticated /readyz leaks the key",
               "approved, though the deadlock on shutdown remains"):
        expect(attest_consistency([GL(_r), GL("docs only"), GL("fine")], M3)["block"] is True,
               f"P1, hidden finding: the tripwire still catches: {_r!r}")
    for _r in ("without fixing auth bypass",
               "merged without addressing the injection",
               "not fixed: privilege escalation in admin"):
        expect(attest_consistency([GL(_r), GL("docs only"), GL("fine")], M3)["block"] is False,
               f"KNOWN HOLE of the demoted tripwire, do not paper over it: {_r!r}")

    # auth_header() -- both branches
    _saved = os.environ.pop("VERIFIER_AUTH_HEADER", None)
    try:
        expect("Authorization" in auth_header(), "default must be Authorization: Bearer")
        os.environ["VERIFIER_AUTH_HEADER"] = "X-OpenCodex-API-Key"
        expect(list(auth_header()) == ["X-OpenCodex-API-Key"], "custom header must replace Authorization")
        os.environ["VERIFIER_AUTH_HEADER"] = ""
        expect("Authorization" in auth_header(), "empty variable (unset Actions var) -> default")
        os.environ["VERIFIER_AUTH_HEADER"] = "authorization"
        expect("Authorization" in auth_header(), "case-insensitive 'authorization' -> default Bearer form")
    finally:
        os.environ.pop("VERIFIER_AUTH_HEADER", None)
        if _saved is not None:
            os.environ["VERIFIER_AUTH_HEADER"] = _saved

    # review_range() -- a non-hex candidate sha must never reach git
    _sv = os.environ.get("VERIFIER_HEAD_SHA")
    try:
        os.environ["VERIFIER_HEAD_SHA"] = "not-a-sha; rm -rf /"
        try:
            review_range()
            expect(False, "non-hex VERIFIER_HEAD_SHA must raise DiffError")
        except DiffError:
            pass
    finally:
        os.environ.pop("VERIFIER_HEAD_SHA", None)
        if _sv is not None:
            os.environ["VERIFIER_HEAD_SHA"] = _sv

    # normalize_base() -- a trailing slash builds "//models", answered 404
    expect(normalize_base("https://h/v1/") == "https://h/v1", "trailing slash must be stripped")
    expect(normalize_base("https://h/v1///") == "https://h/v1", "repeated slashes must be stripped")
    expect(normalize_base("  https://h/v1  ") == "https://h/v1", "surrounding whitespace must be stripped")
    expect(normalize_base("") == "", "empty stays empty so the `or` default applies")
    expect(normalize_base(None) == "", "None stays empty so the `or` default applies")

    # Voice distinctness. Each configured voice is an inference-server GROUP that
    # rotates over its own members; the server answers with the group id, never
    # the member, so this script cannot see a vendor at all. What it CAN check is
    # that two voices are not the same group.
    expect(require_distinct_voices(["combo/SOTA-A", "combo/SOAT-B", "combo/SOTA-C"])["block"] is False,
           "three distinct groups are three voices")
    _dup = require_distinct_voices(["combo/SOTA-A", "combo/SOTA-A", "combo/SOTA-C"])
    expect(_dup["block"] and "more than once" in _dup["reason"],
           "one opinion counted twice is not corroboration")
    expect(require_distinct_voices(["combo/SOTA-A", "", "combo/SOTA-C"])["block"] is False,
           "an empty slot is not a duplicate; resolution failure is caught before this gate")
    _G = ["combo/SOTA-A", "combo/SOAT-B", "combo/SOTA-C"]
    expect(approving_models([A, {"ok": False, "reason": "504"}, RF], _G) == ["combo/SOTA-A"],
           "an errored or refuting voice never counts as approving")
    # The composition rule: A must agree, plus either B or C.
    expect(not require_approvals([A, A2, {"ok": False, "reason": "504"}], _G, "combo/SOTA-A", 1)["block"],
           "group A plus one other is the quorum")
    expect(require_approvals([A, {"ok": False, "reason": "504"}, {"ok": False, "reason": "504"}],
                             _G, "combo/SOTA-A", 1)["block"],
           "group A alone is not corroboration")
    expect(require_approvals([{"ok": False, "reason": "504"}, A, A2], _G, "combo/SOTA-A", 1)["block"],
           "B and C without A cannot carry the panel")
    # NOTE: resolve_panel_models fails closed on an unresolvable voice, but it
    # needs the model catalogue, so its coverage is in tests/test_independent_verify.py
    # (TestResolutionFailsClosed) rather than here.

    print("   OK selftest: decide() + model_matches() + require_approvals() (required approver Sol "
          "+ corroboration) + attest_reasons() + attest_proof() + declared_defects() + "
          "attest_consistency() (ledger 7/7 on the shared corpus; demoted prose tripwire 4/7, "
          "3 known holes pinned) + auth_header() + review_range() + normalize_base() correct.")


# --------------------------------------------------------------- API plumbing --


def _http_json(url: str, payload: dict | None = None, timeout: int = 180) -> tuple[int, Any]:
    req = urllib.request.Request(  # noqa: S310 -- operator-configured https endpoint (VERIFIER_BASE_URL)
        url, headers={"Content-Type": "application/json", **auth_header()},
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


def fetch_model_ids() -> tuple[list[str] | None, str]:
    """(model ids, diagnostic). The diagnostic carries the status and a short
    body so a misconfigured endpoint can be READ from the log rather than
    guessed at from three identical vote errors."""
    status, data = _http_json(f"{BASE}/models")
    if status == 200 and isinstance(data, dict):
        return [m.get("id") for m in data.get("data", []) if m.get("id")], ""
    return None, f"GET {BASE}/models -> {status or 'no response'}: {str(data)[:200]}"


def resolve_panel_models(panel_size: int, wanted: list[str]) -> list[str]:
    if os.environ.get("VERIFIER_MODEL"):
        return [os.environ["VERIFIER_MODEL"]] * panel_size
    ids, why = fetch_model_ids()
    if not ids:
        # An unreachable catalogue used to degrade silently to FALLBACK_MODEL
        # for every voice; each vote then errored on a model the gateway does
        # not serve, and the operator saw three identical failures with no hint
        # that the BASE URL was the cause. The most common cause is exactly
        # that: a base without the version segment, or with a trailing slash.
        # Verified live: ".../v1/models" 200, ".../models" 401, ".../v1//models" 404.
        raise ProviderConfigError(
            f"model catalogue unreachable, so the panel cannot be resolved -- {why}\n"
            f"  VERIFIER_BASE_URL is {BASE!r}. It must include the API version "
            f"segment (e.g. 'https://host/v1'), and the auth header must be the "
            f"one this gateway expects (VERIFIER_AUTH_HEADER).")
    # SUBSTITUTION IS FAIL-OPEN, so it is gone. A configured voice that the
    # account does not serve used to print a WARNING and be replaced by the
    # newest available model. In a merge gate that is the worst possible
    # degradation: the panel reviews with voices the operator did not choose,
    # every gate downstream passes, and the run goes green. Two ways it fires
    # in practice, both cheap: a group not yet published to /v1/models, and a
    # one-character slip in an id -- `combo/SOAT-B` and `combo/SOTA-B` differ by
    # a transposition, and only one of them exists.
    #
    # A voice that cannot be resolved is a panel that cannot be assembled.
    missing = [w for w in wanted if not pick_for_pref(ids, w)]
    if missing:
        raise ProviderConfigError(
            "panel model(s) not enabled on this account: " + ", ".join(repr(m) for m in missing)
            + "\n  Refusing to substitute: reviewing with a voice the operator did not choose "
            "is a green run that reviewed the wrong thing.\n  Enabled ids are: "
            + ", ".join(sorted(ids)[:40])
            + "\n  Set VERIFIER_PANEL_MODELS to ids from that list.")
    return [pick_for_pref(ids, w) for w in wanted]


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


def should_fallback_responses(status: int, body: Any) -> bool:
    """Chat-completions rejection that names the Responses API — 404 or 400."""
    return (status in (400, 404) and isinstance(body, str)
            and re.search(r"v1/responses|responses endpoint|only supported in", body, re.I) is not None)


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
    # Some models only support the Responses API — the rejection may be a 404 OR
    # a documented 400 (panel round-3 finding: a 400-only model would have
    # blocked the panel permanently); switch on either when the body says so.
    if should_fallback_responses(status, data):
        s2, d2 = _http_json(f"{BASE}/responses",
                            {"model": model, "instructions": sys_prompt, "input": user_prompt})
        if s2 == 200:
            v = parse_verdict(_extract_responses_text(d2))
            return {"ok": True, "v": v, "decision": decide(v)}
        return {"ok": False, "status": s2, "reason": f"Responses API {s2}: {str(d2)[:300]}"}
    return {"ok": False, "status": status, "reason": f"API {status}: {str(data)[:300]}"}


# A 401 whose body names an exhausted upstream account pool is a DETERMINISTIC
# gateway state, not a flaky auth hiccup: retrying burns the backoff budget and
# still fails. Observed live on inference.klee.me while its OpenAI account pool
# was empty ("OpenAI account pool has no usable account credential").
_POOL_EXHAUSTED = re.compile(r"no usable account credential", re.I)


def verify_once(model: str, sys_prompt: str, user_prompt: str) -> dict:
    last: dict = {"ok": False, "reason": "no attempt executed"}
    for a in range(1, 4):
        last = attempt_once(model, sys_prompt, user_prompt)
        if last.get("ok"):
            return last
        if last.get("status") and not is_transient(last["status"]):
            return last            # deterministic error -> no retry
        if last.get("status") == 401 and _POOL_EXHAUSTED.search(str(last.get("reason", ""))):
            return last            # deterministic gateway state -> no retry
        if a < 3:
            # 4s, then 16s. The old 0.5s/1.0s was far too short for the real
            # failures seen against this gateway: 429 rate limits and a 504
            # gateway timeout on a model that had answered correctly minutes
            # before. A voice lost to an under-short backoff silently shrinks
            # the panel and can trip the fail-closed quorum gates.
            time.sleep(min(30.0, float(4 ** a)))
    return last


# --------------------------------------------------------------------- main --


def _md_cell(value: object, limit: int = 300) -> str:
    """Model-authored text, made safe for a markdown table cell.

    The reason comes from a language model reading an untrusted diff, so it is
    treated as hostile input to the RENDERER: pipes would break out of the
    cell, newlines out of the row, and backticks out of code spans. Collapsing
    whitespace and escaping the three metacharacters is enough — the value is
    never interpreted as anything but table text."""
    text = " ".join(str("" if value is None else value).split())
    text = text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")
    return (text[: limit - 1] + "…") if len(text) > limit else (text or "—")


def write_step_summary(votes: list[dict], models: list[str], gates: list[tuple[str, dict]],
                       blocked: bool) -> None:
    """Publish the panel's findings where a reviewer will actually see them.

    GITHUB_STEP_SUMMARY renders as markdown on the run page, one click from the
    pull request's check. Chosen over posting a pull-request review because it
    needs no token, no extra permission and no API call that could itself fail
    and turn a clean verdict into a red job.

    Written on BOTH paths. A panel that only explains itself when it blocks
    leaves the reader unable to tell 'three voices examined this and agreed'
    from 'the panel never ran'."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Independent-Verify — cross-vendor panel",
        "",
        f"**Verdict: {'BLOCKED' if blocked else 'APPROVED'}**",
        "",
        "| # | Model | Verdict | Confidence | Declared defects | Finding |",
        "|---|---|---|---|---|---|",
    ]
    for i, (vote, model) in enumerate(zip(votes, models, strict=False), start=1):
        if not vote.get("ok"):
            lines.append(f"| {i} | `{_md_cell(model, 60)}` | ⚠️ no vote | — | — "
                         f"| {_md_cell(vote.get('reason'))} |")
            continue
        v = vote.get("v") or {}
        refuted = v.get("refuted")
        mark = "🔴 refutes" if refuted else ("🟢 approves" if refuted is False else "⚠️ unparsable")
        # The ledger is the field the consistency gate now reads, so it is shown
        # next to the verdict: a reader must see BOTH declarations the gate
        # compares, not only the prose.
        state, declared = declared_defects(v)
        ledger = ("⚠️ " + state if state != "ok"
                  else ("none" if not declared else f"{len(declared)}: " + "; ".join(declared)))
        lines += [f"| {i} | `{_md_cell(model, 60)}` | {mark} "
                  f"| {_md_cell(v.get('confidence'), 12)} | {_md_cell(ledger, 200)} "
                  f"| {_md_cell(v.get('reason'))} |"]
    lines += ["", "### Gates", ""]
    for name, result in gates:
        state = "⛔ blocked" if result.get("block") else "✅ passed"
        lines.append(f"- **{name}** — {state}: {_md_cell(result.get('reason'), 400)}")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        # Never let the REPORT break the VERDICT.
        print(f"[independent-verify] could not write the step summary: {exc}")


def main() -> int:
    if "--selftest" in sys.argv:
        selftest()
        return 0

    if not KEY:
        # Fork-PR bypass (found by the panel itself, Sol veto round 2): GitHub
        # withholds secrets from fork-originated pull_request runs, so "no key"
        # on a fork PR is NOT the operator's documented residual state — it is
        # an untrusted origin that must FAIL CLOSED, or a required cross-vendor
        # check would pass with zero review. The workflow sets
        # VERIFIER_REQUIRE_KEY=true exactly when head repo != base repo.
        if (os.environ.get("VERIFIER_REQUIRE_KEY") or "").lower() == "true":
            print("BLOCK fork-origin run without a vendor key: secrets are withheld "
                  "from fork PRs, so the panel cannot review — fail-closed.", file=sys.stderr)
            return 1
        print("[independent-verify] RESIDUAL: no second-vendor key (SECOND_VENDOR_API_KEY or "
              "OPENAI_API_KEY) provisioned.\n"
              "  The independent cross-vendor review panel is NOT active. Compensation: the\n"
              "  deterministic CI gate remains the sole merge authority. To activate: set the\n"
              "  secret (see docs/INDEPENDENT_REVIEW_PANEL.md).")
        return 0   # same-repo only: no fake block; the residual is documented and visible

    try:
        d = build_diff()
    except DiffError as exc:
        print(f"BLOCK diff assembly failed — cannot review, fail-closed: {exc}", file=sys.stderr)
        return 1
    if not d.strip():
        # Defence in depth behind review_range()'s ancestor check: in a PR run
        # (candidate sha supplied) an empty diff means the range collapsed, not
        # that the change is harmless. Greening here is the fake-green path.
        if (os.environ.get("VERIFIER_HEAD_SHA") or "").strip():
            print("BLOCK empty diff for an explicit candidate sha — the review range "
                  "collapsed; fail-closed.", file=sys.stderr)
            return 1
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

    try:
        models = resolve_panel_models(panel, wanted)
    except ProviderConfigError as exc:
        print(f"BLOCK provider configuration — no panel could be assembled, "
              f"fail-closed: {exc}", file=sys.stderr)
        return 1
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

    # Gates in order, short-circuiting on the FIRST block — the order is the
    # semantics and does not change. What is new is that the gates evaluated so
    # far are collected, so the summary can name which one blocked and why.
    strict_on = (os.environ.get("VERIFIER_STRICT_ANY_REFUTATION") or "").lower() in ("1", "true", "yes")
    pending: list[tuple[str, str, Any]] = [
        ("distinct-voices gate", "distinct voices", lambda: require_distinct_voices(models)),
        ("required-approver gate", "required-approver",
         lambda: require_approvals(votes, models, required_approver, min_others, challenge)),
    ]
    if strict_on:
        pending.append(("strict-mode gate", "strict mode",
                        lambda: strict_any_refutation(votes, models)))
    require_ledger = (os.environ.get("VERIFIER_REQUIRE_DEFECT_LIST") or "").lower() in (
        "1", "true", "yes")
    pending += [
        ("consistency gate", "consistency",
         lambda: attest_consistency(votes, models, require_ledger)),
        ("integrity gate (sham green)", "reason attestation", lambda: attest_reasons(votes, panel)),
        ("proof-of-check gate", "proof of check", lambda: attest_proof(votes, challenge, panel)),
    ]

    gates: list[tuple[str, dict]] = []
    for log_name, summary_name, run in pending:
        result = run()
        gates.append((summary_name, result))
        if result["block"]:
            print(f"BLOCK {log_name}: {result['reason']}", file=sys.stderr)
            write_step_summary(votes, models, gates, blocked=True)
            return 1

    # The label is COMPUTED, never asserted. Printing "Cross-vendor" over a
    # single-vendor result is the defect this whole gate exists to close, and a
    # green run that hides a lost voice looks like a smaller panel that agreed.
    # NAME WHAT APPROVED. The previous form printed "Cross-vendor panel confirms"
    # unconditionally, and run 32121148827 printed it over two sibling models from
    # one vendor while the only other-vendor voice was returning API 504. This
    # script cannot see which vendor served a voice -- each voice is a group id
    # that the server resolves internally -- so it states the voices and leaves
    # the independence claim to whoever composed the groups.
    approving = approving_models(votes, models)
    label = f"Panel confirms on {len(approving)} of {panel} voices ({', '.join(approving)})"
    unreachable = [models[i] for i, x in enumerate(votes) if not x.get("ok")]
    note = (f"\n[independent-verify] NOTE: {len(unreachable)} of {panel} voices were "
            f"unreachable and did not vote: {', '.join(unreachable)}." if unreachable else "")
    print(f"[independent-verify] {label} ("
          + "; ".join(r["reason"] for _, r in gates) + f"). Green.{note}")
    write_step_summary(votes, models, gates, blocked=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
