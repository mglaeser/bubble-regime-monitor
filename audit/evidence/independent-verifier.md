# Evidence — independent adversarial verification (partial S2)

**Limitation (disclosed, finding A-39):** the independent verifier is a *fresh agent of the same vendor/model family* with a falsifying objective ("break these fixes"). This is a **partial** S2 — a genuinely different-vendor verifier is not available in this engagement and remains an open residual risk (`audit/06`).

## Targets submitted for refutation

The verifier was given the five code fixes and instructed to find a bypass, boundary case, or regression for each (not to bless them):

1. Admin-key fail-closed (`app/security.py`) — try to authenticate with the placeholder/empty key, whitespace/case variants, or a route not covered by `require_admin_key`.
2. `/score/history` date validation (`app/routers/score.py`) — find any query input that still returns HTTP 500.
3. Numbers-only prompt invariant — find any path where external free-text reaches the model prompt; check whether the SMS prompt (`sms_report.py`) is a hole the `judgment.PROMPT_TEMPLATE` test misses.
4. LPPLS import-order fix (`app/indicators/d4_lppls.py`) — find another data-shortfall path that still hard-imports `lppls`; check `compute_confidence_isolated`.
5. CI gate (`.github/workflows/ci.yml`) — find a remaining soft-fail on a blocking step; check the secret-scan and install completeness.

## Result

_Appended on verifier completion (run launched in this engagement; its verdict is advisory per the mandate)._
