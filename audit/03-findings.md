# audit/03 — Findings (Phase 3)

One record per check, 119 total, worked in band order. Full schema in `audit/03-findings.json`. Verdicts: **PASS / FAIL / PARTIAL / NOT-APPLICABLE** (no SKIP; NO-EVIDENCE would band as FAIL — none here).

**Tally:** FAIL 15 · PARTIAL 65 · PASS 8 · NOT-APPLICABLE 31 — 80 actionable (FAIL+PARTIAL).

**STOP-SHIP reality:** `B-06` (secrets exposed → rotate) is the one intrinsic priority-10 FAIL. `A-01`+`A-39` **escalate to STOP-SHIP** (no independent verification of production code). `C-01` and `C-04` re-band off STOP-SHIP with written justification (single-tenant; no third-party EU personal data).


---

## STOP-SHIP

### B-06 · Secrets and machine identity — 🔴 FAIL (P10)

Every production API key/token was disclosed in the development chat channel; they are long-lived, static, un-vaulted, un-rotated. The git repo is clean but chat disclosure = publication. Admin key ships with a guessable default.

- **Evidence:**
    - Full history scan for 9 known live-secret fragments (ANTHROPIC/FRED/TIINGO/TWELVEDATA/ALPHAVANTAGE/ADMIN/SIPGATE/POLYGON/phone): 0 blob-hits across all refs; .env never committed; .gitignore:4 lists .env — repo is clean.
    - config.py:34 default ADMIN_API_KEY='change-me-to-a-long-random-string' — a shipped, guessable credential if unset.
    - No vault, no rotation, no short-lived workload identity (system map §identities).
- **Impact:** Any party with the chat transcript holds live Anthropic/Polygon/FRED/Tiingo/TwelveData/AlphaVantage keys, the sipgate SMS token (can send SMS as the operator), and the admin key (can trigger recompute/SMS). Spend, SMS abuse, data-source quota theft.
- **Fix:** ROTATE FIRST (revoke+reissue all 9 credentials at the providers), then: make ADMIN_API_KEY fail-closed (refuse the placeholder / empty value at startup); move to a host secret store; document rotation cadence in SECURITY.md.
- **Substitutions:** S6, S9
- **Residual:** Rotation is an out-of-band operator action this engagement cannot perform on the providers. · **Compensating:** Startup guard rejecting the placeholder admin key (added Phase 5); reads are public-only, so a leaked read path exposes no secret. · **Tripwire:** Add a startup assertion + a documented rotation date; alert if ADMIN_API_KEY equals the known placeholder.

### C-01 · Core application security — 🟠 PARTIAL (P10)

Core app security is sound (server-side constant-time auth, rate limiting, locked CORS, no IDOR surface because single-tenant) — but the admin key ships with a guessable default and one read endpoint 500s on malformed input.

- **Evidence:**
    - security.py:20 uses secrets.compare_digest (constant-time); require_admin_key 401s on mismatch; write endpoints (admin.py) all Depends(require_admin_key).
    - No per-user objects exist (single global snapshot) → no cross-tenant/IDOR surface; probe: there is no {id} object route to enumerate.
    - main.py:99 CORS locked to one origin, GET-only, allow_credentials=False.
    - WEAKNESS: config.py:34 placeholder admin default; score.py:111 datetime.fromisoformat on unvalidated query -> HTTP 500 (see A-25).
- **Impact:** Not a cross-tenant breach (architecturally impossible here). Residual: an operator who never sets ADMIN_API_KEY has an open admin surface; malformed ?from= yields a 500.
- **Fix:** Fail-closed admin default (B-06); validate date query params (A-25). Re-band from STOP-SHIP to PARTIAL: the STOP-SHIP trigger (broken access control / cross-tenant read) does not exist in a single-tenant app.
- **Substitutions:** S3

### C-04 · Privacy and data governance — ⚪ N/A (P10)

No EU personal data of third parties is processed; the single personal datum is the operator's own SMS recipient number, where data subject == controller. Re-banded from STOP-SHIP per §3.

- **N/A basis:** The applicable law is the GDPR (operator is in the EU/DE). The system stores/POSTs exactly one phone number (SIPGATE_RECIPIENT) and one SEC-etiquette contact email — both the operator's own, config-only, not collected from any third party. There is no user base, no data collection, no profiling, no automated decision about any person. GDPR obligations (lawful basis, DPIA, erasure workflow) are de minimis: the controller processes only his own contact detail to text himself. Materiality: negligible. The one hygiene item — the recipient number appearing in an INFO log — is carried under C-23.


---

## BLOCKER-1

### A-01 · Deterministic verification gate on every production change — 🔴 FAIL (P9) → **escalates to STOP-SHIP (A-01+A-39 both fail)**

The only production gate (CI) is red on every recent run and non-blocking; production shipped from a red commit; no branch protection; no independent verifier. STOP-SHIP by §3 escalation (A-01+A-39).

- **Evidence:**
    - audit/evidence/ci-runs.md: runs #34-#43 all conclusion=failure, on feature AND base branch; both jobs fail.
    - a916e8d was built+deployed to the host while its CI was red (user deploy log).
    - ci.yml:29 'mypy app || true' discards the type-check exit code; no required-status-check enforces green.
    - No policy-as-code bundle; the 'gate' is a single human who does not line-review AI output.
- **Impact:** Nothing — no human and no machine — independently verifies any line of production code. Every green build is meaningless; every 'PASS' elsewhere that rests on CI is void.
- **Fix:** Make CI green (install the full prod dep set incl lppls/anthropic OR skip cleanly when absent) and blocking (remove '|| true'; add security gates); require the check for merge (branch protection); add an independent adversarial verifier (S2). See Phase 5 for the CI-gate rebuild.
- **Substitutions:** S1, S2, S3

### A-02 · A test suite that can actually fail — 🔴 FAIL (P9)

No mutation testing exists, so the suite's ability to fail is unmeasured; the suite is non-hermetic (hard-imports optional deps and errors instead of skipping); CI runs it against a different, incomplete dependency set and is red.

- **Evidence:**
    - Repo-wide search: no mutmut/cosmic-ray/hypothesis config — mutation score is UNKNOWN.
    - Local run in .venv: 1 failed / 161 passed; the failure is ModuleNotFoundError: lppls (test hard-imports an optional heavy dep).
    - ci.yml install list omits lppls+anthropic -> CI's pytest necessarily errors on that path (audit/evidence/ci-runs.md).
- **Impact:** The test suite is the only backstop in a no-reviewer model, and its effectiveness is unmeasured and currently non-green. A green build (were it green) would still prove nothing without a mutation score.
- **Fix:** Wire mutation testing (mutmut) on core logic (engine/aggregate, montecarlo, indicators) as a tracked metric; make optional-dep tests skip cleanly; align CI deps to pyproject. Do NOT escalate to STOP-SHIP yet — mutation score is unmeasured, not proven-zero; carry as BLOCKER-1 with the measurement as the first repair.
- **Substitutions:** S3, S4

### A-06 · Version control, batch size, executed rollback — 🟠 PARTIAL (P9)

Commits are small and atomic (good); deploy.sh HAS an automated health-check + auto-rollback to the previous image (good) — but rollback is deploy-time only, not wired to a runtime abort signal, and is not rehearsed on a schedule; CI-on-push is red.

- **Evidence:**
    - git log: staged v3.3.0 commits are small and single-concern.
    - deploy.sh header step 5: health-check + auto-roll-back to previous image on failure.
    - No runtime SLO/error-budget trigger for rollback (A-24/B-19).
- **Impact:** A bad deploy self-recovers on health-check failure; a bad change that passes health-check but degrades output has no automatic runtime rollback.
- **Fix:** Rehearse rollback on a schedule and record duration; wire an output/error-rate tripwire (B-18/B-26) to the rollback. Credit the existing deploy-time auto-rollback.
- **Substitutions:** S8

### A-08 · Security scanning as the control — 🔴 FAIL (P9)

