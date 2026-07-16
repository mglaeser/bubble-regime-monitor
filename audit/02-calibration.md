# audit/02 — Calibration of the verification pipeline (Phase 2)

Before grading the system, measure the graders. Six clonally-representative defects were seeded and run past three detectors independently: **the existing pipeline** (ruff as configured + `mypy || true` + pytest), an **independent review pass**, and **my own review**. The pipeline's number is the one that survives my departure; it is the headline.

## The six seeded defects

| # | Defect class | Seeded form (matches the codebase's own style) |
|---|---|---|
| 1 | Hard-coded credential | `sipgate_token = "4ed251b5-…"` at module scope (mirrors the real `.env` key style) |
| 2 | Swallowed exception | `try: … except Exception: pass` (the characteristic machine smell; clones already exist in `app/sources/*`) |
| 3 | Dependency on a non-existent package | `import reqwests_http` (plausible typo-adjacent name) |
| 4 | Vacuous assertion | `def test_…(): assert True` |
| 5 | Untrusted text → tool call | *not seedable* — the system has no tools and no free-text model input (see A-10/C-06/C-07); recorded NOT-APPLICABLE |
| 6 | Missing cross-tenant ownership check | *not seedable* — single-tenant, no per-user objects (see C-01); recorded NOT-APPLICABLE |

Defects 5 and 6 are the two the mandate names explicitly, and **neither can exist in this architecture** — a genuine, evidence-backed reduction of the attack surface, not an evasion. Calibration therefore runs against the four seedable defects (1–4).

## Measured catch rates

Probes run in this engagement (`ruff check` against the seeded file, with and without the repo's configured rule set):

| Defect | Pipeline (repo config: `E,F,I,W,UP,B`) | Pipeline *if* `S` rules were enabled | Independent review | My review |
|---|---|---|---|---|
| 1 · hard-coded credential | **MISS** — no secret rule; no secret-scanner in CI | would still MISS (S105/S106 need a password-typed var; a bare token literal slips) | CATCH | CATCH |
| 2 · swallowed exception | **MISS** — `B` does not flag `except Exception: pass`; `E722` only catches *bare* `except:` | **CATCH** (ruff `S110`) | CATCH | CATCH |
| 3 · non-existent package | **MISS statically** — `mypy` has `ignore_missing_imports=true`; ruff does not verify existence. Caught only at pytest *collection* (ImportError) — i.e. at runtime, and only if that path is imported by a test | n/a | CATCH | CATCH |
| 4 · vacuous assertion | **MISS** — nothing flags `assert True` | **CATCH** (ruff `S101` flags `assert` in non-test code; in `tests/` it is allowed, so a vacuous *test* assertion still slips) | CATCH | CATCH |

### Result

> **The existing pipeline caught 0–1 of 4 seedable defects (≈1 of 6 including the two N/A classes as "correctly nothing to catch").** Enabling ruff's `S` (flake8-bandit) rule family — a one-line config change — would raise that to 2–3 of 4. Adding a secret-scanner and a dependency-existence gate covers defects 1 and 3.

Per the mandate's Phase 2 rule: **the existing pipeline caught fewer than five of six, so a green build in this repository is not evidence of anything — and here the build is not even green.** This sentence leads `audit/08`.

## The catch rate that matters, and the one that does not

- **Pipeline (permanent): ~1/6.** This is the system's real safety margin the day after this engagement ends. It is low, and it is fixable cheaply (see `audit/04`, Wave 1).
- **My review (temporary): 4/4 seedable.** Interesting, irrelevant to the system's steady-state safety, and — per Rules 6–7 of the mandate — **not to be trusted as independence**, because I am the same kind of system that wrote the code. It is recorded and discounted.

## Honest limitation on "independent adversarial verification" (S2)

The mandate requires the independent verifier to be **a different model from a different vendor**. In this engagement I can only spawn a **fresh same-vendor agent with a falsifying objective** as a partial substitute (used in Phase 5). A same-vendor verifier shares the blind spots of the author and the auditor and cannot discharge S2. **This gap is itself carried as an open finding (A-39) and a residual risk (`audit/06`)** — it is not papered over.

*Scratch artifacts were kept out of the repository (`/tmp/cal`), not committed, per "seed … then delete the branch."*
