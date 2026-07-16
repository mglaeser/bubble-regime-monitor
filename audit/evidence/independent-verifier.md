# Evidence — independent adversarial verification (partial S2)

**Limitation (disclosed, finding A-39):** the independent verifier is a *fresh agent of the same vendor/model family* with a falsifying objective ("break these fixes"). This is a **partial** S2 — a genuinely different-vendor verifier is not available in this engagement and remains an open residual risk (`audit/06`).

## Targets submitted for refutation

The verifier was given the five code fixes and instructed to find a bypass, boundary case, or regression for each (not to bless them):

1. Admin-key fail-closed (`app/security.py`) — try to authenticate with the placeholder/empty key, whitespace/case variants, or a route not covered by `require_admin_key`.
2. `/score/history` date validation (`app/routers/score.py`) — find any query input that still returns HTTP 500.
3. Numbers-only prompt invariant — find any path where external free-text reaches the model prompt; check whether the SMS prompt (`sms_report.py`) is a hole the `judgment.PROMPT_TEMPLATE` test misses.
4. LPPLS import-order fix (`app/indicators/d4_lppls.py`) — find another data-shortfall path that still hard-imports `lppls`; check `compute_confidence_isolated`.
5. CI gate (`.github/workflows/ci.yml`) — find a remaining soft-fail on a blocking step; check the secret-scan and install completeness.

## Result — the verifier found four surviving issues (S2 working as intended)

It ran the full suite (`171 passed`) and ruff clean, then attacked each fix. Verdicts:

| Fix | Verdict | What it found |
|---|---|---|
| 1 · admin fail-closed | **HOLDS** (+2 nits) | Cannot authenticate with placeholder/empty; all 3 admin routes guarded. **Nits:** (a) a non-ASCII `X-API-Key` made `compare_digest` raise `TypeError` → **500**; (b) `require_read_access` lacked the placeholder guard — a **clone** of the very bug fix 1 closed. |
| 2 · `/history` dates | **BYPASSED** | `?to=9999-12-31` → **500**: `_parse_date_bound` parses it, then `to_dt + timedelta(days=1)` overflows `datetime.max` (uncaught). |
| 3 · numbers-only prompt | **HOLDS, coverage gap** | The judgment prompt is clean, but `sms_report.py:SMS_PROMPT` is a **second** prompt no test covered; it interpolates `{judgment}` (free-text, though prior-LLM output). The invariant gave **false assurance** of covering "the LLM prompt." |
| 4 · LPPLS import order | **HOLDS** | Verified with both `lppls` and `numpy` import-blocked; `compute_confidence` and `compute_confidence_isolated` both return INSUFFICIENT_DATA; the module top-level imports nothing third-party. |
| 5 · CI gate | **HOLE** | ruff/pip-audit/pytest are genuinely blocking, mypy honestly advisory. **But** the secret-scan step used `detect-secrets scan --baseline`, which **exits 0 even on a new secret** — proven by planting an `AKIA…` key and running the exact CI command (exit 0). Effectively advisory despite the "BLOCKING" label. |

## Response — all four acted on (fixed + regression-tested), not just recorded

Per the mandate (fix confirmed findings; the verifier's verdict is advisory but these were reproduced):

- **Fix 2 overflow** → `score.py` guards `to_dt + timedelta` with `except OverflowError → datetime.max`. Test `test_far_future_to_date_no_500`.
- **Fix 1b clone** → `require_read_access` now shares the `_require_configured_key` fail-closed guard. Test `TestReadAccessFailClosed`.
- **Fix 1a non-ASCII** → `_key_matches` encodes to bytes before `compare_digest` (no TypeError). Test `test_non_ascii_key_is_401_not_500`.
- **Fix 3 SMS gap** → new `test_sms_prompt_only_prior_llm_free_text` asserts `SMS_PROMPT` fields are the numeric set plus exactly `{limit, judgment}`, with `judgment` documented as prior-LLM output — removing the false assurance.
- **Fix 5 secret-scan** → CI now runs `git ls-files -z | xargs -0 detect-secrets-hook --baseline .secrets.baseline`, which **exits 1** on a new secret (verified locally: clean tree exit 0; planted `AKIA…` exit 1; the old `scan --baseline` exit 0 on the same plant).

Suite after the response: **176 passed** (was 171; +5 tests), ruff clean, pip-audit clean.

**This is the value of S2 made concrete:** a same-family auditor (me) shipped a fix (2) that was outright bypassable and a gate (5) that did not gate, and left two clones/gaps (1b, 3). An adversarial pass caught all four. It also underlines the residual A-39: this verifier is still *same-vendor* — a genuinely different-vendor pass could surface a class this one and I both share.
