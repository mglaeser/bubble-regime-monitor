"""Deterministic review planning for the cross-vendor verifier.

Split out of scripts/independent_verify.py because that file had grown past
the legacy 90,000-character per-part review budget — the very defect this
package exists to fix (P0-02: an oversized control-bearing file is omitted
from review rather than split, which permanently blocks the panel). A fix for
review-size deadlock must not itself create one.

Layering, strictly one direction:

    gitdiff  -> atoms -> splitters -> coverage -> plan
    classification, generated              (leaf helpers)
    capabilities -> provider -> cost -> executor -> evidence

`plan` (Stage 1) is STRICTLY ZERO-NETWORK. Only `provider` may open a socket.
"""
