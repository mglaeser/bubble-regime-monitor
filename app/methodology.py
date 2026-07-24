"""Canonical frozen-methodology artifact loader (F-01 / L-07).

`frozen_methodology.json` is the SINGLE runtime source of every score-effective
constant. The Python calculation paths and (via the GSADF bridge) the R path load
their constants from here, so a change to the artifact is the ONLY way to change a
frozen constant — enforced by:

  * a byte-level SHA-256 guard (tests/test_frozen_methodology.py) that fails CI on
    any edit and directs maintainers to the v4 process;
  * a completeness test that every score-effective runtime constant equals the
    artifact value (no hardcoded duplicate may remain);
  * a mutation test proving a changed value changes engine output.

`<PIN>` is rejected anywhere it would feed a calculation (get_path / the
score-effective tree). It is permitted ONLY inside the `_meta` block, which holds
governance bookkeeping (methodology_frozen_at / falsification_tracking_since) that
was never recorded in the codebase and only the operator can supply.

The artifact is byte-faithful to the v3.7.8 runtime literals; the seeded-MC golden
headline is 52.43 and is unchanged by wiring the engine to load from it.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

FROZEN_PATH = Path(__file__).resolve().parents[1] / "frozen_methodology.json"

PIN_SENTINEL = "<PIN>"


@lru_cache
def frozen_bytes() -> bytes:
    """The canonical file's exact bytes (what the SHA-256 guard hashes)."""
    return FROZEN_PATH.read_bytes()


@lru_cache
def frozen_sha256() -> str:
    return hashlib.sha256(frozen_bytes()).hexdigest()


def _reject_pins(node: Any, path: str) -> None:
    """Raise if a <PIN> sentinel appears anywhere OUTSIDE the _meta block."""
    if isinstance(node, str) and node == PIN_SENTINEL:
        raise ValueError(f"frozen_methodology: unresolved {PIN_SENTINEL} at {path} "
                         "— an operator PIN must be resolved before runtime load")
    if isinstance(node, dict):
        for k, v in node.items():
            _reject_pins(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _reject_pins(v, f"{path}[{i}]")


def _validate_frozen(data: dict[str, Any]) -> None:
    required = {"aggregation", "override", "action_bands", "red_flags", "coverage",
                "monte_carlo", "freshness_sla_days", "quality", "indicators", "gsadf"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"frozen_methodology: missing required sections {sorted(missing)}")
    # <PIN> is allowed ONLY in _meta (governance bookkeeping); never in a scored path.
    for key, node in data.items():
        if key == "_meta":
            continue
        _reject_pins(node, key)


def _deep_freeze(node: Any) -> Any:
    if isinstance(node, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in node.items()})
    if isinstance(node, list):
        return tuple(_deep_freeze(v) for v in node)
    return node


@lru_cache
def frozen_methodology() -> MappingProxyType[str, Any]:
    """Validated, deeply-immutable view of the canonical artifact."""
    data = json.loads(frozen_bytes())
    _validate_frozen(data)
    return _deep_freeze(data)


def get_path(*keys: str | int) -> Any:
    """Fetch a value by key path, e.g. get_path('aggregation','rescale_floor')."""
    value: Any = frozen_methodology()
    for key in keys:
        value = value[key]
    return value


def as_dict(*keys: str | int) -> dict[str, Any]:
    """A MUTABLE plain-dict copy of a mapping node (for module constants that must
    be ordinary dicts, e.g. weight vectors passed to numpy/dataclass code)."""
    node = get_path(*keys)
    return dict(node)


def as_tuple(*keys: str | int) -> tuple:
    """A tuple copy of a list node (e.g. an anchor range or beta pair)."""
    return tuple(get_path(*keys))
