# PR #23 — integration simulation and remediation-stack topology

Contract §5.5–§5.7. Everything here was produced by a **disposable local
integration**, never pushed. PR #23's branch
(`claude/bubblegauge-build-spec-fzthju`) is untouched and still at
`a9062aa656a5a6f3dbe5991d16ce9c218aad0454`.

**No trusted claim is made anywhere in this document.** Every finding below is
`DETERMINISTIC_STATIC_ANALYSIS` — reproducible by running the quoted command. No
model has reviewed PR #23; D1 and D2 are not active.

## Simulation identities

| thing | value |
|---|---|
| base `main` | `112e29d34f4d094a3fa2747c6af2e66c10c0b255` |
| simulated precursor-main | `26818e9f13bd334ed57cc9262c199be2c6e97167` |
| simulated integration commit | `7802e16d7e7074e9ae87f18f92e0c0543c857273` |
| simulated integration **tree** | `774b0f87554ee32c988e39091a16ec563330d515` |
| PR #23 head (frozen, unmodified) | `a9062aa656a5a6f3dbe5991d16ce9c218aad0454` |

The precursor merged into `main` **cleanly, no conflicts**.

## Conflict register — exactly four files

Only PR #23 conflicts, and every conflict is with a file Exchange 2 changed:

| file | why it conflicts | resolution taken in the simulation |
|---|---|---|
| `.github/workflows/independent-verify.yml` | PR #23 predates the V-TRUST fix | **take `main`** — see F-01 |
| `.github/workflows/ci.yml` | PR #23 predates the SHA action pins | **take `main`** |
| `.gitignore` | independent edits | take PR #23 |
| `pyproject.toml` | independent edits | take PR #23 |

Two of the four are security-bearing. A resolution that takes "theirs" on either
one reverts an Exchange-2 fix.

## Changed-file universe — 41 files

`git diff --name-status 26818e9f 7802e16d` → **22 added, 1 deleted, 18 modified**

| area | count |
|---|---|
| `audit/` | 17 |
| `tests/` | 8 |
| `governance/` | 5 |
| `scripts/` | 2 |
| `app/` | 2 |
| root/config (`pyproject.toml`, `.gitignore`, `.secrets.baseline`, `CLAUDE.md`, `AGENTS.md`, `.github/CODEOWNERS`) | 6 |
| `.github/workflows/` | 1 |

## Deterministic findings

### F-01 — PR #23 reintroduces V-TRUST · **P0**

`git show a9062aa:.github/workflows/independent-verify.yml` lines 35–36:

