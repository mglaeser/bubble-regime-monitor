# Evidence — CI run history (GitHub Actions `ci.yml`)

Retrieved via GitHub API (`actions_list`, method `list_workflow_runs`) during this engagement. Total runs on record: 43. The 10 most recent (newest first):

| Run # | status / conclusion | event | branch | created |
|---|---|---|---|---|
| 43 | completed / **failure** | pull_request | claude/bubblegauge-build-spec-fzthju | 2026-07-14T15:17:47Z |
| 42 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T15:17:44Z |
| 41 | completed / **failure** | pull_request | claude/bubblegauge-build-spec-fzthju | 2026-07-14T14:56:07Z |
| 40 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T14:56:02Z |
| 39 | completed / **failure** | pull_request | claude/bubblegauge-build-spec-fzthju | 2026-07-14T13:59:13Z |
| 38 | completed / **failure** | push | **bubblegauge-pre-v3.3.0** (base) | 2026-07-14T13:58:34Z |
| 37 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T13:36:14Z |
| 36 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T12:46:49Z |
| 35 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T12:42:32Z |
| 34 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T12:03:56Z |

## CORRECTED ROOT CAUSE — the jobs never execute (infrastructure)

An initial hypothesis attributed the red `test` job to the CI install list omitting `lppls`/`anthropic`. **Job-timing evidence refutes that as the operative cause and is recorded here rather than a plausible-but-wrong reconstruction (§0 "confident absence").**

Per-job data (`list_workflow_jobs`) for the pre-existing run 42 (`b8d46bc`) **and** the audit commit run 44 (`eaafdab`):

| Run | Job | started → completed | duration | logs |
|---|---|---|---|---|
| 42 (pre-audit) | test (3.12) | 15:17:45 → 15:17:48 | **~3s** | 404 (none) |
| 42 (pre-audit) | image | 15:17:45 → 15:17:49 | **~4s** | 404 (none) |
| 44 (audit) | test (3.12) | 16:19:42 → 16:19:45 | **~3s** | 404 (none) |
| 44 (audit) | image | 16:19:42 → 16:19:45 | **~3s** | 404 (none) |

A job that "completes" in ~3 seconds with **no downloadable logs** never ran any steps — **no runner executed it.** This is consistent across the pre-existing runs and the audit run, and the repository's git remote in this environment is a **local proxy** (`127.0.0.1:...`), i.e. a mirrored GitHub instance where **Actions job execution is not wired up**. The red CI is therefore an **infrastructure condition — Actions cannot run here — not a content failure.**

## What this establishes (corrected)

- The repository's only automated gate has been **red on every run** — because **it never executes**. A gate that cannot run is a stronger form of "decorative": it has *never* verified a single line. **A-01 / B-01 / A-39 FAIL stands, and is more severe than first stated.**
- Production (`a916e8d`) was **deployed with CI red** and no required-status-check prevented it.
- **Consequence for remediation:** the rebuilt gate (blocking ruff+`S` / pip-audit / detect-secrets / pytest) is verified **GREEN LOCALLY ONLY** (`audit/05`, all four gate commands pass). It **cannot be confirmed green on a runner in this environment** because no runner runs. A prerequisite for the gate to be real is therefore **a functioning CI executor** (working Actions runners, or an alternative CI/pre-commit/pre-push executor on the deploy host). This is added to the residual register (`audit/06`, A-01).
- The content fixes the initial hypothesis pointed at were nonetheless real and are done anyway: the suite is now **hermetic** (LPPLS path self-skips; locally `161→171` passing) and CI installs `anthropic`/`xlrd` (A-02, B-04) — so when an executor exists, the gate is ready to pass.
