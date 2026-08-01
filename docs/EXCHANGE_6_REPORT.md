# Exchange 6 — terminal report

**Classification: `EXTERNAL_BLOCK`.**

Every item of the Exchange-6 terminal objective that a model can perform without
operator credentials is complete and merged or pushed. What remains needs acts
this branch cannot perform: deleting a workflow run, rotating a key, protecting
a ref, minting an authenticated operator envelope, and approving spend.

One defect found in the last hour is **open and reproduced**, not closed. It is
stated in §4 rather than buried.

---

## 1. Merged to main

`409cc5d` — PR #33. CI green on all five checks before merge.

| finding | what changed |
|---|---|
| **EX5-R21** | `runtimebinding.assert_authorizations_match_runtime` compares every authenticated payload against the fact this run established for itself, before any capability is obtained. Ten prerequisites bound. Every mismatch costs zero provider attempts. |
| **EX5-R22** | An exact EMPTY literal-authorization set is representable: `expected_occurrence_count: 0`, empty members, exact empty-set digest. Distinct from missing prerequisite, missing scanner policy, file-wide and wildcard. |
| **R04/R13** | D1 signs the executable plan itself — a 31-field private document — and D2 verifies both signatures before reading either. `trusted_plan_sha256` alone is gone. |
| **R02/R09/R12** | D2 runs through `verifier.executor`. The lane's own request builder, endpoint gate, plan digest, verdict check, panel loop and finalizer are **deleted**. |
| **R19** | The provider-output privacy scan reads the key the engine emits, and a scan that scanned zero fields is refused. |
| **R06** | `inputmaterial` produces every path the lanes read, with `assert_producer_consumer_graph` checking each has a producer strictly earlier in the same job. |
| **R10** | `trusted-engine-build.yml` — protected, credential-free, deterministic, rebuilds and compares before retaining. |
| **R07/R20** | `statustransport` is a real publisher. Only obtaining the token is phase-gated. |

### Defects this branch found in itself

Each has a test that reddens without the fix.

1. **Both CLIs called `run()` without `engine_identity` or `bootstrap`** — required parameters. `d1cli.main` and `d2cli.main` would have raised `TypeError` on a real runner. Invisible because every test calls the runtime directly.
2. **Nothing compared the approved engine identity to the artifact the run verified.** The operator's five digests were checked against the identity record; the artifact was checked against its own expected digest; the two artifact digests were never required to be the same number.
3. **The output privacy scan read a key the engine has never emitted.** `result.get("evidence_records", [])` returned an empty list, scanned nothing, and reported success. Every test asserting "the output was scanned" passed.
4. **`executableplan.validate` checked unknown fields before forbidden ones**, so a plan carrying a credential reported `unknown_fields` and the capability refusal was unreachable.
5. **`governed_policy_digests` and `GenerationLedger` called the engine outside `engine_refusals`** — a PIN out of range would have escaped as a crash: no failure status, pull request pending forever.
6. **An engine `BlockingError` escaped D1's `except LaneRefusal`**, leaving the pending status on the PR permanently.
7. **Both signed D1 documents carried the same evidence class**, so the only thing separating the plan from the count evidence was the argument slot D2 received them in — chosen by the caller.
8. **The candidate-plan path pointed inside a `--no-checkout` clone**, which by construction has no working tree, so it could never have existed.
9. **`TRUSTED_ENGINE_SOURCE_SHA256`** was a second copy of a digest the identity record already carries.

### Test-quality repairs

Three checks were asking a proxy for the question that mattered, and one of them
found defect 5 the moment it stopped.

- The engine-seam test compared `where=` labels against a hand-written set, so it had to be edited whenever an engine call was added. It now derives the call set from the AST and asserts none is unwrapped.
- `test_f02_..._nine_distinct_registered_names` asserted `check_names == 9`. Same shape.
- The build workflow's containment test asked `"secrets." not in text` and failed on the file's own header explaining that it names no secret. It now walks the parsed YAML values.

---

## 2. PR #29 — rebased, draft, **not clean**

`5f23381`. Main merged in. It stays draft, correctly: no trusted evidence exists.

Merging main put `scripts/verifier/` and the trusted-lane suite on one branch for
the first time. In a single pytest process, `tests/test_verifier_plan.py` imports
the package — which is what it is for — and after that every lane test that loads
an engine refuses, because `enginepolicy.assert_no_candidate_import` compares
each loaded `verifier` module's file against the artifact root and finds the
checkout. **170 failures, one cause.**

