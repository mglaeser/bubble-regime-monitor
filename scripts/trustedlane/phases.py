"""The lane's phase order, and the gate that keeps this branch inside D0."""

from __future__ import annotations

from .errors import refuse

D0 = "D0_NO_SECRET_BOOTSTRAP"
D1 = "D1_TRUSTED_COUNT"
D2 = "D2_TRUSTED_GENERATION"

PHASE_ORDER = (D0, D1, D2)

#: What each phase is permitted to do. Data rather than prose, so a runner can
#: assert its own limits instead of relying on someone having read a comment.
PHASE_CAPABILITIES = {
    D0: {
        "reads_credential": False,
        "calls_provider": False,
        "emits_trusted_evidence": False,
        "may_execute_candidate_code": False,
        "requires_protected_deployment": False,
    },
    D1: {
        "reads_credential": True,
        "calls_provider": True,
        "calls_generation": False,
        "emits_trusted_evidence": True,
        "may_execute_candidate_code": False,
        "requires_protected_deployment": True,
    },
    D2: {
        "reads_credential": True,
        "calls_provider": True,
        "calls_generation": True,
        "emits_trusted_evidence": True,
        "may_execute_candidate_code": False,
        "requires_protected_deployment": True,
    },
}

#: The phase this code is. Raising it is a deployment decision, not an edit.
IMPLEMENTED_PHASE = D0


def assert_phase_permitted(phase: str) -> dict:
    """Refuse any phase past the one this deployment actually is.

    D1 and D2 need a protected deployment and an operator-approved engine.
    Neither is a property of source code, so neither can be satisfied by
    changing source code — which is exactly why this refuses rather than
    checking a flag a branch could set."""
    if phase not in PHASE_CAPABILITIES:
        refuse(f"category=unknown_phase phase={phase!r}")
    if phase != IMPLEMENTED_PHASE:
        refuse(
            f"category=phase_not_deployed requested={phase} "
            f"implemented={IMPLEMENTED_PHASE} — {phase} requires a protected "
            "deployment, an operator-approved digest-pinned engine, and an "
            "environment-only credential; none of those is something this "
            "branch can grant itself")
    return dict(PHASE_CAPABILITIES[phase])
