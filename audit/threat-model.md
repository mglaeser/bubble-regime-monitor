# Threat model — bubblegauge (produced by the audit, finding C-02)

STRIDE per trust boundary, for the architecture as built (`audit/00-system-map.md`). This did not exist before the engagement.

## Data-flow / trust boundaries

```
[Public Internet] --HTTPS--> [Nginx Proxy Manager] --> [uvicorn/FastAPI (rootless Podman)]
                                                          |  reads: SQLite /data/bubble.db
                                                          |  writes (admin, X-API-Key): recompute, send-SMS
        [Operator] --X-API-Key--> admin endpoints         |
                                                          v
   outbound HTTPS (app code, hosts fixed in code): FRED, Tiingo, TwelveData, AlphaVantage, Polygon,
        SSGA, SEC, FINRA, CBOE, multpl/GuruFocus, sipgate(SMS), Anthropic(inference: numbers only)
   outbound HTTPS (host from CONFIG, not code): imessage-proxy (POST /api/messages, Bearer key)
```

Trust boundaries: **(B1)** Internet→proxy→app (public reads); **(B2)** operator→admin (keyed writes); **(B3)** app→external data providers (egress); **(B4)** app→Anthropic (model, numbers-only prompt); **(B5)** app→sipgate (SMS to the operator's own number); **(B6)** developer→GitHub→CI→host (supply/delivery); **(B7)** app→imessage-proxy (iMessage to the operator's own handle; Bearer `messages:send` key, destination host supplied by CONFIG rather than fixed in code). B5 and B7 are alternatives, never both: exactly one transport carries the digest per run and there is no fallback.

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
| B5 | **Tampering/DoS** | SMS to a fixed operator number; irreversible (can't unsend). | Admin+schedule gated, ASCII-coerced, ≤160 chars, single recipient; low blast radius (**A-11/A-34**). Fires only when the transport selector picks sipgate. |
| B7 | **Info disclosure** | The Bearer key and the digest body go to whatever `IMESSAGE_API_BASE_URL` names. The value is unvalidated (`_base_url()` strips a trailing slash and nothing else): an `http://` typo sends both in cleartext, a wrong host sends both to a stranger. | **GAP — no control.** Validate scheme and reject a path at the boundary; container egress allowlist (**A-11/B-22/C-02**). `sanitize()` keeps the key out of logs and admin responses (`imp_` pattern), which does nothing about the destination. |
| B7 | **Spoofing/EoP** | A stolen `messages:send` key sends arbitrary text as the operator. | Bounded upstream: the recipient allowlist is `admin`-scoped, so the key cannot add a destination, and this client pins `"service": "imessage"` and never sends `sender_identifier` (test-covered). Per the proxy's security doc the key is still "authority to spend money and to send unencrypted text" — it may select carrier SMS. Residual: no rotation, no vault (**B-06**). |
| B7 | **DoS / silent failure** | Proxy down, key expired (90d default), or the recipient absent from the proxy's allowlist → the digest stops. No fallback by design, and a `202` is acceptance by Messages.app, explicitly not delivery. | Structured skip/failure reasons + per-status operator hints (401/403/404/409/413/429/503). **GAP:** nothing alerts on repeated failure, so the failure mode is silence (**B-21/B-26**). |
| B7 | **Repudiation** | Which digests actually reached the phone? | `operation_id` from the 202 is logged and returned. It records acceptance only; delivery is unobservable from here, and the proxy's audit store holds metadata only — no bodies, no recipients. |
| B6 | **Supply chain** | Hallucinated/typo-squatted or unpinned dependency reaches the build. | Verified no hallucinated pkg today, but **no lockfile/existence gate** (**B-04/C-03/A-08**). |
| B6 | **Tampering (the gate)** | Author identity can edit `ci.yml`; CI is red + non-blocking; ships on red. | **A-01/A-39/B-01/B-35** — the load-bearing failure. |

## Staleness control (fix for C-02)

Add a CI check that **fails when a new outbound host, a new router, or a new secret name appears without a matching threat-model row** — the machine substitute for the architect who would have noticed the new integration in review.

**This check has now been missed once, measurably.** The daily-digest iMessage transport added an outbound host (`IMESSAGE_API_BASE_URL` → `POST /api/messages`), a secret name (`IMESSAGE_API_KEY`) and boundary B7 in a single change, and every gate stayed green: 847 tests passing, `ruff` clean, mypy at its pinned ceiling of 217, `detect-secrets-hook` exit 0 with a byte-identical baseline. This document was updated afterwards, by hand, because someone went looking — which is precisely the human step the check exists to replace. Until it is built, boundary coverage is a discipline obligation and should be described as one everywhere it is relied on.
