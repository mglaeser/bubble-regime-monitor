# Independent Cross-Vendor PR Review Panel

**Ported 2026-07-25 from `RoseLohr/roses-food-blog`** (`scripts/regime/independent-verify.mjs`
+ `.github/workflows/independent-verify.yml` + `CODEOWNERS`) — **same mechanism,
same roles**, translated to Python to match this repository (stdlib-only,
pytest-covered, ruff-scanned; the type check reads `app/` only).

## Why

This repository is written and maintained with Anthropic models. The reference
repo's constitution (Article IV — Independence) states the principle: *the
generator never grades its own work* — every change is attacked by a verifier
fleet from a **different vendor** with falsifying intent. Here that closes the
gap the CI audit tracked as B-35 and A-39: until the panel, merges relied on
the deterministic gate plus the operator alone.

## Mechanism

On every pull request, from the default branch's definition
(`pull_request_target`; workflow `Independent-Verify`, job `panel`): the
candidate is fetched as inert git objects, never checked out or run, and the
verdict is published as the commit status `independent-verify` on the
candidate's head.

1. **Panel, one DIFFERENT model per voice** (default
   `gpt-5.3-codex, gpt-5.6-sol, gpt-4.1-mini`, resolved against the account's
   `/v1/models`; exact ID or dated snapshot only — never a `-mini`/`-preview`
   variant; a voice the account does not serve is not replaced: the run
   refuses and lists the ids that are enabled).
   Model diversity beats mere lens diversity; each voice additionally gets a
   distinct lens (security/privacy · correctness/fail-closed · gates-stop-firing
   /blast-radius **+ this repo's own invariant: undeclared changes to scored
   values, the frozen artifact, its SHA pin, or the golden fixture**).
2. Each voice receives the full `--stat` file overview plus a code excerpt of
   the diff and must answer strict JSON:
   `{refuted, confidence, reason, defects, proof}`.
