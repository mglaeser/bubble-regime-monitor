# audit/11 — Branch-wide adversarial review: verified disposition

**Review received:** 2026-07-26 (advisory, "implementation-ready backlog").
**Reviewed range in the review:** `9255261..6e0cfd9`.
**Verified against:** current HEAD (the round-33 fixes `b0dd689` were NOT in
the review's snapshot, so some claims were already fixed before this review
was written).

Every claim below was independently re-checked with quoted code/command
evidence (a 7-agent adversarial verification, defaulting to *refute*). The
review is high quality: of the 20 verified claims, 12 confirmed, 3 already
fixed, 5 overstated/refuted. This file is the standing disposition; the
in-code fixes it references are landed on this branch with red→green tests.

## Fixed on this branch (confirmed true, AI-safe, no invented constants)

| ID | Claim (verified) | Fix |
|---|---|---|
| **P0.1** | `_volume_complete` reported `part2_status: COMPLETE` while Part 2 source text is absent (manifest path/sha null) — it checked only finding verdicts, never text presence. | `_volume_complete` now also requires the volume's manifest part to have a non-null path, a tracked file, and a matching sha. `part2_status` truthfully flips to `IN_PROGRESS`; `part1_status` stays `COMPLETE` (its text is present + attested). |
| **P1.1** | Class-2 calibration ran `ruff --isolated`, proving the ruff *binary* knows S110, not that `pyproject.toml`/CI still select it. | `_ruff_selects_s110()` reads the real config and fails if S/S110 is unselected or ignored. |
| **P1.2** | The credential scanner's regex missed PEP 526 annotated assignments (`api_key: str = "…"`). | `scan_credential_shapes` is now AST-based (Assign/AnnAssign/walrus), fails closed on unparseable source, keeps the name-independent UUID/entropy value scan. Residual (low-signal value indistinguishable from a placeholder) recorded in `audit/10` item 1. |
| **P1.5-a** | `test_round6_diff_error_blocks_main` monkeypatched `build_diff`, which `main()` no longer calls (it calls `build_diff_chunks`) — the test passed for an unrelated reason. | Patches `build_diff_chunks`; asserts the DiffError branch; a companion test proves the guard is load-bearing. |
| **P1.5-c** | Several class-5/class-6/transition tests asserted a *copied regex* against a snippet, never running the shipped detector. | The detectors are extracted to importable module-level functions (`tool_enabling`, `declares_tenancy_column`, `declares_strong_tenancy`, `transition_matches`); `cmd_calibrate`/`cmd_ratchet` and the tests now share them. |
| **P2.5** | CI/AGENTS/audit prose said "43 tracked mypy errors"; the real count is ~116. | Removed the pinned number (per the review's own P3.4, "stop encoding mutable counts in prose"); the count is measured in CI, not pinned. |
| **P2.7** | The audit surface named `app/llm.py`, which does not exist (the real model call is `app/engine/judgment.py`); drift-check only proved JSON==generator. | Corrected the reference and added a check that every path-shaped inventory token resolves to a real file/dir (guarded on the gate script's presence so unit tests with synthetic roots are unaffected). |

## Operator decisions (confirmed true, but NOT AI-safe to land)

| ID | Claim (verified) | Why it is the operator's call |
|---|---|---|
| **P0.3** | `production_eligible` is computed but has no deploy consumer. | (a) A fail-closed deploy gate would brick *every* deploy — the field is pinned false and stays so (Track C out-of-repo). (b) Renaming the field is an Article XIII amendment of hash-attested governance text. Both need operator authorization. |
| **P1.6** | Same-repo no-key verifier runs return green with zero votes. | Becoming truly fail-closed needs either a provisioned `SECOND_VENDOR_API_KEY` (infra) or a merge-policy decision to hard-block same-repo no-key PRs; the code deliberately refuses a "fake block". Gated by branch protection either way. |
| **P1.7** | Cross-vendor independence is configuration/trust, not enforced (one base URL for all voices; name-based model identity). | Closing it requires provider identity / endpoint attestation infrastructure. In-repo name matching cannot prove provenance. (Note: the matching is stricter than "substring" — exact ID or dated snapshot, `-mini`/`-codex` rejected.) |
| **P2.1** | Floating lower-bound deps + a duplicate unpinned CI install list; `pip-audit` is time-dependent. | `pip install .[dev]` is a small edit, but true reproducibility is a dependency-lock **policy** decision (how strictly to freeze). |
| **P2.2** | Actions are tag-pinned (`@v4`/`@v5`), not SHA-pinned. | Needs the correct upstream commit SHAs (external facts) and a pin-and-bump policy. |

## Rebutted (wrong, overstated, or already fixed)

| ID | Verdict | Reasoning |
|---|---|---|
| **P0.2** | Overstated | The kernel is true — the `audit/10` prose backlog is outside the finding/status state machine. But "despite known critical/high defects" overstates: the same gate run reports `production_eligible=False`, STOP-SHIP open, 32 accepted blockers — it does not present the system as clean. The sweep's "critical/high" items are about the audit *machinery*, not the shipped product, and several overlap existing open blockers (A-02/A-08/A-36). Promoting them to findings requires operator **band assignment** (barred to an AI by the Freeze Rule). |
| **P1.3** | Overstated | Both dataflow escapes (config-only endpoint, cross-module construction) are *already disclosed* as a STATED LIMIT in the code comment and `audit/10` item 7. |
| **P1.5-b** | Already fixed | The "SystemExit trips an earlier guard" class was a historical defect; current fixtures reattest so the intended guard fires (verified by capture). |
| **P1.8** | Overstated | Over-budget *control-bearing* files are OMITTED (block), not truncated; only non-control prose/data is truncated, and truncation is marked with an instruction to the model not to approve blind. A real code defect is control-bearing → omitted → blocks. |
| **P1.9** | Refuted | Classification is total by construction: `path_risk` has a default tier, `is_code`/`is_control_bearing` return a bool for any string, every changed path is routed to a reviewed chunk or to `excluded_content`, and every control-bearing unreviewed path blocks. No fall-through, no example given. |

## Already fixed before this review (no action)

- **P0.1-part1** — Part 1 is genuinely COMPLETE (text present + attested).
- **P1.4** — tenancy scan already covers all ORM modules + migrations + raw SQL (rounds 25/29), predating the review's own snapshot.
- **P2.8** — non-literal route paths already fail closed with literal folding + `api_route` per-method expansion (round 33).

## Deferred to follow-up work (the review's own multi-PR plan)

The review's larger asks — deriving inventories instead of hard-coding
(P2.7 deep), dependency locking (P2.1), SHA-pinning actions (P2.2), the typed
LLM adapter (P2.12/P2.13), module decomposition (P3.1), schemas (P3.2),
property/mutation testing (P3.3), and the operational-admission gate (P0.3) —
are correctly scoped by the review itself as **separate sequential PRs**.
They are not folded into this branch, which is already large and blocked on
the mandate-text review budget.