No SAST, no dependency scanning, no secret scanning, no DAST, no SBOM — none in CI, none blocking. The lint gate as configured (ruff E,F,I,W,UP,B) misses secrets, swallowed exceptions and vacuous asserts (calibration §02).

- **Evidence:**
    - Repo-wide search: no bandit/pip-audit/safety/trivy/semgrep/gitleaks/detect-secrets/cyclonedx.
    - Calibration §02: repo ruff config caught 1/4 seeded defects; enabling ruff 'S' + a secret scan + a dep-existence check covers 3-4.
- **Impact:** Machine-authored code ships untrusted with no scanner proving any security property; the load-bearing 'the scanners ARE the control' substitution is absent.
- **Fix:** Add to CI as blocking: ruff 'S' rules, pip-audit (dep CVEs), a secret scanner (gitleaks/detect-secrets over head+history), and an SBOM (cyclonedx). Calibrate each against a seeded instance.
- **Substitutions:** S1, S3

### A-24 · The system operates and recovers itself — 🟠 PARTIAL (P9)

Strong graceful degradation and a /readyz self-check, plus deploy-time auto-rollback — but no SLI/SLO/error budget, no automated runtime detection→containment→recovery, single instance; recovery ultimately assumes a human notices.

- **Evidence:**
    - main.py lifespan seeds off the boot path; sources drop-and-renormalize; /readyz runs R+lppls self-checks.
    - No SLO/error-budget/golden-signal instrumentation; no automated runtime rollback on quality breach.
- **Impact:** Known data-failure modes self-heal; unknown/quality failure modes wait for the operator.
- **Fix:** Define an availability SLO + error budget; add golden-signal metrics; wire an automated abort (B-18) to a health/quality signal. Materiality is bounded by single-user self-host.
- **Substitutions:** S8, S10

### B-01 · The pipeline actually gates — 🔴 FAIL (P9)

Delivery-side of A-01: the pipeline contains a soft-fail ('mypy || true'), is red on every recent run, and has no bypass-proof blocking behaviour; production shipped on red.

- **Evidence:**
    - ci.yml:29 '|| true' soft-fail.
    - audit/evidence/ci-runs.md: 10 consecutive red runs incl base branch.
- **Impact:** The pipeline is a pipeline-shaped object that does not block; with no reviewer downstream it is the entire safety system, and it is off.
- **Fix:** Remove soft-fails; make jobs green and blocking; re-run seeded-defect calibration to confirm it now catches them.
- **Substitutions:** S1

### B-03 · Observability that reaches root cause — 🟠 PARTIAL (P9)

structlog gives structured, queryable JSON logs (good) — but there are no metrics, no traces, no OpenTelemetry, no golden-signal dashboards, and MTTD/MTTR are not tracked.

- **Evidence:**
    - logging_conf.py configures structlog; sources/engine log structured events.
    - No metrics/traces/otel deps or config in the repo.
- **Impact:** A request can be followed in logs; there is no metric/trace correlation to root-cause a latency or quality regression.
- **Fix:** Add OpenTelemetry traces + the four golden-signal metrics on the recompute path and the API; keep it minimal (single service).

### B-04 · Dependencies that exist, are pinned, were vetted — 🟠 PARTIAL (P9)

Every dependency resolves to a real package (no hallucinated/slopsquat name; lppls==0.6.24 and R exuber confirmed) — but only lppls is exact-pinned, there is no lockfile/hash pinning, and CI installs an unpinned, hand-edited subset that diverges from pyproject.

- **Evidence:**
    - pyproject.toml: all deps use '>=' floors except lppls==0.6.24; no requirements.txt/uv.lock/poetry.lock.
    - ci.yml:23-25 installs a hand-listed subset omitting lppls+anthropic+xlrd.
- **Impact:** A transitive or floating dependency can change under the build; CI tests a different graph than production runs; no existence/existence-date gate defends against the slopsquat class the mandate calls the highest-yield attack on machine-built code.
- **Fix:** Generate a hash-pinned lockfile; install from it in CI and the Containerfile; add a pre-install package-existence/allowlist check + pip-audit as blocking gates.
- **Substitutions:** S1, S3

### B-11 · Rollback for code, prompts and models — 🟠 PARTIAL (P9)

Code rolls back via commit-tagged Podman images with deploy.sh auto-rollback; prompts roll back via git — but models are floating aliases with no pinned snapshot to roll back TO (B-13), and rollback is not signal-triggered.

- **Evidence:**
    - deploy.sh keeps KEEP_IMAGES commit-tagged images + auto-rollback.
    - config.py:15 model is an alias, not a dated snapshot -> nothing to pin/roll back to on a silent provider-side model change.
- **Impact:** A silent provider model update presents as a code regression with no pinned version to revert to.
- **Fix:** Pin the model to a dated snapshot (B-13); wire rollback to an abort signal (B-18).
- **Substitutions:** S5, S8

### B-20 · Runtime detection+containment of injection/exfiltration — ⚪ N/A (P9)

No path exists by which untrusted content reaches the model, and the model controls no outbound channel, so runtime injection/exfiltration detection has nothing to protect.

- **N/A basis:** Exfiltration via a model requires the model to (a) ingest attacker-controlled content and (b) control an outbound channel (URL fetch, markdown/image render, email, PR). Here the model receives only numbers/enums from the operator's own pipeline (judgment.py:139) and emits a <=300-char text with NO tools and NO ability to trigger any fetch/send. The SMS/JSON carrying its output are deterministic, operator-directed sinks. There is no data-exfiltration primitive to detect. (If a tool/connector is ever added, this check re-activates immediately — see C-08.)

### B-22 · Agents deployed with least privilege, enforced — ⚪ N/A (P9)

No agents exist, so per-agent least-privilege enforcement is inapplicable; the analogous container-privilege item is a real finding tracked under B-12.

- **N/A basis:** The model performs zero tool calls (repo-wide search: no tool_use/function_call/MCP). There is no agent identity, no connector allowlist to enforce. The nearest applicable control — process/container least privilege — is audited under B-12 (container currently runs as root: a real finding).

### C-03 · Supply-chain security and hallucinated packages — 🟠 PARTIAL (P9)

No package-existence gate, no lockfile, no composition analysis — but a full audit of every import resolved to a real package (no hallucinated/newly-registered/typo-adjacent dependency).

- **Evidence:**
    - Manual resolution of every pyproject dep + R exuber: all real; lppls==0.6.24 exists on PyPI; no slopsquat candidate.
    - No SCA/existence gate in CI (same root as B-04/A-08).
- **Impact:** The specific machine-built-code attack (slopsquatting a hallucinated import) is not currently realised, but nothing prevents a future floating install from pulling one.
- **Fix:** Same remediation as B-04: hash-pinned lockfile + pre-install existence/allowlist gate + SCA.
- **Substitutions:** S1

### C-05 · LLM application risk taxonomy, mapped and tested — 🟠 PARTIAL (P9)

The OWASP LLM top-10 coverage matrix did not exist; it is produced in this engagement (audit/matrices/llm-top10.md). Most categories are N/A by architecture; improper-output-handling and misinformation apply and are addressed by disclaimers but lack an executable groundedness gate.

- **Evidence:**
    - audit/matrices/llm-top10.md (this engagement): 10 categories each mapped to a control + test/justification.
    - No untrusted input, no tools, no RAG, no unbounded consumption path (max_tokens cap, 2 calls/day).
- **Impact:** The residual live categories are LLM09 misinformation and LLM05 improper output handling: a hallucinated judgment note reaches the API/SMS with only length/punctuation validation and a disclaimer, no factual grounding check.
- **Fix:** Add a deterministic sanity check on the judgment text (e.g. it must not assert a numeric band inconsistent with the snapshot); keep the disclaimer + AI-generated marking (C-36). Ship the matrix as a living artifact.
- **Substitutions:** S3

### C-07 · Prompt injection — 🟢 PASS (P9)

Prompt-injection containment is architectural, not filter-based: no untrusted free-text reaches the model, and a successful 'injection' could at most perturb a number, never trigger an action (the model has none).

