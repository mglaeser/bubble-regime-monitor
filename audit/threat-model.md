# Threat model — bubblegauge (produced by the audit, finding C-02)

STRIDE per trust boundary, for the architecture as built (`audit/00-system-map.md`). This did not exist before the engagement.

## Data-flow / trust boundaries

```
[Public Internet] --HTTPS--> [Nginx Proxy Manager] --> [uvicorn/FastAPI (rootless Podman)]
                                                          |  reads: SQLite /data/bubble.db
                                                          |  writes (admin, X-API-Key): recompute, send-SMS
        [Operator] --X-API-Key--> admin endpoints         |
                                                          v
   outbound HTTPS (app code, fixed hosts): FRED, Tiingo, TwelveData, AlphaVantage, Polygon,
        SSGA, SEC, FINRA, CBOE, multpl/GuruFocus, sipgate(SMS), Anthropic(inference: numbers only)
```

Trust boundaries: **(B1)** Internet→proxy→app (public reads); **(B2)** operator→admin (keyed writes); **(B3)** app→external data providers (egress); **(B4)** app→Anthropic (model, numbers-only prompt); **(B5)** app→sipgate (SMS to the operator's own number); **(B6)** developer→GitHub→CI→host (supply/delivery).

## STRIDE

| Boundary | Threat | Analysis | Control / finding |
|---|---|---|---|
| B1 | **Spoofing** | No user identity to spoof (public reads, no accounts). | N/A |
| B1 | **Tampering** | Malformed query → `datetime.fromisoformat` → 500. | **A-25** (fix: validate date params). |
| B1 | **DoS** | Public reads could be flooded. | slowapi 60/min/IP rate limit (`security.py`); reads serve a cached snapshot (no per-request model call). Adequate; proxy adds a layer. |
| B1 | **Info disclosure** | Reads expose only public research data + disclaimers; no secrets in responses. | PASS (C-24). |
| B2 | **Spoofing/EoP** | Guess/replay the admin key to trigger recompute/SMS. | Constant-time compare (`security.py:20`); **weak default key** if unset (**B-06/C-01**, fix: fail-closed). |
| B2 | **Repudiation** | Admin actions logged (structlog); git history for code. No immutable audit trail. | **B-07/C-37** (partial). |
| B3 | **Tampering (data poisoning)** | A compromised/spoofed upstream feed could shift an indicator number. | Multi-provider fallback + source-health/provenance notes + science-audit surfacing; **not** an instruction-injection vector (numbers only). Residual: data integrity, mitigated by cross-source sanity + contested flags. |
| B3 | **Info disclosure** | API keys sent to providers; egress not allowlisted at platform. | **A-11/B-22** (add egress allowlist). Keys are per-provider; rotate (**B-06**). |
| B4 | **Prompt injection / exfiltration** | Could untrusted content hijack the model or exfiltrate data? | **No** — prompt is numbers/enums only; model has no tools/outbound channel (**A-10/C-07/C-08/B-20 PASS/N-A**). The one residual: a poisoned upstream number perturbs the note's content, not its capabilities. |
| B4 | **Info disclosure (to provider)** | Prompt/data sent to Anthropic. | Numbers only, no PII/secrets → nil exposure (**C-34**). Document provider data-use position. |
| B5 | **Tampering/DoS** | SMS to a fixed operator number; irreversible (can't unsend). | Admin+schedule gated, ASCII-coerced, ≤160 chars, single recipient; low blast radius (**A-11/A-34**). |
| B6 | **Supply chain** | Hallucinated/typo-squatted or unpinned dependency reaches the build. | Verified no hallucinated pkg today, but **no lockfile/existence gate** (**B-04/C-03/A-08**). |
| B6 | **Tampering (the gate)** | Author identity can edit `ci.yml`; CI is red + non-blocking; ships on red. | **A-01/A-39/B-01/B-35** — the load-bearing failure. |

## Staleness control (fix for C-02)

Add a CI check that **fails when a new outbound host, a new router, or a new secret name appears without a matching threat-model row** — the machine substitute for the architect who would have noticed the new integration in review.