3. **Roles:**
   - **Required approver "Sol"** (`gpt-5.6-sol`, configurable via
     `VERIFIER_REQUIRED_APPROVER`): must be resolved in the panel and must
     **explicitly approve**. A refutation of ANY confidence — even "low" — is a
     **veto**. A missing, errored, or unparsable Sol vote
     blocks. Sol's green must be *proven*: its own substantive reason and its
     own valid challenge echo (it cannot free-ride on the panel's attestation).
   - **Independent corroborators:** ≥ `VERIFIER_MIN_OTHER_APPROVERS` (default
     1) **distinct** non-Sol models must also approve; repeat votes of one
     model count once.
4. **Integrity gates (all fail-closed):**
   - `decide()` per vote: no boolean `refuted` → block; refutation with
     high/medium confidence → block;
   - strict mode (`VERIFIER_STRICT_ANY_REFUTATION`): a high- or
     medium-confidence refutation by ANY voice blocks, not only the required
     approver's. It is on unless the variable is explicitly `0`, `false`, `no`
     or `off`, and the run log names which way it resolved;
   - `attest_consistency()`: an approving vote must not itself report a
     defect. The verdict carries a `defects` ledger, a JSON array of strings,
     empty when the voice found nothing. A ledger that names a defect blocks,
     and so does approving prose that contains one of the defect words the
     prompt lists, negated or not. `VERIFIER_REQUIRE_DEFECT_LIST` — off by
     default in the script, set to `true` by the hosted workflow — makes the
     ledger mandatory: a missing one, or one that is not an array, blocks as
     well. One such vote blocks the run; the other approvals do not carry it;
   - `attest_reasons()`: a majority of green votes must carry substantive,
     mutually distinct reasons (anti **canned green**);
   - `attest_proof()`: a majority of green votes must echo the per-run random
     challenge as `<challenge>-<tier 1-9999>` (anti **hardcoded green** — a
     future "return green" shortcut carries no run-valid echo). Honest scope:
     this does not cryptographically prove an LLM round-trip; a malicious
     endpoint could mirror the challenge. That is the documented cross-vendor
     trust assumption, compensated by the deterministic CI gate remaining the
     sole merge authority.
5. **No-key mode is explicit:** the standalone script can print the documented
   **residual** and exit 0 without `SECOND_VENDOR_API_KEY` / `OPENAI_API_KEY`.
   The hosted required workflow defaults `VERIFIER_REQUIRE_KEY` to true, so a
   missing secret fails closed instead of publishing a green status with zero
   votes. Only an exact operator-set repo variable value of `false` selects the
   visibly inactive residual mode for an ordinary same-repo pull request; forks
   and Dependabot remain fail-closed. With `VERIFIER_REQUIRE_KEY=false` and no
   key the panel is dormant and the status is **failure**
   (`panel dormant — no review performed; re-enable the verifier to merge`):
   dormancy pauses merging, it does not approve. An upstream workflow failure
   publishes failure as well.
6. **Privacy:** only code leaves the origin — images (raster and SVG), fonts,
   binaries, PDFs, GeoJSON, DB/RDS/XLSX files and `data/**` are excluded from
   both the overview and the body (one git pathspec per extension; `:(glob)`
   does not brace-expand).
7. **Robustness:** per voice up to 3 attempts on transient errors (network,
   408/409/429/5xx, and the observed flaky 401), never on deterministic 400/403/
   404. Each attempt starts on the Responses API (streamed), the primary wire;
   a network error, a 5xx or a gateway without that route (404/405) earns that
   attempt one try on chat completions.

## Separation of powers

`.github/CODEOWNERS` assigns the gate (`.github/`, `scripts/`) and the
frozen-methodology governance surfaces (`frozen_methodology.json`,
`app/methodology.py`, the byte-guard test, the governance docs) to the
operator role — the identity writing gated code must not silently modify the
gates that judge it.

## Activation and explicit dormant mode

1. Create a key for the chosen OpenAI-compatible verifier endpoint and store it
   as the `trusted-verifier` **environment secret**
   `TRUSTED_VERIFIER_API_KEY`. The workflow maps that one secret to the
   verifier process's `SECOND_VENDOR_API_KEY`; its endpoint preflight receives
   only a Boolean presence signal derived from the same secret, never the key.
   The key must be an 8–4096-character visible-ASCII value; shorter,
   whitespace/control, or Unicode forms are refused before diff or network I/O.
   The lower bound keeps exact-echo leak detection from treating ordinary text
   as a credential match when an operator supplies a trivially short key. For
   the structured verdict, the five required top-level JSON field names are
   public protocol syntax rather than peer-authored values: they are exempted
   from the echo comparison only in that exact root-key position. Every value,
   nested key, and unexpected root key remains covered, including JSON-escaped
   echoes after decoding.
2. Store the endpoint as the `trusted-verifier` **environment secret**
   `VERIFIER_BASE_URL` — NOT as a variable. It is required even for direct
   OpenAI use (`https://api.openai.com/v1` must be explicit); there is no host
   fallback because the key belongs to the operator-selected endpoint. The
   transport accepts HTTPS `/v1` endpoints on fully qualified ordinary ASCII
   DNS hostnames only (no IP literal, single-label, IDN/punycode, trailing-dot,
   or zone-qualified host), and refuses redirects and ambient proxies so the
   credential cannot be forwarded to another host or exposed through a
   resolver's alternate spelling.
   The runner prints every env block into the job log, and this log is public:
   it redacts secrets there and never variables, so a variable publishes the
   endpoint on every run.
3. Optionally set repo **variables**: `VERIFIER_PANEL_MODELS`,
   `VERIFIER_REQUIRED_APPROVER`, `VERIFIER_MIN_OTHER_APPROVERS`,
   `VERIFIER_AUTH_HEADER`, and `VERIFIER_STRICT_ANY_REFUTATION` (Mechanism,
   item 4). The single pin `VERIFIER_MODEL` and its voice count
   `VERIFIER_PANEL` are settings of the standalone script only: the workflow
   passes neither. Set `VERIFIER_REQUIRE_KEY=false` only to deliberately make
   ordinary same-repo runs dormant: while they are, every pull request's status
   is failure and nothing merges (Mechanism, item 5). An absent, blank, or
   other value remains fail-closed, and fork or Dependabot runs cannot opt out.
   The auth header defaults to `Authorization`; a custom value must be an
   `X-*-Key` header so the credential cannot replace routing, proxy, cookie, or
   content headers.
4. Branch protection, as the repository's settings read on 2026-11-15: one
   active ruleset on `main`, with no bypass actors, requires the status checks
   `test (3.12)`, `image` and `independent-verify` on an up-to-date branch. It
   requires no approving review, and Code Owner review is not enabled, by
   decision: GitHub accepts no self-approval, so in a single-maintainer
   repository it would deadlock every pull request. `.github/CODEOWNERS` names
   the same checks and gives that reason under its enforcement status; its
   list of paths is the inventory of the surfaces that must not move without
   the owner, not an approval rule.

## The panel's first live act: vetoing its own PR

On PR #21 — the PR that introduced it — the live panel's required approver
refuted with two findings about the panel's own implementation. Both are
addressed:

1. **Privacy excludes were case-sensitive** (uppercase `.PNG`/`.SVG`/`.PDF`
   would have reached the vendor): fixed with git `icase` pathspec magic;
   pinned by test. The reference repo carries the same latent gap.
2. **Non-Sol high/medium refutations do not block once Sol + a corroborator
   approve.** That is the reference's *documented* semantics (its selftest
   asserts it). A **strict mode** (`VERIFIER_STRICT_ANY_REFUTATION`) blocks on
   ANY high/medium refutation; it was opt-in at first and has since become the
   default (Mechanism, item 4).