- **Evidence:**
    - judgment.py:139 interpolates only floats/enums; no external free-text in the prompt.
    - No tools/function-calling (repo-wide search) -> a compromised completion cannot act.
    - Worst case: a poisoned upstream price feed shifts an indicator number, which is a data-integrity issue (source health/provenance), not an instruction-injection issue.
- **Impact:** Residual is bounded: the model output flows to the operator's own SMS/JSON as a disclaimered note; no consequential action is reachable from model output.
- **Fix:** None required for containment. Keep the numbers-only prompt invariant; add a test asserting the prompt template contains no externally-sourced free-text field (guards against regression).
- **Substitutions:** S5
- **Residual:** If a future feature feeds external free-text (e.g. news headlines) into the prompt, C-07 re-opens. · **Compensating:** Numbers-only prompt invariant (add a regression test). · **Tripwire:** Test fails if any free-text external field enters PROMPT_TEMPLATE.

### C-09 · EU AI Act — 🟠 PARTIAL (P9)

Classified minimal-risk (a disclaimered research heuristic making no decisions about people) — so no high-risk obligations — but the AI-generated 'judgment call'/SMS are generative text shown to a reader, so Article 50 transparency (disclose AI-generated) applies from 2026-08-02.

- **Evidence:**
    - No decision about any person is produced or acted on; headline is explicitly not advice (references.py DISCLAIMER).
    - judgment_call/SMS are model-generated text surfaced to a human reader with no 'AI-generated' marking (C-36).
- **Impact:** Under Art. 50, AI-generated content presented to a person should be disclosed as such; currently it is not explicitly labelled.
- **Fix:** Add an explicit 'ai_generated: true' marker on the judgment_call field and a one-line SMS/UI disclosure. Record the minimal-risk classification in an AI inventory.
- **Substitutions:** S1

### C-27 · Sector compliance baseline — ⚪ N/A (P9)

No sector-compliance framework binds this product.

- **N/A basis:** No payment data (no PCI DSS), no health data (no HIPAA), no enterprise customers or security attestations claimed or required (no SOC 2 / ISO 27001 obligation). It is a self-hosted, single-operator research tool with a public read-only API. No framework is in scope; none is claimed, so there is no lapsed/expired-attestation finding either.


---

## BLOCKER-2

### A-04 · Testable specification that gates merges — 🟠 PARTIAL (P8)

A rich, testable de-facto specification exists (references.py REGISTRY + README + golden fixtures acting as acceptance tests), but it is not a frozen spec with explicit non-goals and there is no spec-coverage merge gate.

- **Evidence:**
    - references.py:REGISTRY documents what/how/why + weights per indicator; golden fixtures pin deterministic + MC outputs.
    - No given/when/then acceptance file; no CI gate rejecting a change that maps to no criterion.
- **Impact:** Change intent is inferable but not enforced; an unrequested change cannot be mechanically flagged.
- **Fix:** Freeze the spec (the REGISTRY + action-band rules) and add a lightweight requirement->test map; treat golden fixtures as the acceptance gate once CI is green.
- **Substitutions:** S1, S3

### A-09 · Architecture as decided, not accreted — 🟠 PARTIAL (P8)

Clean, deliberate layering (indicators/engine/sources/services/routers) with extensive inline rationale, but no formal ADRs and no architecture fitness functions.

- **Evidence:**
    - Module tree is well-separated; heavy design rationale in comments + references.py.
    - No docs/adr; no import-boundary fitness test.
- **Fix:** Write one-page ADRs for the irreversible decisions (SQLite, single-node, subprocess isolation, hosted-LLM) and add an import-linter boundary test.
- **Substitutions:** S3

### A-10 · Injection-resistant architecture — 🟢 PASS (P8)

Injection resistance is a structural property here: the instruction/data boundary is deterministic because the only model input is machine-computed numbers and the model has no capability to act.

- **Evidence:**
    - Same evidence as C-07.
    - No unmitigated {private data + untrusted content + outbound channel} combination (C-08).
- **Fix:** None; add the numbers-only-prompt regression test (shared with C-07).
- **Substitutions:** S5

### A-17 · Non-functional requirements specified and measured — 🟠 PARTIAL (P8)

Some NFRs are implicit and enforced (subprocess timeouts, rate limit, MC determinism), but there is no NFR table across the nine ISO product-quality characteristics and most have no measured target/test.

- **Evidence:**
    - config.py timeouts + security.py rate limit + seeded MC are de-facto NFRs.
    - No NFR checklist; safety/security of the AI feature not separately assessed.
- **Fix:** Write the nine-characteristic NFR table; attach a number+test to the prioritised ones (latency of recompute, availability, reproducibility).

### A-22 · User outcomes, performance, accessibility — 🟠 PARTIAL (P8)

A self-contained status/dashboard HTML is served, but there is no accessibility (WCAG) audit or gate and no Core Web Vitals / real-user-outcome measurement.

- **Evidence:**
    - status.html served at / and /status.
    - No a11y gate; single-operator tool so user-outcome metrics are low-materiality.
- **Fix:** Run an automated a11y check on status.html; fix blockers; the rest is low materiality for a single-user tool.
- **Substitutions:** S3

### A-25 · Input validation at every boundary — 🔴 FAIL (P8)

GET /api/v1/score/history?from=<garbage> returns HTTP 500: date query params reach datetime.fromisoformat unguarded (score.py:111,113). No property/fuzz tests on parsers/validators.

- **Evidence:**
    - Reproduced via TestClient: /api/v1/score/history?from=garbage -> HTTP 500 'Internal Server Error'.
    - score.py:111 datetime.fromisoformat(from_) / :113 (to) have no try/except and no query validation (unlike granularity which uses a regex pattern).
- **Impact:** Trivially reachable unauthenticated 500 on a public endpoint; also contradicts the 'never 500' guardrail in spirit (README:17). Low security impact, real robustness defect.
- **Fix:** Validate from_/to as ISO dates (Query pattern or explicit parse with a 422 on failure); add a regression test + a property test over the parser.
- **Substitutions:** S3

### A-33 · Maintainability without a maintainer — 🟠 PARTIAL (P8)

A cold-start agent is well-served by references.py + tests, but there is no AGENTS.md, no cold-start-success metric, and no per-module provenance chain.

- **Evidence:**
    - references.py + 15 test files give strong context.
    - No AGENTS.md/CLAUDE.md (repo-wide search).
- **Fix:** Add AGENTS.md (agent constitution); the tests+registry already give a cold agent most of what it needs.
- **Substitutions:** S3, S9

### A-34 · Autonomy levels, gates, self-firing kill switch — 🟠 PARTIAL (P8)

Autonomy is inherently low (scheduled recompute + optional SMS; no tools); kill switches exist as config flags (SMS_ENABLED, scheduler) but fire manually, not on a tripwire.

- **Evidence:**
    - scheduler.py cron; SMS gated by SMS_ENABLED.
    - No irreversible tool action; no automatic halt tripwire.
- **Fix:** Document the (minimal) autonomy level per action; optionally wire an SMS/cost tripwire. Low materiality.
- **Substitutions:** S5, S6, S10

### B-02 · A paved road an agent can walk — 🟠 PARTIAL (P8)

README + Containerfile + deploy.sh give a paved road, but a cold bootstrap requires compiling R exuber, installing lppls, and provisioning many keys; CI (the automated bootstrap proof) is red.

- **Evidence:**
    - Containerfile builds R+exuber from source; pyproject needs lppls; .env needs 8 keys.
    - ci.yml 'image' job (the bootstrap proof) is failing.
- **Fix:** Get CI green so the bootstrap is continuously proven; document the minimal key set to boot in a degraded-but-working state.

### B-05 · Models/prompts/agents have a lifecycle — 🟠 PARTIAL (P8)

Prompts and model config are versioned in git (good), but there is no model/prompt registry, no evaluation history, and no deprecation policy.

- **Evidence:**
    - judgment.py holds the prompt + model list under version control.
    - No registry / eval history / deprecation runbook.
