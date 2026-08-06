"""When a prior panel result may be reused, and when it may not.

## Why "same head" is not the question

CI reruns. A rerun on an unchanged head must not buy a second three-model panel,
so some reuse rule is needed. The obvious rule — "same candidate SHA, skip" — is
wrong in a way that is invisible until it matters:

    the head is what was reviewed. It is not what the review was.

Between two runs on the same head, the engine artifact can be rebuilt, the panel
policy can change which models vote or what blocks, and the request semantics can
change what the models are actually shown. Each of those produces a materially
different review of identical code, and a head-only rule would report the old
answer for the new question.

So the binding is five things, and reuse requires ALL of them to match:

    candidate head          what code
    candidate base          against what
    engine digest           which analyser produced the units
    policy digest           which panel, which rules
    request semantics       what the models were actually asked

and the prior result must already be terminal. A `pending` prior is not a result;
reusing one would publish a conclusion nobody reached.
"""

from __future__ import annotations

from .errors import refuse
from .evidence import digest_of

#: The five fields whose conjunction identifies a review.
BINDING_FIELDS = ("candidate_head_sha", "candidate_base_sha", "engine_digest",
                  "policy_digest", "request_semantics_digest")

TERMINAL_STATES = ("success", "failure", "error")


def binding(*, candidate_head_sha: str, candidate_base_sha: str,
            engine_digest: str, policy_digest: str,
            request_semantics_digest: str) -> dict:
    """The identity of a review, as data.

    Keyword-only, because five same-shaped hex strings positionally is a swap
    waiting to happen and a swapped binding still hashes to something."""
    values = {
        "candidate_head_sha": candidate_head_sha,
        "candidate_base_sha": candidate_base_sha,
        "engine_digest": engine_digest,
        "policy_digest": policy_digest,
        "request_semantics_digest": request_semantics_digest,
    }
    missing = sorted(k for k, v in values.items() if not v)
    if missing:
        refuse(f"category=dedupe_binding_incomplete fields={missing} — a "
               "binding with an empty component would match every other "
               "binding with the same gap")
    return {**values, "binding_digest": digest_of(values)}


def may_reuse(*, prior: dict, current: dict) -> dict:
    """Refuse-by-default. Reuse only on a complete match of a terminal prior.

    Returns a decision rather than raising on mismatch: a mismatch is the NORMAL
    case — it means "do the review" — and normal control flow should not be an
    exception. Refusals here are reserved for malformed input, which is a
    different thing from a legitimate cache miss."""
    for name, record in (("prior", prior), ("current", current)):
        if not isinstance(record, dict):
            refuse(f"category=dedupe_binding_not_a_mapping which={name}")

    differing = [f for f in BINDING_FIELDS if prior.get(f) != current.get(f)]
    prior_state = prior.get("state")

    if differing:
        return {"reuse": False, "reason": "binding_changed",
                "differing_fields": differing,
                "explanation": ("the head may be identical; the review is not — "
                                f"{differing} changed since the prior result")}
    if prior_state not in TERMINAL_STATES:
        return {"reuse": False, "reason": "prior_not_terminal",
                "prior_state": prior_state,
                "explanation": ("a pending prior is not a result; reusing it "
                                "would publish a conclusion nobody reached")}
    return {"reuse": True, "reason": "identical_binding_and_terminal_prior",
            "prior_state": prior_state,
            "binding_digest": current.get("binding_digest")}


def assert_reuse_is_not_head_only(rule_fields) -> dict:
    """A guard against this module being simplified into the bug it prevents.

    The tempting refactor is "just compare the SHA" — smaller, faster, and
    silently wrong the first time an engine is rebuilt. Asserted rather than
    commented, because a comment does not fail."""
    fields = set(rule_fields or ())
    missing = sorted(set(BINDING_FIELDS) - fields)
    if missing:
        refuse(f"category=dedupe_rule_too_narrow missing={missing} — reuse "
               "keyed on fewer than the five binding fields reports a previous "
               "answer to a different question")
    return {"binding_fields": sorted(fields)}
