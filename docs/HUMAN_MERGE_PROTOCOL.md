# Human merge protocol — mid-term panel

**Status:** the merge decision in this repository is made by a person. No
workflow merges anything, and no tool in this repository holds a token that
could.

This document is the procedure for that person. `scripts/human_merge_gate.py`
mechanises the parts of it that are tedious to check and catastrophic to get
wrong; it is a checklist, not a substitute for reading the change.

---

## 1. What the two green checks actually mean

| status | means |
| --- | --- |
| `midterm-panel-count` | the approved engine derived the review units from the candidate's commits, and counted every governed model and batch against the provider's token endpoint |
| `midterm-panel-review` | those exact counted requests were executed, three governed models returned validated verdicts, and no unit was refuted |

They do **not** mean:

- that the change is correct — three models approved a diff, which is evidence,
  not proof;
- that a third party attested to anything;
- that the review was write-separated. It was not. The provider key is a
  repository-level secret and the workflow that holds it is the workflow this
  repository maintains.

Every record this lane writes says so in its own honesty fields. If you ever
see a status describing itself as trusted, write-separated, or independently
attested, **stop**: the merge gate refuses those strings, and a status carrying
one has been edited.

## 2. The exact-head rule

A green check is a statement about a **commit**, not about a pull request.

Between reading the panel's verdict and pressing merge, the author can push.
GitHub will merge the new head under the old review's afterglow — the checks tab
shows the newest run, you remember the one you read, and nothing in between
tells you they are different commits.

So every merge in this repository uses:

```
gh pr merge <N> --match-head-commit <sha> --squash
```

`--match-head-commit` makes the server refuse if the head moved. It is not
optional and the gate will not print a command without it.

## 3. Never

- **`--admin`.** It steps over branch protection. If protection is what is
  blocking the merge, satisfy it or change it deliberately — not in the same
  breath as reviewing.
- **`--auto`.** It merges later, when checks pass, on a head nobody has looked
  at. That is precisely the merge this protocol exists to prevent.

The gate refuses to print or validate a command containing either.

## 4. The procedure

1. **Read the change.** The panel is a second opinion, not the first one.
2. **Read the panel's evidence.** The run summary carries the count evidence
   digest, the engine provenance, and the per-model decision. Note the head sha
   it ran on.
3. **Run the gate**, passing what you actually read and retained:

   ```
   GITHUB_TOKEN=<a read token> \
     python scripts/human_merge_gate.py \
       --pr <N> \
       --reviewed-head <sha> \
       --expected-base <base sha the panel counted against> \
       --panel-run-id <Actions run id of the privileged panel run> \
       --count-evidence-sha256 <digest of the count evidence you kept> \
       --panel-evidence-sha256 <digest of the panel evidence you kept> \
       [--human-approval <path>]
   ```

   It checks, in order: the pull request is open and not a draft; the head has
   not moved since you read it; the base has not moved and `main` is still
   where the panel counted against; the merge is clean; **ordinary CI** —
   `test (3.12)`, `image` and `midterm-panel-selftest` — is green in its latest
   attempt on that exact commit; both panel statuses are `success` in their
   latest state; no status overclaims; and then the part that makes the rest
   mean something.

   **Why the run id and the digests.** A commit status is not
   self-authenticating. In a one-repository architecture any workflow holding
   `statuses: write` can post `midterm-panel-count = success`, and the creator
   still shows as `github-actions[bot]`. So the gate resolves the run you name
   and requires it to be the deployed `midterm-panel-review.yml`, triggered by
   `workflow_run`, **from the default branch** — the three facts that make its
   definition and checkout trusted — with both credential-bearing jobs
   successful; it requires each green status to point at that run; and it
   requires the statuses to name the evidence digests you actually hold.

   `--human-approval` is required when the pull request touches
   `.github/workflows/`, `.github/actions/`, `scripts/trustedlane/` or
   `scripts/midtermpanel/`. The record must assert
   `workflow_security_review_completed`, name **this** head, and carry a
   reviewer and a timestamp.

4. **Run the command it prints.** Yourself. The gate does not merge — that
   keeps the decision and the credential with you.

5. If the gate refuses, **do not work around it.** Each refusal names the check
   and the sha. A moved head means going back to step 1 on the new head.

## 5. When the panel is red

`midterm-panel-review` failing means at least one governed model refuted at
least one unit, or the engine's anti-canned gate found the panel's voices
indistinguishable. The private verdict evidence in the run's artifact says
which, per model, per unit, with each model's own reason and proof-of-check.

Read it before deciding anything. A refutation is a finding; the red check is
the process exit, not the finding.

## 6. What no one may do

Merge on a green check without reading the change. That is the failure this
whole architecture is arranged around: the reviewer holds a provider credential,
so the reviewer is not trusted to be the last word. You are.