- **Fix:** A lightweight registry (a versioned prompts.py + a model-pin + a CHANGELOG entry per change) suffices at this scale.
- **Substitutions:** S9

### B-07 · Every model/agent run traceable and replayable — 🟠 PARTIAL (P8)

Each completion logs model + shape + error_class, and the snapshot stores judgment text/stale/error — but token counts, cost and a full replay (exact inputs) are not captured.

- **Evidence:**
    - judgment.py:117/121 logs model+shape+error; score.py exposes judgment_call.text/stale/error_class.
    - No token/cost span; no otel GenAI semantic-convention trace.
- **Fix:** Log token usage + a request id per completion; at 2 calls/day a full trace store is optional.
- **Substitutions:** S9

### B-10 · Evaluation gates in the pipeline — 🟠 PARTIAL (P8)

The deterministic score has a frozen-seed golden-fixture regression gate (good), but the LLM output has no evaluation gate — appropriate given it is a disclaimered note, not a gated decision.

- **Evidence:**
    - tests/test_golden_fixture.py pins deterministic score + seeded MC.
    - No golden eval set for the judgment text (none gates a decision).
- **Fix:** If the judgment text ever gates anything, add an eval set with externally-sourced labels; today, a deterministic sanity check (C-05) is proportionate.
- **Substitutions:** S1, S2

### B-12 · Runtime defence — 🟠 PARTIAL (P8)

A reverse proxy (NPM) fronts the app and rootless Podman limits blast radius, but the container runs as root (no USER directive) and patching is manual.

- **Evidence:**
    - Containerfile has no USER; CMD runs uvicorn as root (rootless Podman maps to an unprivileged host uid, mitigating).
    - No WAF; no automated patch SLA.
- **Fix:** Add a non-root USER to the Containerfile; document a patch cadence. Rootless Podman already caps host impact.

### B-13 · Pinned model versions — no floating aliases — 🔴 FAIL (P8)

Model references are floating aliases (claude-opus-4-8, claude-sonnet-5, claude-sonnet-4-6) with no dated snapshot pin; a provider-side model migration can change behaviour with no code change and nothing to roll back to.

- **Evidence:**
    - config.py:15 anthropic_model='claude-opus-4-8' (no date suffix); judgment.py:88 fallback aliases.
    - No CI check failing on an unpinned model reference.
- **Impact:** Silent behaviour drift in the judgment/SMS text; presents as an unexplainable regression with no pinned version to revert to (B-11).
- **Fix:** Pin to a dated snapshot where the provider offers one, or document explicitly that the family alias is the intended canonical id and add a canary re-eval on provider model change; add a lint check for 'latest'-style aliases.
- **Substitutions:** S1

### B-15 · Guardrails are deployed artifacts and fail closed — ⚪ N/A (P8)

No guardrail/classifier layer exists because the architecture needs none (no untrusted input, no tools); the one output-shaping control (_clean_completion) already fails closed.

- **N/A basis:** Guardrails guard an untrusted-content or tool-action boundary; neither exists here. The output shaper _clean_completion (judgment.py:60) degrades to the last-good text or None on any failure — it fails closed by construction. There is no fail-open classifier to kill-test.

### B-28 · Detection that triggers action, not just notification — 🟠 PARTIAL (P8)

/healthz + /readyz + structured logs enable detection, but detection routes to logs/human, not to an automated response; no synthetic monitoring of the critical journey.

- **Evidence:**
    - health router exposes liveness/readiness; deploy.sh acts on /healthz at deploy time.
    - No alert->auto-response binding at runtime; no synthetic canary.
- **Fix:** Add a synthetic check on the recompute freshness + wire it to an automated action (restart/rollback). Break-glass = operator alert.
- **Substitutions:** S8, S10

### B-31 · Backups you have actually restored — 🔴 FAIL (P8)

No documented, tested backup/restore of the SQLite database; no RPO/RTO.

- **Evidence:**
    - DB is a single SQLite file at /data/bubble.db on the host; no backup runbook/restore test in the repo.
- **Impact:** History (score snapshots, caches) is unrecoverable if the volume is lost; a backup that has never been restored is an untested hypothesis.
- **Fix:** Add a scheduled SQLite backup (e.g. sqlite3 .backup) + a restore drill with a recorded duration; state RPO/RTO. Cheap and high-value.
- **Substitutions:** S3

### C-02 · Threat model — 🔴 FAIL (P8)

No threat model existed; one is produced in this engagement (audit/threat-model.md). Without it, no CI staleness check catches a new egress path or integration.

- **Evidence:**
    - Repo-wide search: no threat model.
    - audit/threat-model.md (this engagement): STRIDE per trust boundary for the actual architecture.
- **Impact:** Design-time analysis — the only thing that catches architectural flaws no scanner finds — was absent.
- **Fix:** Ship the threat model; add a CI check that flags a new outbound host / router / secret without a corresponding threat-model entry.
- **Substitutions:** S1, S3

### C-06 · The agentic risk taxonomy — ⚪ N/A (P8)

No agentic system exists; the priority-10 escalation ('any model can call a tool that writes/sends/spends/deletes/executes') does NOT trigger because the model calls no tools.

- **N/A basis:** Repo-wide search finds no tool_use/function_call/MCP/connector. The model produces text only and cannot plan or act. All ten agentic categories (goal hijack, tool misuse, identity/privilege abuse, agentic supply chain, unexpected code execution, memory poisoning, insecure inter-agent comms, cascading failures, human-agent trust exploitation, rogue agents) require a plan-and-act loop that does not exist. The SMS send and recompute are deterministic, non-model-controlled, admin/schedule-gated actions in application code, not model tool calls.

### C-08 · The dangerous three: never combine all — 🟢 PASS (P8)

No session combines the dangerous three: the model's only 'untrusted input' is machine-computed numbers, and it holds no tool to act on any 'private data' or 'external communication' leg.

- **Evidence:**
    - Leg labelling: [untrusted input]=numbers/enums only (not free-text); [private data]=the operator's own readings (not third-party secrets); [external communication]=SMS/JSON are code-driven sinks, not model-controlled.
    - The model has no capability to complete the triangle (no tools).
- **Fix:** Maintain the invariant; the C-07 regression test (no external free-text in the prompt) also defends C-08. Re-audit automatically if any tool/integration is added.
- **Substitutions:** S5, S6

### C-10 · Evaluation methodology — 🟠 PARTIAL (P8)

The deterministic core has a versioned golden dataset + frozen seed + a regression assertion (strong); the non-deterministic LLM text has no offline/online eval regime, which is proportionate since it gates no decision.

- **Evidence:**
    - Golden fixtures + seed 20260711 frozen in tests/config.
    - No eval set for judgment text; none is a gate.
- **Fix:** Keep the deterministic gate (wire it blocking once CI is green); a judged eval set is only needed if the text becomes decision-bearing.
- **Substitutions:** S1

### C-23 · Personal data in prompts, logs and traces — 🟠 PARTIAL (P8)

The operator's own SMS recipient number is written to an INFO log (sipgate.py:63); no redaction-at-emitter, no explicit retention limit on that log field.

- **Evidence:**
    - sipgate.py:63 log.info('sipgate_sms_sent', ..., recipient=to) — the E.164 number in structured logs.