```yaml
SECOND_VENDOR_API_KEY: ${{ secrets.SECOND_VENDOR_API_KEY }}
OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

in a job that is `on: pull_request`, checks out the PR ref, and runs
`python scripts/independent_verify.py` from that checkout. That is the exact
defect Exchange 2 closed. The job is also still named `cross-vendor`, the
inactive-but-authoritative-sounding name EX2-F02 renamed.

**A naive "accept theirs" conflict resolution reintroduces it.**

Guarded: feeding that blob to the merged ratchet refuses —

```
category=pr_controlled_workflow_reaches_a_secret name=independent-verify.yml
count=2 classes=['PROVIDER_CLASS'] triggers=['pull_request','workflow_dispatch']
```

so CI would go red rather than shipping it. The guard is the reason this is
recoverable, not the reason it is unimportant.

### F-02 — PR #23 unpins both actions · **P1**

`actions/checkout@v4` and `actions/setup-python@v5`. Same conflict, same
resolution: take `main`, which carries the approved SHA pins.

### F-03 — `.secrets.baseline` more than doubles · **P1, needs review not rejection**

| branch | files | entries |
|---|---|---|
| `main` | 2 | 5 |
| PR #23 | 3 | 11 |

A baseline is an allowlist. Six added entries are six things the secret scanner
will stop reporting. Each needs to be looked at individually before merge; this
analysis does not claim any of them is wrong, only that a doubling is not a
detail.

### F-04 — commits do not map to stack areas · **structural, drives the decision below**

45 commits. Sampling the twelve most recent, each touches **5–8 top-level
areas**:

```
2d1049e -> .secrets.baseline,audit,governance,scripts,tests
dc6beec -> .github,.secrets.baseline,audit,governance,scripts,tests
6b90277 -> .github,.secrets.baseline,AGENTS.md,app,audit,governance,scripts,tests
```

They are **review rounds**, not feature units — "Close iteration-3 fail-opens",
"Work the branch-wide review", "Fix 7 confirmed items, rebut 5". Therefore
**cherry-picking by commit is not viable**. A stack has to be reconstructed
by *file*.

### F-05 — the integration tree is otherwise clean · nonblocking

On the resolved integration: `ruff check app tests scripts` passes; the
live-workflow policy, status-name policy, lane workflow validators and the
deployed-D0 byte-identity check all pass. An earlier run of the full suite on
the equivalent tree gave **1733 passed, 1 xfailed**.

## Remediation-stack topology

Dependencies are real: `scripts/mandate_gate.py` hard-references **13**
`governance/` paths, so the gate cannot land before the files it attests. `app/`
imports nothing from the gate, so it is independent.

| # | stack item | source files | depends on | merge order | exercised by | cherry-pick? |
|---|---|---|---|---|---|---|
| 1 | **Governance source & manifest** | `governance/mandate.md`, `governance/mandate/part1.md`, `governance/mandate/manifest.json`, `governance/constitution.md`, `governance/accepted-residuals.json` | — | 1st | none yet (inert data until item 2) | **yes** — additive, no imports |
| 2 | **Mandate gate & verification machinery** | `scripts/mandate_gate.py`, `tests/mandate_gate_support.py`, `tests/test_mandate_gate.py`, `tests/test_mandate_gate_calibration.py`, `tests/test_mandate_gate_registers.py` | 1 (13 hard path references) | 2nd | `test (3.12)` | **yes**, but only after 1 |
| 3 | **Findings / catalogue / transitions** | `audit/03-findings.json`, `audit/03-findings.md`, `audit/00-check-catalogue.json`, `audit/03b-coverage-ledger.md`, `audit/ratchet-baselines.json` | 2 (the gate validates consistency) | 3rd | `test (3.12)` | **yes** |
| 4 | **Secret / history controls** | `.secrets.baseline`, `app/config.py` (allowlist pragma on the placeholder default) | — | any, but **review F-03 first** | secret-scan step | **yes**, one file at a time |
| 5 | **Audit-surface derivation** | `audit/00-audit-surface.json`, `audit/engagement-status.json`, `audit/05-08…`, `audit/09`–`audit/13`, `audit/evidence/…` | 2, 3 | 4th | `test (3.12)` | **yes** — but 17 files; split by generated-vs-authored |
| 6 | **Supply-chain / CI controls** | `.github/workflows/independent-verify.yml`, `.github/workflows/ci.yml`, `.github/CODEOWNERS`, `pyproject.toml` | — | **DROP the two workflow files** | `test (3.12)`, live-workflow policy | **NO — needs redesign** |
| 7 | **Deployment / production-eligibility semantics** | `audit/engagement-status.json` (`production_eligible`), `audit/08-standing-regime.md`, `audit/06-residual-risk-register.md`, `CLAUDE.md`, `AGENTS.md` | 1, 2, 3, 5 | last | `test (3.12)` | **yes** |
| — | **`app/engine/judgment.py`** | explicit-kwargs change (no `**` expansion, so the no-tool invariant stays source-checkable) | — | independent | `test (3.12)` | **yes** — smallest, safest, lands first |

**Item 6 is the one that needs redesign, not transplant.** Its two workflow
files are precisely the Exchange-2 fixes; PR #23's versions are strictly worse
(V-TRUST live, actions unpinned, inactive job misnamed). The correct move is to
drop them from the stack entirely and keep `main`'s, taking only `CODEOWNERS`
and `pyproject.toml` from PR #23.

## One PR or a stack?

**A stack — six or seven PRs.** The reasoning that actually drives it:

1. **Blast radius is not uniform.** Item 6 contains a live credential-exposure
   regression; items 1 and 3 are inert data. Reviewing them under one approval
   means the riskiest change is approved by whoever got tired around file 30.
2. **Independent revertibility.** If the gate (item 2) turns out to fail closed
   on something legitimate, reverting it should not also revert the governance
   text it attests. As one PR, it cannot.
3. **F-04 forces reconstruction anyway.** Commits span 5–8 areas each, so
   nobody is going to cherry-pick this by commit whatever they decide. Once the
   stack is being rebuilt by file, building it as several PRs costs almost
   nothing extra.
4. **The trusted review is metered.** D1 counts input tokens and D2 spends on
   generation. A 41-file, ~12k-line single review is one enormous batch whose
   cost is hard to predict and whose failure is total. Six smaller reviews fail
   independently and can be re-run individually.

The counter-argument, stated fairly: the 45 commits contain interdependent
review-round fixes, and splitting risks landing a partial fix. That is real, and
it is why item ordering above is dependency-driven rather than convenience-driven
— nothing lands before what it references.

## What this is not

No model reviewed anything. No count and no generation has occurred. The
findings above are static analysis a reader can reproduce with the quoted
commands, and the stack is a proposal for Exchange 4 to implement, not an
approved plan.
