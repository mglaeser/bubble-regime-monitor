"""Indicator sub-score computations.

Each module implements one indicator from the fixed three-leg hybrid
framework. Module docstrings quote the canonical Methodology registry in
app.references (the single source of truth); the compute functions are pure
(raw inputs -> sub-score in [0, 1]) so they are unit-testable without
network access.
"""
