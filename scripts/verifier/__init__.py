"""Deterministic review planning for the cross-vendor verifier.

Split out of scripts/independent_verify.py because that file had grown past
the legacy 90,000-character per-part review budget — the very defect this
package exists to fix (P0-02: an oversized control-bearing file is omitted
from review rather than split, which permanently blocks the panel). A fix for
review-size deadlock must not itself create one.

Layering, strictly one direction:

    canon -> errors -> contentpolicy -> gitdiff -> rawchange
                                     -> repostate, codeowners
    gitdiff -> classification
    canon   -> atoms -> splitters -> units
    rawchange + contentpolicy -> identity
    coverage                            (pure proof, canon only)
    everything above -> plan            (Stage 1)
    capabilities -> provider -> cost -> executor -> evidence   (Cycle 3+)

`plan` (Stage 1) is STRICTLY ZERO-NETWORK — no socket, no HTTP client, no
key read anywhere in this package; enforced by AST test. Only the future
`provider` module may open a socket.
"""
