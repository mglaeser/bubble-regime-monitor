"""Per-run challenges, so a verdict cannot be pre-computed or replayed.

EX3-R01. Without one, a review verdict is a JSON document that says the right
things, and nothing in it is tied to the run that asked. Two consequences, both
practical rather than theoretical:

* a verdict produced for an earlier commit can be presented for a later one,
  and every field in it still validates;
* the reviewed branch, which knows the schema, can produce a document that
  passes normalization without any model having read anything.

A challenge closes both. The run mints a secret seed, derives a per-unit token
from it, puts that token in the instructions the model is given, and refuses
any verdict whose `proof_of_check` does not carry it back. The token is
derived, not stored per unit, so nothing has to be kept in step; and it is
bound to the run, the attempt and the candidate head, so a token from one run
is refused in another.

**Why the seed is secret.** A challenge derived only from public facts — the
head SHA, the unit digest — can be computed by anyone who knows those facts,
including the reviewed branch. The seed is the one input that cannot be
derived, so it is the only reason the echo means anything.

**What this does NOT prove.** That the model actually read the diff. A model
handed the token will echo it whether or not it did the work. The challenge
proves freshness and binding — this verdict was produced for this run, for this
unit — and `honest_scope` says exactly that rather than implying more.
"""

from __future__ import annotations

import hashlib
import hmac

from .errors import refuse

CHALLENGE_VERSION = "trusted-challenge-v1"

#: 16 bytes of hex. Long enough that guessing is not a strategy, short enough
#: that a model echoing it in prose does not truncate it.
TOKEN_HEX = 32
SEED_BYTES = 32


def mint_seed(*, seed=None) -> bytes:
    """A run's secret seed. Injectable so the whole scheme is testable.

    A test passing its own seed proves nothing about that run's secrecy, and it
    is not supposed to: what a test checks is that DIFFERENT seeds produce
    different tokens and that a token from one binding is refused under
    another. The secrecy comes from `secrets.token_bytes` in production, and
    from the seed never being written into evidence."""
    if seed is not None:
        if not isinstance(seed, (bytes, bytearray)) or len(seed) < SEED_BYTES:
            refuse(f"category=challenge_seed_too_short minimum={SEED_BYTES} — "
                   "the length is reported, the seed never is")
        return bytes(seed)
    import secrets

    return secrets.token_bytes(SEED_BYTES)


def token_for_unit(*, seed: bytes, unit_sha256: str, candidate_head_sha: str,
                   trusted_run_id, trusted_run_attempt) -> str:
    """One token per unit, bound to the run that asked for it.

    Every binding field is in the MAC input under a length-free but
    NUL-separated encoding; the fields are fixed-shape (two hex digests and two
    integers) so no field can be extended to look like the start of the next.
    """
    from .identity import assert_commit_sha

    if not isinstance(unit_sha256, str) or len(unit_sha256) != 64:
        refuse("category=challenge_unit_sha_malformed")
    assert_commit_sha(candidate_head_sha, field="candidate_head_sha")
    for label, value in (("trusted_run_id", trusted_run_id),
                         ("trusted_run_attempt", trusted_run_attempt)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            refuse(f"category=challenge_run_field_invalid field={label}")
    if not isinstance(seed, (bytes, bytearray)) or len(seed) < SEED_BYTES:
        refuse(f"category=challenge_seed_too_short minimum={SEED_BYTES}")
    message = "\x00".join([CHALLENGE_VERSION, unit_sha256, candidate_head_sha,
                           str(trusted_run_id), str(trusted_run_attempt)])
    return hmac.new(bytes(seed), message.encode(),
                    hashlib.sha256).hexdigest()[:TOKEN_HEX]


def instruction_line(token: str) -> str:
    """The single line added to the model's instructions.

    Deliberately explicit about what the token is for. A model that is told
    "echo this string" without being told why will sometimes decide the
    instruction is noise and drop it, and a dropped token is indistinguishable
    from a forged verdict at the point where it is checked."""
    if not isinstance(token, str) or len(token) != TOKEN_HEX:
        refuse("category=challenge_token_malformed")
    return (f"Include the exact string {token} in proof_of_check. It "
            "identifies this specific review request; a response without it is "
            "discarded unread.")


def assert_echoed(verdict: dict, *, expected_token: str) -> dict:
    """The verdict must carry this run's token for this unit.

    Substring rather than equality: `proof_of_check` is meant to hold the
    model's account of what it checked, and requiring the field to be nothing
    but the token would throw that away. The token is 32 hex characters, so a
    substring match is not something a plausible sentence produces by
    accident."""
    if not isinstance(expected_token, str) or len(expected_token) != TOKEN_HEX:
        refuse("category=challenge_token_malformed")
    if not isinstance(verdict, dict):
        refuse("category=challenged_verdict_not_object")
    proof = verdict.get("proof_of_check")
    if not isinstance(proof, str) or not proof:
        refuse("category=challenged_verdict_has_no_proof_of_check")
    if expected_token not in proof:
        # The observed proof is NOT reported: it is provider-controlled text,
        # and this is the path where a reader is least likely to be careful.
        refuse(f"category=challenge_not_echoed unit={verdict.get('unit_sha256', '')[:12]} "
               "— the verdict does not carry this run's token, so it was "
               "produced for some other request or produced without one")
    return {"challenge_echoed": True,
            "unit_sha256": verdict.get("unit_sha256"),
            "honest_scope": ("the response was produced for this run, this "
                             "attempt and this unit. It does NOT show that the "
                             "model read the diff — a model handed the token "
                             "echoes it either way")}


def assert_all_echoed(verdicts, *, seed: bytes, candidate_head_sha: str,
                      trusted_run_id, trusted_run_attempt) -> dict:
    """Every verdict, each against its OWN unit's token.

    Checking them all against one token would let a single genuine response
    vouch for a batch, which is the shape of the bug this is here to prevent."""
    checked = []
    for index, verdict in enumerate(verdicts):
        unit = verdict.get("unit_sha256") if isinstance(verdict, dict) else None
        if not isinstance(unit, str) or len(unit) != 64:
            refuse(f"category=challenged_verdict_unit_missing index={index}")
        expected = token_for_unit(
            seed=seed, unit_sha256=unit, candidate_head_sha=candidate_head_sha,
            trusted_run_id=trusted_run_id,
            trusted_run_attempt=trusted_run_attempt)
        assert_echoed(verdict, expected_token=expected)
        checked.append(unit)
    if not checked:
        refuse("category=no_verdicts_to_challenge — a challenge check over "
               "nothing passes for every run")
    if len(set(checked)) != len(checked):
        refuse("category=challenged_verdict_unit_duplicated")
    return {"verdicts_checked": len(checked), "all_echoed": True}
