# Permanent Governance Freeze Rule

**Operator ruling of record: 2026-07-23 (PR #17 merge note).**
**Status:** permanent maintenance rule. The governance milestone is CLOSED with
this rule; future work primarily improves methodology, not governance.

## What the freeze architecture guarantees (as merged in PR #17)

A canonical causal methodology artifact (`frozen_methodology.json`); runtime
loading (Python + R); byte-level SHA-256 protection; mutation protection;
completeness protection with the anti-recurrence collision ledger
(`tests/test_frozen_methodology.py::KNOWN_COLLISIONS`); shadow infrastructure
for future methodology work; explicit runtime provenance; a documented PIN
process; deterministic governance around methodology evolution.

---

## The two remaining classes of governance debt

### Class 1 — duplicate source (artifact key exists, runtime hardcodes the same value)

Same value, same runtime behavior, same score, same Golden, same Monte Carlo —
only the **causal ownership** changes when fixed. Classification:

> **PATCH / FREEZE COMPLETION** — not v4, not a methodology change.

These should all eventually disappear (see the maintenance policy below).

### Class 2 — artifact gap (no artifact key exists; the runtime constant is still the canonical source)

Closing a gap adds new artifact keys: the frozen **specification** grows while
every value stays identical. Still not a methodology change; it is a
**governance artifact evolution**. Classification:

> **FREEZE RE-PIN** — new artifact keys; new artifact SHA-256; no existing
> scored value changes; unchanged methodology version; unchanged service
> version; unchanged Golden. This is exactly the evolution the freeze
> architecture was designed to allow.

*(Reconciliation note, flagged for the operator: adding keys necessarily
changes the scored-tree byte hash even though no value changes. Rule 3 below is
therefore read as applying to Class-1 cleanups; a Class-2 FREEZE RE-PIN instead
requires "no existing key's value changes" plus the Golden/MC/version
invariants, with the artifact SHA re-pinned deliberately.)*

---

## The Freeze Rule (permanent)

A future governance cleanup is permitted **only if ALL of the following hold**:

| # | Rule |
|---|---|
| 1 | The change removes an existing duplicate. |
| 2 | No numeric value changes. |
| 3 | The scored-tree hash is byte-identical. |
| 4 | The deterministic Golden is bit-identical. |
| 5 | Seeded Monte Carlo outputs are bit-identical. |
| 6 | The methodology version is unchanged. |
| 7 | The service version is unchanged. |

If all seven hold:

```
classification = GOVERNANCE_REPIN
```

Otherwise the change **immediately exits governance** and enters the normal
methodology (v4) process.

## Maintenance policy for the duplicate ledger

The committed ledger (`KNOWN_COLLISIONS`) is **technical debt, not a work
queue**. No immediate batch cleanup. Whenever one of the ledgered files is
modified in the future for a legitimate reason, remove that file's duplicates
then — under the seven rules above — and update the ledger in the same reviewed
change. The ledger's scan guarantees no NEW duplicate can enter silently, which
keeps governance work proportional.

## Verification recipe for any GOVERNANCE_REPIN

1. `scored-tree sha256` = hash of the artifact minus `_meta`, sorted-key JSON —
   must equal the value recorded in the ledger test's re-pin history.
2. Deterministic Golden printed at full float precision (bit comparison, not
   `approx`).
3. Full suite (covers the seeded-MC goldens) + ruff.
4. `EXPECTED_SHA256` re-pinned with a history entry documenting what changed
   and why it is metadata-/wiring-only.