- **Impact:** One personal datum (the operator's own) in logs, outside any retention policy. Low materiality (self-data), but it is PII in a log store.
- **Fix:** Redact/tokenize the recipient at the emitter (log a hash or the last 3 digits); set a retention limit on logs.
- **Substitutions:** S3

### C-26 · SBOM, AI-BOM, provenance, cyber-resilience — 🔴 FAIL (P8)

No SBOM, no AI-BOM, no signed build provenance, no verify-on-deploy. A minimal SBOM+AI-BOM is produced in this engagement as a starting artifact.

- **Evidence:**
    - Repo-wide search: no cyclonedx/spdx/in-toto/cosign.
    - audit/sbom/ (this engagement): a starter AI-BOM listing the model+providers and a dependency inventory.
- **Impact:** No machine-readable bill of materials for the shipped artifact or the AI components; no provenance verified at deploy.
- **Fix:** Generate a CycloneDX SBOM in CI per build; keep the AI-BOM current; add cosign signing + verify-on-deploy if the artifact is ever distributed beyond the self-host.
- **Substitutions:** S1, S9


---

## MUST-FIX

### A-05 · Domain boundaries and consistent vocabulary — 🟠 PARTIAL (P7)

Bounded contexts are clear and the vocabulary is consistent, but boundaries are enforced by convention, not by a fitness function.

- **Fix:** Add an import-linter/architecture test forbidding cross-layer reaches (e.g. routers importing sources directly).
- **Substitutions:** S3

### A-07 · Clone, churn and refactoring signature — 🟠 PARTIAL (P7)

No clone detector or duplication tripwire in CI; the source-adapter modules share structural boilerplate (a clone cluster) that is not measured.

- **Evidence:**
    - app/sources/*.py share fetch/parse/fallback boilerplate; no jscpd/duplication gate.
- **Fix:** Add a duplication tripwire (e.g. jscpd) as a warning-then-gate; de-duplicate the largest source-adapter clone cluster.
- **Substitutions:** S3

### A-11 · Least-privilege topology for agents/tools (design) — 🟠 PARTIAL (P7)

No agent/tool matrix to build (no tools), but the one side-effecting capability (send-SMS) and the outbound data fetches deserve an explicit irreversibility note; egress is not platform-allowlisted.

- **Evidence:**
    - send-SMS is admin-gated + schedule-gated + rate-bounded but is irreversible (cannot unsend).
    - No container egress allowlist.
- **Fix:** Document send-SMS as the one irreversible action (validated by: admin key + recipient allowlist = the fixed operator number); optionally add a container egress allowlist.
- **Substitutions:** S5, S6

### A-12 · Technical debt and the comprehension problem — 🟠 PARTIAL (P7)

Modules trace to references.py + tests (good), but there is no provenance chain and mutation-survival is unmeasured (A-02).

- **Fix:** Add per-release provenance notes; measure mutation score on core logic.
- **Substitutions:** S9

### A-13 · Enforced coding standards — 🟠 PARTIAL (P7)

ruff runs and blocks (good), but mypy is soft-failed ('|| true'), ruff 'S' rules are off, and there is no suppression counter.

- **Evidence:**
    - ci.yml:29 '|| true'; pyproject ruff select omits 'S'; no '# noqa' justification/expiry policy.
- **Fix:** Make mypy blocking (or explicitly tier it); enable ruff 'S'; add a suppression-count guard.
- **Substitutions:** S1, S3

### A-18 · Model/agent architecture chosen deliberately — 🟢 PASS (P7)

The simplest possible model architecture (one hosted-API call, no routing/agents/RAG) is deliberate and documented; the context budget is tiny (a short numeric prompt).

- **Evidence:**
    - judgment.py docstring records the model+fallback rationale; prompt is a few hundred tokens.
- **Fix:** None; record the choice in an ADR (A-09).

### A-19 · API contracts match the implementation — 🟠 PARTIAL (P7)

FastAPI generates the OpenAPI spec from the code, so it matches by construction, and SCORE_EXAMPLE is pinned; but there is no committed spec + drift gate and errors are not RFC 9457 Problem Details.

- **Fix:** Commit the generated openapi.json + a CI drift check; adopt a single error shape.
- **Substitutions:** S3

### A-21 · Context, retrieval and memory architecture — ⚪ N/A (P7)

No retrieval or memory architecture exists.

- **N/A basis:** The model prompt is a fixed template of computed numbers; there is no retrieval, no vector index, no conversational/persistent memory. Context occupancy is a few hundred tokens, far below any effective-window concern.

### A-23 · Data architecture and ownership — 🟠 PARTIAL (P7)

Schemas are versioned via Alembic migrations (good) and the science-audit coverage gate watches data usability, but there is no schema-drift detector and no scheduled data-quality job with thresholds.

- **Evidence:**
    - migrations/versions/0001-0004; compute.py coverage gate.
    - No drift detection on cache tables.
- **Fix:** Add lightweight data-quality assertions on the cache tables; the coverage gate already partially covers this.
- **Substitutions:** S3

### A-26 · Error handling that does not lie — 🟠 PARTIAL (P7)

Most exception handling degrades deliberately with a comment and a log, but several handlers swallow silently (no log, no machine-readable justification), and there is no lint rule against new bare handlers.

- **Evidence:**
    - Silent handlers: stooq.py:133/140 (pass), compute.py:357/377 (pass), prices.py:353/444 (return None), vix.py:74 (silent fallback), edgar.py:140 (continue), status.py:259 (default).
    - Good handlers rollback+raise or log+raise (db.py:51, http_client.py:132).
- **Impact:** A silently-swallowed failure in a source path can degrade output with no log trail; in a no-reader system those lies are never contradicted.
- **Fix:** Add a log/justification annotation to each surviving swallow; enable ruff 'S110' (try-except-pass) to block new ones.
- **Substitutions:** S1, S3

### A-27 · AI-specific non-functional requirements — 🟠 PARTIAL (P7)

No enforced latency/cost/hallucination budget on the AI feature — but exposure is tiny (max_tokens=8000 caps a call; 2 scheduled calls/day; single-flight lock prevents stacking).

- **Fix:** Set a documented cost ceiling + a groundedness sanity check (C-05); enforcement is low-priority given the 2-calls/day envelope.
- **Substitutions:** S6

### A-28 · Dependency topology and blast radius — 🟠 PARTIAL (P7)

Sources have timeouts, tenacity retries, a circuit breaker (http_client) and fallback chains (good), but there is no written blast-radius/dependency-criticality map or SBOM.

- **Evidence:**
    - http_client.py circuit breaker; sources fallback chains; no dependency map doc.
- **Fix:** Write the one-page dependency-criticality map (already largely in references.py source specs); add an SBOM (C-26).
- **Substitutions:** S6

### A-32 · Documentation that is true and executable — 🟠 PARTIAL (P7)

README is largely true (claims ledger) but there is no AGENTS.md (the agent constitution the mandate calls the most important doc), the .env.example is stale (LPPLS_TIMEOUT_S=600 vs 1500), and docs are not executed in CI.

- **Evidence:**
    - .env.example:37 LPPLS_TIMEOUT_S=600 (code default 1500; 600 caused the prod outage).
    - No AGENTS.md/CLAUDE.md.
- **Fix:** Add AGENTS.md; fix .env.example; add a docs-as-tests check for README commands. (Phase 5.)
- **Substitutions:** S3

### A-38 · Provenance and licensing of shipped code — 🟠 PARTIAL (P7)

No LICENSE file, no license scanning, no SBOM, and no written IP position on machine-generated code.

- **Evidence:**
    - No LICENSE/COPYING file in the repo.
    - No license scan in CI.
- **Fix:** Add a LICENSE; add license scanning + an SBOM (C-26); write a one-line IP-position note (essentially all code is machine-generated — copyright status is uncertain in the US).
- **Substitutions:** S1, S9

### B-08 · No path consumes unbounded tokens/money — 🟠 PARTIAL (P7)

max_tokens caps a single completion and the admin-refresh single-flight lock prevents stacked recomputes, but there is no per-path cost cap enforced in infrastructure — exposure is bounded by the 2-calls/day schedule and admin-gated manual trigger.

- **Evidence:**
    - config.py anthropic_max_tokens=8000; admin.py recompute_lock single-flight.
    - No infra cost cut-off.
- **Fix:** Set an Anthropic spend cap at the provider console; the app-level envelope is already small.
- **Substitutions:** S6

### B-09 · Signed provenance for everything shipped — 🟠 PARTIAL (P7)

No signed build provenance / SBOM verified at deploy; deploy.sh does verify health and auto-rolls-back, which is a partial deploy-time integrity check.

- **Fix:** Add SBOM (C-26); sign images with cosign + verify-on-deploy if ever distributed.
- **Substitutions:** S9

### B-17 · Infrastructure as code, reconciled from VC — 🟠 PARTIAL (P7)

compose.yml + Containerfile are declarative (good), but the host (NPM, .env, cron, volumes) is hand-configured and there is no drift detection.

- **Fix:** Capture the host config (NPM proxy, systemd/podman unit) as code; add drift detection. Low priority for a single host.
- **Substitutions:** S1

### B-19 · Service objectives with an error budget that bites — 🔴 FAIL (P7)

No SLOs, no SLIs, no error budget, so nothing freezes releases on reliability breach.

- **Fix:** Define a simple availability SLO + freshness SLI; the 'freeze' is proportionate as an alert for a single-operator tool.
- **Substitutions:** S1

### B-23 · Behavioural baselines for agents — ⚪ N/A (P7)

No agents to baseline.

- **N/A basis:** No agentic behaviour exists (see C-06). There is no per-agent tool-call/API pattern to baseline; the scheduled recompute is a fixed cron job.

### B-24 · Quality drift detection — 🟠 PARTIAL (P7)

Data-degradation drift is detected (science audit + coverage gate), but model-output quality drift is not, and the unpinned model (B-13) means a provider change could shift output silently with no canary.

- **Fix:** Pin the model (B-13) and add a canary re-eval on model change; sample the judgment text for obvious drift.
- **Substitutions:** S8

### B-25 · Environment separation and parity — ⚪ N/A (P7)

Effectively one environment (prod on the host) plus local dev; no staging points at a production data plane because there is no staging.

- **N/A basis:** There is a single production deployment and a developer checkout. No non-production environment holds a production connection string/credential because no such environment exists. The risk the check guards (staging->prod DB) is not present. Recorded with the note that adding a staging environment would re-activate this check (rotate + assert separation then).

### B-27 · Artifact integrity — 🟠 PARTIAL (P7)

Images are content-addressed by commit tag and deploy.sh verifies health, but images are not cryptographically signed and there is no verify-on-deploy admission policy.

- **Fix:** cosign sign + verify if the artifact is ever pulled from a shared registry; for a self-host build-run host the exposure is low.
- **Substitutions:** S1, S9

### B-29 · Reliability primitives validated by breaking things — 🟠 PARTIAL (P7)

Circuit breaker + timeouts + tenacity retries exist and are unit-tested, but they have not been validated by injected failure / chaos on a schedule.

- **Fix:** Add one scheduled fault-injection test (kill a source, add latency) asserting graceful degradation.
- **Substitutions:** S3

### B-33 · Integrity of the retrieval corpus — ⚪ N/A (P7)

No retrieval corpus / vector store exists.

- **N/A basis:** There is no embedding store or retrieved context; nothing can be poisoned or go stale in a retrieval index because there is none.

### C-11 · A running AI risk-management programme — 🟠 PARTIAL (P7)

No formal NIST-AI-RMF programme, but the science-audit + disclaimers + provenance notes constitute a lightweight, durable, pipeline-emitted risk-disclosure artifact.

- **Fix:** Record an AI inventory + intended-use note; the science audit already emits ongoing evidence.
- **Substitutions:** S9

### C-12 · Excessive agency and the autonomy policy — ⚪ N/A (P7)

Least agency holds trivially: the model has no tools, no permissions, and makes no decision alone that is acted upon.

- **N/A basis:** Excessive functionality/permissions/autonomy all require agency. The model has zero tools (functionality), zero credentials (permissions), and zero act-upon-able decisions (autonomy) — its output is a disclaimered note. The kill switch is disabling the feature flag. Nothing to cut.

### C-15 · Guardrails have their own tests and adversary — ⚪ N/A (P7)

No guardrail layer exists (see B-15); there is nothing to adversarially test.

- **N/A basis:** Same basis as B-15: no classifier/guardrail mediates any untrusted-input or tool-action boundary, because neither boundary exists.

### C-16 · Machine identity and agent-to-agent trust — 🟠 PARTIAL (P7)

All machine identities are static, long-lived API keys with no per-purpose scoping or rotation; no agent borrows a human session (there are no agents), but the single admin key is shared across write endpoints.

- **Evidence:**
    - 8 static provider keys + 1 admin key in .env; no per-agent identity, no rotation (B-06).
- **Fix:** Rotate (B-06); at this scale per-purpose keys are optional but provider-scoped keys + expiry where offered are cheap wins.
- **Substitutions:** S6, S9

### C-17 · Tool poisoning — ⚪ N/A (P7)

No tools/tool-descriptions are given to the model.

- **N/A basis:** Tool poisoning requires tool definitions read by the model; there are none (no function-calling/MCP/connectors). Nothing to pin/diff.

### C-18 · Connector and tool-server security — ⚪ N/A (P7)

No external tool/connector servers are connected.

- **N/A basis:** The model connects to no MCP/tool server. The outbound HTTPS calls are ordinary REST data fetches in application code (app/sources/*), not model-driven connectors; each targets a fixed, hard-coded host.

### C-21 · Training and fine-tuning data governance — ⚪ N/A (P7)

No custom or fine-tuned model and no training/tuning datasets exist.

- **N/A basis:** All inference is against the Anthropic hosted API; nothing is trained or fine-tuned; there is no training/tuning dataset to govern.

### C-22 · Retrieval evaluated separately from generation — ⚪ N/A (P7)

No retrieval-augmented generation; there is no retriever to evaluate separately from a generator.

- **N/A basis:** The model receives a fixed numeric prompt, not retrieved passages; there is no retrieval step to decompose.

### C-24 · Disclosure via memorisation/system-prompt leakage — 🟢 PASS (P7)

No secrets or personal data live in the prompt or system context (verified: prompt interpolates numbers/enums only); system-prompt leakage would reveal nothing sensitive.

- **Evidence:**
    - judgment.py:139 prompt fields are all computed numbers/enums; no key/URL/PII in the template.
    - No security control is enforced inside the model (auth/rate-limit/CORS are all in code).
- **Fix:** Maintain the invariant (shared C-07 regression test). Security controls are already outside the model.
- **Substitutions:** S1, S3

### C-28 · Residency and regulatory scope — 🟠 PARTIAL (P7)

Storage/processing is on the operator's own EU host; the main cross-border flow is numbers-only inference to Anthropic. Residency is de-facto but not asserted/enforced.

- **Fix:** Note the residency position (self-hosted DE; only non-PII numeric prompts leave for inference). Low materiality.

### C-34 · Provider training on your data — 🟠 PARTIAL (P7)

No in-repo assertion of the Anthropic data-use position. Anthropic's commercial API does not train on API inputs by default, and the prompt carries no sensitive data — but this is neither documented nor programmatically re-checked.

- **Evidence:**
    - Prompt is numbers-only (no customer data to train on regardless).
    - No documented DPA/no-train assertion in-repo.
- **Fix:** Document the provider data-use position in SECURITY.md; the numbers-only prompt makes the exposure nil regardless.
- **Substitutions:** S3


---

## SHOULD-FIX

### A-03 · Deterministic vs probabilistic assertions kept apart — 🟠 PARTIAL (P6)

Deterministic behaviour is hard-asserted and the LLM text is tested only deterministically (_clean_completion, degradation), so no brittle string-match on model output — but there is no separated 'judged' class because nothing is judged.

- **Fix:** None needed; the separation is effectively clean. Keep any future judged check out of the hot path.
- **Substitutions:** S3

### A-14 · A written policy for how AI builds/maintains this — 🔴 FAIL (P6)

No written policy for how AI builds/maintains this system (which models/tools for which change classes, what must be verified before merge).

- **Fix:** Add a short AI-build policy to AGENTS.md with a verification tier per change class (docs vs core-scoring vs auth/deploy).

### A-16 · Last mile: stubs, edge cases, integration — 🟠 PARTIAL (P6)

No production stubs/mocks/NotImplemented in production paths (verified) — the only marker was references.py:70 'TODO: verify citations', now resolved in this engagement.

- **Evidence:**
    - grep of app/ for TODO/FIXME/NotImplemented/mock/pass# : only the citation TODO + 'NEVER a placeholder' design comments; no functional stub.
    - All three citations verified real (claims ledger #3).
- **Fix:** Clear the citation TODO/flag (Phase 5). Add a stub-detector lint rule to keep the class out.
- **Substitutions:** S3

### A-20 · Prompts/retrieval config are code and pass the gate — 🟠 PARTIAL (P6)

Prompts are in version control (good) and there is no hot-swap path to production, but prompt changes ride the same red CI and there is no eval gate on them.

- **Fix:** Route prompt changes through the (fixed) gate + a minimal eval; already in git.
- **Substitutions:** S1

### A-29 · Build vs buy vs open source, including the model — 🟠 PARTIAL (P6)

The intra-Anthropic fallback chain gives partial model resilience, but there is no cross-vendor abstraction, no TCO/exit-plan doc, and no model-deprecation monitor.

- **Fix:** Note the exit plan (the judgment/SMS degrade to deterministic templates if Anthropic is gone — a real, tested exit); add a deprecation alert.

### A-30 · Non-functional trade-offs analysed — 🟠 PARTIAL (P6)

Trade-offs (single-node SQLite for consistency-over-availability, subprocess isolation for safety-over-speed) are implicit in comments but not written as decisions.

- **Fix:** Capture in ADRs (A-09).

### A-31 · Unit economics decided at design time — 🟠 PARTIAL (P6)

(see MUST band note; duplicated priority list resolves A-31 at 6) Unit economics are de minimis and not modelled.

- **Fix:** One-line cost note.
- **Substitutions:** S6

### A-35 · Runtime containment without an operator — 🟢 PASS (P6)

There are zero load-bearing approval queues — the mandate's ideal end state — because the system has no pending-human-action state at all.

- **Evidence:**
    - No approval/pending-action state in any router or service; recompute is single-flight automatic; SMS is schedule/admin gated, not queued for approval.
- **Fix:** None; this is the target condition. Runtime containment is limited (A-24) but not gated on a human queue.
- **Substitutions:** S5, S10

### A-36 · Calibration of the verification pipeline — 🟠 PARTIAL (P6)

Pipeline detection rate is now measured (audit/02: ~1/6) but was never tracked before, is not trended, and no game day has been run.

- **Fix:** Track the seeded-defect catch rate per release; run a periodic game day (disable auto-rollback, induce a failure).
- **Substitutions:** S2, S4

### B-14 · Governance over prompt/config changes — 🟠 PARTIAL (P6)

Prompt/config changes are in git with commit history (audit trail), but they pass only the same red CI and have no separate governance record.

- **Fix:** Once CI is green, config-as-code through the gate suffices at this scale.
- **Substitutions:** S1

### B-16 · Cost attribution and finops — ⚪ N/A (P6)

AI/cloud spend is de minimis (owned hardware; 2 LLM calls/day; free-tier data APIs) and needs no attribution/chargeback (single operator).

- **N/A basis:** There is one 'tenant' (the operator) and negligible spend; cost attribution/chargeback has no addressee. Carry a provider spend cap under B-08.

### B-18 · Progressive delivery with automatic abort — 🟠 PARTIAL (P6)

No progressive delivery/canary (single instance, 100% blast radius) — but deploy.sh's health-check auto-rollback bounds the exposure of a boot-breaking change.

- **Fix:** A blue/green on one host is heavy; the deploy-time auto-rollback is proportionate. Wire an output tripwire to it (B-26).
- **Substitutions:** S8

### B-21 · The application survives a provider outage — 🟢 PASS (P6)

The application survives an Anthropic outage: the intra-vendor model fallback chain plus deterministic degradation (judgment -> stale/None; SMS -> deterministic template) keep the service up; data sources have their own fallback chains.

- **Evidence:**
    - judgment.py degrades to last-good/None on any API failure; sms_report.py + digest fall back to a deterministic template; README:151 documents 'always sends'.
    - Sources: multi-provider fallback chains + circuit breaker.
- **Fix:** None for continuity; single-vendor for the LLM is a documented, tested-degradation risk, not an outage.
- **Substitutions:** S5

### B-26 · Feature flags and self-firing kill switches — 🟠 PARTIAL (P6)

Feature flags/kill switches exist as config (SMS_ENABLED, STOOQ_ENABLED, READ_ENDPOINTS_PUBLIC, GSADF_CONTESTED) but flip manually, not on a tripwire.

- **Fix:** Wire an automatic tripwire (e.g. disable SMS on repeated send failures / cost spike).
- **Substitutions:** S8, S10

### B-30 · Capacity, including inference capacity — 🟢 PASS (P6)

Load is trivial (single operator + a public read-only API behind a rate limit) and inference capacity is 2 calls/day; the documented binding constraint is the Atom CPU, planned around (numpy/pyarrow pins, subprocess timeouts).

- **Fix:** None; capacity is understood and bounded.
- **Substitutions:** S6

### B-35 · Segregate the gate from the gated — 🔴 FAIL (P6)

No segregation of the gate from the gated: the same identity that pushes code can edit ci.yml; there is no separate policy repo or separate write credentials.

- **Evidence:**
    - Single repo, single owner; ci.yml lives beside the code and is writable by the code-author identity.
- **Impact:** The author can rewrite the rules it is judged by — in a no-reviewer model this means there is effectively no gate.
- **Fix:** Move the merge-gate policy (required checks) to branch-protection settings the code-writing automation cannot change; or a separate policy config with separate write access. At single-owner scale, branch protection owned by the human-in-command is the proportionate form.
- **Substitutions:** S1, S9

### B-36 · Model deprecation is a plan, not an outage — 🟠 PARTIAL (P6)

No automated model-deprecation tracking; the fallback chain and deterministic degradation cushion a forced deprecation, but a floating alias (B-13) means the change could arrive silently.

- **Fix:** Pin the model + subscribe an alert to provider model-lifecycle notices; the degradation path already prevents an outage.

### C-13 · AI management system — ⚪ N/A (P6)

No ISO/IEC 42001 AI management system is claimed.

- **N/A basis:** No AIMS certification is asserted anywhere, so there is no unsubstantiated claim to fault and no statement-of-applicability obligation. Its absence is not fatal for a single-operator research tool.

### C-14 · Validating the judge — ⚪ N/A (P6)

No model-as-judge gates any decision.

- **N/A basis:** Nothing in the pipeline uses a model to grade another model's output as a gate. The judgment text is generated, not judged; the deterministic score is gated by golden fixtures, not by an LLM judge.

### C-19 · Memory and context poisoning — ⚪ N/A (P6)

No persistent agent memory or writable retrieved context.

- **N/A basis:** There is no memory store and no retrieval context; nothing persists across sessions to poison (each recompute rebuilds state from source data + the DB caches, which are not model-writable).

### C-20 · Responsible-AI dimensions with owners and numbers — 🟠 PARTIAL (P6)

The relevant responsible-AI dimensions here — transparency, veracity, contestability — are addressed (disclaimers, science audit, open methodology); fairness/bias is N/A (no decisions about people). None has an automated scheduled measure.

- **Fix:** Keep the science audit; fairness/bias testing is not applicable (no protected-attribute decisions).
- **Substitutions:** S3

### C-25 · Copyright and licensing of generated code — 🟠 PARTIAL (P6)

No LICENSE, no license scan of generated code, no written IP position — the code is essentially all machine-generated (uncertain US copyright status).

- **Fix:** Add a LICENSE + license scan + a one-line IP note (shared with A-38).
- **Substitutions:** S1, S9

### C-29 · Content safety and misuse prevention — ⚪ N/A (P6)

No user-generated content and no third-party-facing generative feature to abuse.

- **N/A basis:** The only generative output is the operator's own daily note/SMS to himself. There is no user population that could submit content or receive generated content, so content-safety classification/misuse-monitoring has no subject. The public API serves numeric research data, not generated media.

### C-30 · Jailbreak resistance — ⚪ N/A (P6)

The model has no adversarial user: its input is numbers from the operator's own pipeline, not attacker-supplied prompts.

- **N/A basis:** Jailbreak resistance concerns an adversary who can craft model input. Here the model input is machine-computed and not user-controllable; there is no prompt surface for an attacker to jailbreak.

### C-32 · Vector and embedding weaknesses — ⚪ N/A (P6)

No vector store or embeddings.

- **N/A basis:** No embeddings are computed or stored; there is no vector store to invert, isolate, or poison.

### C-33 · An AI usage policy that is enforced — 🔴 FAIL (P6)

No published/enforced AI usage policy (same root as A-14).

- **Fix:** Add a short AI usage + build policy (AGENTS.md/SECURITY.md); enforce the machine-relevant clauses (permitted model pinned at the gateway/config, permitted change classes) via the gate.
- **Substitutions:** S1

### C-36 · Transparency and marking of generated output — 🟠 PARTIAL (P6)

The judgment_call and SMS are AI-generated but not explicitly marked as such to the reader (ties to C-09 Art. 50).

- **Fix:** Add an 'ai_generated: true' marker on the judgment_call field + a one-line SMS/UI disclosure.

### C-38 · Fabrication and downstream reliance — 🟠 PARTIAL (P6)

The system's own cited references were verified real in this engagement (positive), but there is no programmatic citation/groundedness check on the model's generated text — a hallucinated judgment note is caught only by length/punctuation validation + the disclaimer.

- **Evidence:**
    - All three flagged citations resolve to real sources (claims ledger #3).
    - judgment.py validates only shape (_clean_completion); no factual grounding against the snapshot numbers.
- **Impact:** Low stakes (single user, disclaimered, not advice), but the note could misstate the snapshot it summarises.
- **Fix:** Clear the stale citation flag (Phase 5); add a deterministic sanity check that the note's stated direction/band is consistent with the snapshot (C-05).
- **Substitutions:** S3


---

## PLAN

### A-15 · Boundary between prototype and production — 🟠 PARTIAL (P5)

No written prototype/production boundary; 'gated vs ungated' is moot because the single gate is red — but there are no ungated hotfix/skip-ci paths either (all changes go through the same CI, red though it is).

- **Fix:** Once CI is green+blocking, state the boundary explicitly.
- **Substitutions:** S1

### A-37 · Takeover readiness — 🟠 PARTIAL (P5)

references.py + tests aid both agent and human takeover, but there is no takeover pack and no AGENTS.md, and time-to-first-safe-change is untracked.

- **Fix:** AGENTS.md + a short takeover note.

### A-39 · Verification loop must not be self-referential — 🔴 FAIL (P5) → **escalates to STOP-SHIP (A-01+A-39)**

The verification loop is fully self-referential: the same model family authored the code and (would) author its tests, and no independent, different-vendor adversarial verifier or deterministic arbiter exists. STOP-SHIP by §3 escalation (with A-01).

- **Evidence:**
    - No second-vendor verifier anywhere in the process.
    - Tests were authored by the same model family that wrote the code (this engagement's Phase-5 fixes use a same-vendor sub-agent as only a partial S2 — disclosed).
- **Impact:** No independent witness to any production line; self-preference bias is unmeasured.
- **Fix:** Introduce a different-vendor adversarial verifier for high-stakes changes; make the deterministic gate (A-01) the sole merge authority; generate tests from the frozen spec, not the code.
- **Substitutions:** S2
- **Residual:** A genuinely different-vendor verifier is not available within this engagement. · **Compensating:** Deterministic golden-fixture + mutation gate (once wired) as the non-model arbiter. · **Tripwire:** Mutation score below threshold blocks merge.

### B-32 · Drift detection — 🟠 PARTIAL (P5)

No IaC drift detection/reconciliation for the host config.

- **Fix:** Low priority for one host; capture host config as code first (B-17).
- **Substitutions:** S1

### B-34 · Latency budgets for inference — ⚪ N/A (P5)

No interactive/latency-sensitive inference path.

- **N/A basis:** The LLM call runs in the background recompute and the daily digest; no user waits on a token stream. Interactive latency budgets (TTFT, etc.) do not apply. The public API serves the last precomputed snapshot with no model call in the request path.

### B-37 · Retirement of AI artifacts and right to erasure — ⚪ N/A (P5)

No third-party personal data, so no right-to-erasure machinery is required.

- **N/A basis:** The only personal datum is the operator's own phone/email (config), deletable by editing .env. There is no user base, no derived personal-data store (no embeddings/training set), so there is nothing to erase on request from a third party.

### B-39 · Design against a named reference framework — 🟠 PARTIAL (P5)

The platform was not designed against a named reference framework (12-factor/well-architected), though several 12-factor properties hold incidentally (config in env, stateless-ish process, logs as streams).

- **Fix:** Run a lightweight 12-factor/well-architected gap review; record the gaps.

### C-31 · Adversarial technique taxonomy / agentic layers — ⚪ N/A (P5)

No agentic layers to decompose against a seven-layer agentic threat model.

- **N/A basis:** The agentic threat layers (agent frameworks, agent ecosystem, inter-agent comms) do not exist here. The non-agentic threat surface is covered by the STRIDE threat model produced this engagement (audit/threat-model.md).

### C-37 · Accountability without a signature — 🟠 PARTIAL (P5)

No attested provenance chain for production artifacts (which model/prompt/spec/gate/evidence produced any given line) — per §3 this escalates toward BLOCKER-1; the mitigating fact is a single named owning role (the operator) who is accountable regardless of provenance.

- **Evidence:**
    - No per-commit provenance tags (model, prompt hash, spec id, policy bundle); git history is the only record.
    - Single owning role is unambiguous (operator).
- **Impact:** The organisation cannot reconstruct which model/prompt produced a given change beyond git blame; accountability rests on the owner by default, not on a demonstrable chain.
- **Fix:** Add provenance notes per release (model+prompt+spec+gate); the single-owner accountability is already clear. Band held at PLAN-with-escalation-noted given single-operator materiality.
- **Substitutions:** S9
- **Residual:** Full attested provenance is disproportionate for a single-maintainer tool; git history + release notes are the proportionate substitute. · **Compensating:** Git history + CHANGELOG + this audit as the reconstruction record. · **Tripwire:** A release without a CHANGELOG entry is the tripwire (add a CI check).


---

## ASSESS

### A-40 · Energy and carbon as a design constraint — ⚪ N/A (P4)

Energy/carbon is immaterial: an Atom N2800 (~6.5W) running a small service with 2 short LLM calls/day.

- **N/A basis:** The workload is a single low-power (Atom N2800) always-on host plus two short hosted-inference calls per day. Both the compute and inference footprints are negligible; there is no scale at which a Software Carbon Intensity measurement would change a design decision. Documented immaterial, no invented number.

### B-38 · Inference economics — ⚪ N/A (P4)

Inference economics/caching is immaterial (2 calls/day).

- **N/A basis:** Prompt caching optimises high-volume repeated-prefix inference; at 2 calls/day the savings are nil. Not worth the complexity.

### C-35 · Benchmark contamination / who evaluates the evaluator — ⚪ N/A (P4)

No benchmark numbers are used in any product claim, and no evaluation set is exposed to a provider.

- **N/A basis:** The product makes no benchmark/accuracy claim (it explicitly states it is uncalibrated). There is no held-out eval set to contaminate and no marketing number to defend under re-run.

### C-39 · Lifecycle process and vocabulary standards — ⚪ N/A (P4)

No AI management system is claimed, so lifecycle/vocabulary-standard alignment is not required.

- **N/A basis:** Same basis as C-13: with no AIMS claim there is no standards-alignment obligation and no hollow claim to correct.

### C-40 · Societal and environmental impact — ⚪ N/A (P4)

Societal/environmental impact is immaterial for a single-operator research heuristic.

- **N/A basis:** No high-impact use case (no decisions about people, no scaled deployment); environmental footprint is negligible (A-40). Documented immaterial.