3. **(Round 3) Incomplete data denylist** (`customers.csv`/`backup.sql` would
   have reached the vendor): data-file classes added (`csv/tsv/sql/jsonl/
   parquet/sqlite/dump/bak/pickle/npy…`). `.json` stays reviewable ON PURPOSE —
   the frozen methodology artifact is JSON and must be visible to the panel.
   Residual, stated honestly: a denylist is inherently incomplete; novel data
   extensions or extensionless dumps would still be sent. **(Round 3) Silent
   truncation**: the diff now carries a COMPLETE `--name-status` file list and
   every cap (file list 100k / diffstat 8k / body 50k then, 200k today) is
   explicitly marked; the prompt instructs reviewers not to approve blind past
   a marked cut. **(Round 3) Responses-API fallback** was made to trigger on
   the documented 400 rejection as well as 404 (a 400-only model would have
   blocked permanently); the Responses API has since become the primary wire
   (Mechanism, item 7).
4. **(Round 4) Excluded-only auto-green:** the privacy excludes were applied
   to the authoritative file list too, so a PR touching only excluded-content
   files produced an empty diff and greened with ZERO votes. Fixed: the
   `--name-status` list now carries ALL changed paths unfiltered (paths are
   not content); the excludes keep protecting contents (stat/body only).
   **(Round 4) Wrong diff base:** `origin/main` was hard-coded; PRs targeting
   another branch were mis-based and could green unreviewed. Fixed via
   `GITHUB_BASE_REF` (empty-string-safe) with `main` fallback.
5. **(Round 5) Dependabot bypass:** Dependabot PRs are same-repo (so the fork
   test passes) yet run with Actions secrets withheld — no-key exit 0 would
   green a required check with zero votes. `VERIFIER_REQUIRE_KEY` now also
   triggers for `dependabot*` actors; mirror the vendor key into the
   **Dependabot secrets store** if those PRs should be reviewed rather than
   blocked.
6. **(Rounds 6-7) Failure-as-emptiness:** `_sh` converted git errors and
   invalid-UTF-8 decodes into empty output (empty diff → green with zero
   votes), and a failed merge-base silently fell back to `HEAD~1`, reviewing
   only a multi-commit PR's tip. Fixed: bytes decoding with visible
   replacement characters, `DiffError` fail-closed on any required diff
   command including the merge-base — no fallback narrows the review window.
7. **(Round 8) Dissent must not attest:** the attestation pool admitted
   low-confidence refutations as "green-carrying", so a dissenter's
   reason/proof could satisfy the integrity majorities while an approving
   corroborator rode through canned. `_green` now includes explicit approvals
   only — a low-confidence refutation neither blocks nor attests. (Same
   latent conflation exists in the reference's `attestReasons` filter.)
8. **(Round 2) Fork-PR bypass:** GitHub withholds secrets from fork-originated
   `pull_request` runs, so the no-key exit-0 would have let a fork PR pass a
   *required* `cross-vendor` check with zero review. Fixed: the workflow sets
   `VERIFIER_REQUIRE_KEY=true` by default and always for fork-origin and
   Dependabot runs, and the panel then **fails closed** without a key. Only an
   explicit operator-set false enables the documented residual behavior for an
   ordinary same-repo run. The reference repo carries the same latent bypass.

## Verification

- `python scripts/independent_verify.py --selftest` exercises every pure gate
  function (ported 1:1 from the reference's selftest, including its panel-found
  hardening cases: Sol-variant impersonation, NaN fail-open, canned-green
  majorities, whitespace-padded reasons, challenge-tier bounds, single-pin
  no-corroboration).
- `tests/test_independent_verify.py` pins the same semantics in pytest and
  proves the standalone script's no-key mode is green-but-loud. Its class
  `TestTheDocumentDescribesThisPanel` holds this document to the workflow and
  the script: the trigger, the job, the status, the dormant status, and the
  settings.