The refusal is correct and was not relaxed: in production, a `verifier`
importable from the candidate checkout on the machine holding the provider key
is precisely what that check exists to catch. An autouse fixture restores the
process isolation production has by construction.

**This is not fully fixed.** See §4.

---

## 3. PR #23 remediation stack — four branches, each green

Reconstructed from `a9062aa` **by file**. Topology F-04: the 45 commits each span
five to eight stack areas, so nothing there is cherry-pickable as a commit.

| branch | contents | tests |
|---|---|---|
| `remediation/pr23-00-judgment-kwargs` | explicit keywords where the model is called | 975 |
| `remediation/pr23-01-governance-source` | `governance/`, inert | 975 |
| `remediation/pr23-02-mandate-gate` | the gate + everything its calibration reads | 1213 |
| `remediation/pr23-03-audit-record` | audit record, CODEOWNERS, standing regime, independent-verify | 1301 |

Unmerged, and PR #23's branch was not touched.

**The seven-item topology was wrong in two specific, reproducible ways**, and
both corrections are recorded in the package that found them:

1. **The gate cannot be separated from its calibration inputs.** With only items 1 and 2 present, 15 of the gate's 238 tests fail, each naming the file it is missing. A gate that cannot demonstrate it still catches its seeded defect is a gate nobody can review — `MANDATE-GATE FAIL: seeded-defect calibration (S12)` is what it says when it cannot.
2. **CODEOWNERS cannot be separated from what derives control-bearing status from it.** `independent_verify.is_control_bearing` derives rather than lists, so without CODEOWNERS an unreviewed change to the standing law would not have blocked.

**Both of PR #23's workflow files are dropped** (topology F-01, P0): they
reintroduce V-TRUST, unpin both actions, and carry the inactive job under its old
name. Only `CODEOWNERS` and `pyproject.toml` are taken from that item.

---

## 4. OPEN — reproduced, not fixed

**PR #29's full-suite run has 245 failures that the targeted runs do not.**

Reproduction:

```
git checkout fix/verifier-intra-file-review-plan
pytest tests/                     # 245 failed, 2211 passed
pytest tests/test_trusted_lane_bootstrap.py tests/test_verifier_plan.py \
       tests/test_verifier_trusted_lane.py tests/test_verifier_repostate.py
                                  # 1219 passed
```

What is known: the deterministic 170-failure cause is fixed (both file orders
pass). The residual is order- or collection-dependent and involves the same
`sys.modules` interaction — most likely `_ENGINE_CACHE` holding an engine whose
modules were purged, so a later `assert_no_candidate_import` reads a `sys.modules`
that no longer describes the loaded engine. `pytest-randomly` is **not**
installed, so shuffling is not the cause and that hypothesis is excluded.

It is stated here as open because it is open. It does not affect main — the
suite is green there — and it does not affect the four remediation branches. It
blocks calling PR #29 rebased-and-clean, which is why this report does not.

---

## 5. What is left, and who can do it

Every remaining item needs a credential or a console this branch must not have.

| # | action | why no model can do it |
|---|---|---|
| 1–16 | the operator prerequisites | fifteen are acts in a console; the sixteenth is an approval to spend |
| — | mint the authenticated envelopes | needs the MAC keys in the trust store, which the candidate never sees |
| — | set the seven environment secrets and two repository variables | environment scope, by design |
| — | run `trusted-engine-build`, create the release, approve the five digests | dispatch plus prerequisite 14 |
| — | run D1, obtain real `TRUSTED_COUNT_EVIDENCE` | needs the provider key |
| — | approve generation separately, run D2 | prerequisite 16, a distinct decision |
| — | review and merge the four remediation branches | needs the trusted review that needs all of the above |

`docs/TRUSTED_LANE_OPERATOR_ACTIONS.md` §Group 6 now lists the exact secret
names, variable names, and the D1 → D2 handoff by run id.

---

## 6. Machine state

| fact | value |
|---|---|
| `phases.IMPLEMENTED_PHASE` | `D0_NO_SECRET_BOOTSTRAP` |
| provider calls ever made by the lane | 0 |
| trusted evidence in existence | none |
| operator prerequisites satisfied | 0 of 16 |
| main | `409cc5d`, CI green |
| PR #29 | draft, rebased, **one open defect** (§4) |
| PR #23 | untouched, `a9062aa`; four reviewable branches beside it |

No model reviewed anything in this exchange. No count and no generation has
occurred. Every claim above is either a merged diff, a pushed branch, or a
command a reader can run.
