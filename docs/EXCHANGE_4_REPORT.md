# Exchange 4 — result report

**Branch under review:** `fix/instants-status-and-required-contexts` (PR #33)
**Precursor:** PR #29, `fix/verifier-intra-file-review-plan`
**Frozen:** PR #23 at `a9062aa656a5a6f3dbe5991d16ce9c218aad0454` — untouched this exchange.

---

## 1. What was asked, and what happened

Exchange 3's result was reclassified as `SESSION_BOUNDARY_CHECKPOINT_1` rather
than `EXTERNAL_BLOCK`, consuming Recovery Slot 1. The stated reason was
specific and correct: D1/D2 were `exit 1` placeholders, there was no trusted
runtime, no normalizer, no signer, and the engine candidate was neither a
protected-build artifact nor bound to its claimed source commit.

Ten load-bearing fixes were named. This exchange closed **eight of the ten**
in code, on the branch, with tests and mutation sweeps. Two are
operator-blocked and are reported as such below rather than as done.

---

## 2. Fix status

| id | what it required | state |
|---|---|---|
| EX3-R01 | replace D1/D2 placeholder exits with complete, fake-server-tested runtime | **CLOSED** |
| EX3-R02 | trusted status published on the candidate head SHA | **CLOSED** (Exchange 3 late) |
| EX3-R03 | only requirable status classes may be required | **CLOSED** (Exchange 3 late) |
| EX3-R04 | engine identity over BOTH source roles | **CLOSED** |
| EX3-R05 | engine source bound to the exact source commit | **CLOSED** |
| EX3-R06 | hash-locked runtime, no-secret build artifact interface | **CLOSED** |
| EX3-R07 | UTC-instant expiry comparison | **CLOSED** (Exchange 3 late) |
| EX3-R08 | required contexts observed, not assumed | **CLOSED** (Exchange 3 late) |
| EX3-R09 | evidence signing/verification + real normalizer | **CLOSED** |
| EX3-R10 | rebase PR #29, rerun CI, update body, keep draft | **CLOSED** |
| EX3-R11 | V-TRUST reported as separate machine facts | **CLOSED** (Exchange 3 late) |

Operator-blocked and **not** claimed as done: activation of D1, activation of
D2, branch/ruleset protection, the sixteen operator records, the status
publisher's installation token, the precursor merge, and PR #23's rebase and
trusted review. None of these is a code change and none can be performed from
a reviewed branch.

---

## 3. The correction that mattered most

The Exchange-3 verdict said the D1/D2 templates were "NOT DEPLOYABLE AS
WRITTEN". They said, under a step named *Verify and load the approved engine
artifact*:

```yaml
run: |
  echo "engine verification is implemented in the D1 activation PR"
  exit 1
```

A step that has never verified anything, and an `echo` that is a comment with
an exit code. That is now:

- `artifactload.extract` — verify the artifact against an operator-approved
  digest, then unpack it into a directory that must be empty, refusing every
  unsafe member **by name** rather than filtering it silently;
- `d1cli.main()` / `d2cli.main()` — assemble every argument from the runner
  and run the lane.

Neither template contains an executable `exit 1`. A test parses the YAML and
checks the `run:` blocks rather than grepping the file, because the header
quotes the old placeholder so a reader can see what changed — and grepping the
whole file asks a question adjacent to the one that matters.

---

## 4. The lanes

`d1runtime.run` and `d2runtime.run` are the complete ordered sequences. The
order is the design, not presentation: each step can only refuse things the
steps before it established.

| # | step | why here |
|---|---|---|
| 1 | protected state | every later answer is worthless if a credential is reachable from an unprotected ref |
| 2 | engine artifact by digest | is the code about to hold the key the code that was approved |
| 3 | operator records | did a human authorize this, recently |
| 4 | occurrence-scoped literal | which exact number, for which exact commit |
| 5 | challenge seed | minted before anything is sent |
| 6 | inert fetch | candidate arrives as data, `--no-checkout` |
| 7 | trusted rebuild | the candidate's plan is compared against, never acted on |
| 8 | status `pending` | before any provider work |
| 9 | preflight + count | global budget, checked before each request |
| 10 | ledger close | every planned unit, nothing extra, zero generation |
| 11 | signed evidence | `TRUSTED_COUNT_EVIDENCE` |
| 12 | terminal status | success only after the signature exists |

D2 is a separate module, not `run(generate=True)`. It differs in a separate
approval, its own environment, a plan it did not choose, and verdicts to check.
A flag would put those four differences inside a branch nobody reads.

### Why this is testable without a credential

Every capability is a parameter: `opener`, `credential`, `signing_key`,
`publisher`, `fetch`. Both lanes run end to end against a fake server in D0,
including every refusal, with no credential in the process.

The gate is on **obtaining** the capability, not on calling the function that
uses it. `transport.read_credential`, `transport.open_https` and
`signing.read_signing_key` each compare against `phases.IMPLEMENTED_PHASE`,
which is a deployment fact. Obtaining the capability is what a candidate branch
cannot do; calling a function is not.

---

## 5. The suite-wide invariant that had to change, and how

The lane's rule was "no module in `scripts/trustedlane/` imports a network
client". The count lane cannot count without sending a request, so the rule had
to change. It was **narrowed, not dropped**, and the narrowing is itself
checked:

- exactly one file is exempt and the test names it;
- the import is inside a function body, so importing `transport` loads no HTTP
  client — checked by execution, via the module's own namespace, because
  checking `sys.modules` would prove nothing (pytest pulls `ssl` in anyway);
- the function refuses before it imports, and at D0 the refusal always fires;
- neither credential-bearing module may assign `IMPLEMENTED_PHASE` or reach it
  via `setattr`/`globals`/`vars` — checked by AST, because grepping for the
  string finds the docstring.

The same treatment applies to the credential-env rule, exempting `transport.py`
and `signing.py` only.

---

## 6. Signing — what it is and what it is not

HMAC-SHA256 over the envelope digest, with domain separation. **Symmetric.** It
proves the signer held the trusted key and can be checked only by someone else
holding it. It is not a public-key signature and a third party cannot verify it
independently.

That is a trade, stated rather than glossed. Ed25519 would need a cryptography
package inside the credential-bearing runtime, and EX3-R06 argues at length
that the runtime installs exactly one dependency because every extra package is
more code next to a provider key. `honest_scope` says so in every record, and a
test asserts the wording is present — an overclaim in the field a reader most
wants to trust is worse than the limitation.

`evidencewire.verify` also stopped promising a **PUBLIC** key. That was a claim
about a design that does not exist.

---

## 7. Defects found by reading code against its own claims

Three, all real, all fixed:

1. **`parse_operator_record` discarded `exact_values`.** It validated a record
   and then dropped the literal the record exists to convey, forcing every
   consumer back to the unvalidated original — the copy an attacker edits.

2. **`scope` was carried and compared to nothing.** Every operator record had a
   `repo@commit` scope field that no code read. An approval to spend on
   reviewing one commit silently authorized spending on another, and a push is
   exactly how the reviewed thing changes. `countledger.authorize` now compares
   it.

3. **`d1runtime` listed "inert fetch" as step 6 and never fetched.** The
   property was left to whatever `skeleton_rebuild` happened to do. A step that
   only exists in a comment is not a step. The fetch is now real, injected, and
   checked: `checked_out` must be `False`, because a working tree is candidate
   code on the machine holding the credential.

A fourth, from `d1cli`: the candidate's declared plan was read from **inside
the engine artifact**. That gets the provenance backwards — the engine is the
trusted side — and would have made the rebuild comparison compare the engine
against itself.

---

## 8. Mutation sweeps

Two sweeps this exchange, both run with `PYTHONDONTWRITEBYTECODE=1` and a
`__pycache__` purge between mutations.

| sweep | mutations | caught first pass | after fixes |
|---|---|---|---|
| R04/R05/R06 engine identity | 28 | 19 | **28/28** |
| R01/R09 runtime | 58 | 55 | **58/58** |

The survivors were real test gaps, not sweep noise. The instructive ones:

- **`transport.exchange_generation` had no test.** Only the assertion beside it
  did, so the function that actually joins the credential and sends was
  unguarded.
- **The ledger digest bound the total and the unit names, not the per-unit
  attribution** — which is the claim the evidence makes. The old test varied a
  count, which also varied the total, so a digest ignoring attribution passed.
- **A proxy defect in my own test.** `engine_role_unknown` is emitted by both
  `multi_source_manifest` and `role_digest`, so asserting the category alone
  passed with the manifest-level check deleted. It now matches the plural
  `roles=` marker only the manifest emits.
- **A docstring claim that was false.** The evidence class in the MAC input was
  described as what stops a class swap. It is not — `envelope_digest` already
  covers the class. The term stays, because the redundancy is one edit to
  `ENVELOPE_FIELDS` deep, but the comment now says so.

---

## 9. Evidence classes — unchanged, and that is the point

| produced | class |
|---|---|
| 836 lane tests, 1281 total on the fix branch | `MOCK_TEST_EVIDENCE` / `UNTRUSTED_LOCAL_EVIDENCE` |
| 1826 tests on the merged precursor | `UNTRUSTED_LOCAL_EVIDENCE` |
| hosted CI on PR #33 and PR #29 | ordinary CI, not review |
| D0 containment run on `main` | `HOSTED_D0_CONTAINMENT_EVIDENCE` |
| any trusted count or generation | **none exists** |

No count and no generation has occurred anywhere in this programme. Writing the
runtime did not change that and is not evidence that it works against a real
provider — only that it refuses correctly against a fake one.

---

## 10. V-TRUST, as separate machine facts

| fact | value |
|---|---|
| `pr_controlled_provider_credential_exposure` | `CLOSED` |
| `trusted_review_authority` | `INACTIVE_OPEN_BLOCKING` |
| `precursor_merge_trust_gate` | `OPEN` |

The first is closed because `independent-verify.yml` no longer injects either
provider key and `livepolicy.py` is a merged ratchet that refuses
reintroduction. The second is inactive because activation requires two acts
neither of which is an edit. Reporting these as one state would conflate a
defect that is fixed with an authority that does not exist.

---

## 11. Activation is two acts, deliberately

1. Rename `d1-trusted-count.yml.template` → `.yml`. GitHub reads only `.yml`
   and `.yaml` under `.github/workflows/`, so the template is inert by
   extension.
2. Raise `phases.IMPLEMENTED_PHASE` in a commit on the protected branch.

Doing one without the other produces a workflow that refuses. Neither is an
ordinary edit, and the second is impossible from a candidate branch by
construction rather than by policy.

---

## 12. What remains, and who can do it

**Operator only.** Incident actions 1–4; branch and ruleset protection;
environment protection; the status-publisher installation token; the sixteen
operator records; D1 activation; D2 activation.

**After the operator, and only then.** Trusted review of PR #29; precursor
merge; PR #23 rebase onto the merged precursor; PR #23 full trusted review; the
remediation-stack decision.

**Available now without an operator.** Trusted review of PR #33 by an external
reviewer, since the branch is complete and its CI is green. That review would
be `UNVERIFIED_EXTERNAL_CLAIM` under this programme's own vocabulary until the
trusted lane is active — which is precisely the circularity the lane exists to
break, and precisely why the operator steps cannot be worked around.

---

## 13. Honest scope of this report

Every claim above about test counts, mutation results and CI conclusions was
produced by, or observed from, this session. The test counts and mutation
sweeps are `UNTRUSTED_LOCAL_EVIDENCE`: they were produced by the branch being
reviewed. The CI conclusions are hosted facts about a server. Neither is
trusted review, and this document does not claim otherwise.
