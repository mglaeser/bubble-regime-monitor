# audit/08 — Executive summary

**The single most important number first, because the mandate demands it and because it is the truest thing in this report: the existing verification pipeline caught roughly 1 of 6 seeded defects, and it was not even green — CI has been `failure` on every recent run while production shipped anyway. A green build in this repository was, until this engagement, evidence of nothing.**

## What is still broken (lead with this)

Three items are **STOP-SHIP and are NOT closed by this engagement** — they need an operator action or a second vendor that code cannot supply:

1. **Rotate the credentials (B-06).** Nine credentials — every provider key plus the sipgate SMS token — were disclosed in a development chat channel. The git repo is clean, but a secret shown to a third party is published. Nothing is rotated yet. Do this before serving production traffic. The tenth credential, the imessage-proxy key added with the iMessage digest transport, was never disclosed and is not on the rotation list — but it lives in the same un-vaulted `.env` and is the only one that **expires** on its own (90 days by default), so it needs a diary entry rather than a rotation.
2. **Enable branch protection (A-01 / B-35).** CI is now rebuilt to be blocking and green-capable, but nothing yet *enforces* it: an operator can still merge and deploy on red, and the identity that writes the code can edit the gate. Mark the `test` check required, with no bypass, owned by the human-in-command.
3. **Get a different-vendor verifier (A-39), and measure the tests (A-02).** The verification loop is self-referential — the same model family wrote the code and its tests — and the suite's mutation score is unmeasured, so its fault-detection power is unknown. Until `mutmut` is wired and a second-vendor adversary reviews high-stakes changes, "green" is not proof.

Until items 1 and 2 are done, **treat this system as not independently verified.**

## What this engagement actually changed (with evidence)

Test-first, small, atomic, each red→green (`audit/05`). Suite `161 passed / 1 errored` → **`176 passed`** (now hermetic); ruff (with security rules) clean; `pip-audit` clean. An independent adversarial pass then **broke four of the fixes** (a bypassable 500, a missed clone, a false-assurance test, a non-blocking secret-scan) — all four were reproduced, re-fixed and regression-tested. Two honesty corrections were folded in: the red CI is **infrastructural** (Actions never executes here — jobs die in 3s with no logs), so the rebuilt gate is verified **green locally only**; and the citations flagged "unverifiable" turned out **real**.

- **Fixed a public 500** — `/score/history?from=<garbage>` returned HTTP 500; now validated to 422 (A-25).
- **Closed the admin door** — the guessable placeholder admin key used to authenticate; it now fails closed (B-06/C-01).
- **Rebuilt the gate** — CI is now blocking on ruff+`S`, `pip-audit`, secret-scan and tests, with the deceptive `mypy || true` replaced by an honestly-labelled advisory step (A-01/A-08/A-13/B-01).
- **Made the suite hermetic and honest** — the LPPLS insufficient-data path no longer hard-imports the optional engine (A-02); enabling ruff `S` lifted the seeded-defect catch rate from ~1/6 toward ~3/6.
- **Hardened the edges** — masked the phone number in logs (C-23), annotated/blocked swallowed exceptions (A-26), non-root container (B-12), setuptools bumped to clear a CVE.

> **Post-engagement architecture change (daily digest over iMessage).** The daily digest gained a second transport — an imessage-proxy instance reached over HTTPS with a scoped bearer key — with the sipgate SMS path retained and exactly one of the two sending per run. This adds an outbound host, a tenth credential and a trust boundary that post-date everything above. C-04 was re-examined and **holds** (the recipient is the operator's own handle; data subject == controller is untouched); C-08 and B-20 were re-audited and hold. C-02 is the finding this change tests hardest: the staleness check it prescribed — flag a new outbound host, router or secret with no matching threat-model row — was never built, and the whole pipeline stayed green through a change that added all three. Details in `audit/00-system-map.md`, `audit/threat-model.md` (boundary B7) and `audit/06`.
- **Told the truth in the docs** — the three "unverifiable" citations were **independently confirmed real** during the audit (Chen et al. arXiv:2604.25826; Basele–Phillips–Shi Cowles CFDP 2430; BIS AER 2026); the stale flags are cleared (C-38). Added `SECURITY.md`, `AGENTS.md`, `LICENSE`, a `threat-model`, the OWASP-LLM matrix, and an AI-BOM.

## The honest architectural verdict

Most of the mandate's fearsome surface **does not exist here, and that is a real, evidence-backed reduction — not an evasion.** This is a single-tenant, self-hosted research tool whose only AI use is a **numbers-in / short-text-out hosted-API call with no tools, no RAG, no fine-tuning, and no untrusted free-text input.** That makes the agentic taxonomy (C-06/C-12/C-16–C-19), the injection/exfiltration runtime controls (B-20/C-07/C-08), the retrieval and vector-store checks (A-21/C-22/C-32/B-33), fine-tuning governance (C-21/C-35), and multi-tenant IDOR (C-01) **genuinely not-applicable** — each argued against the architecture in `audit/03`, not assumed. 31 of 119 checks resolve NOT-APPLICABLE on that basis; 8 PASS; 65 PARTIAL; **15 FAIL**.

The failures cluster on one root cause the mandate predicted exactly: **the operating model's safety rests entirely on an automated gate, and that gate was decorative** — red, non-blocking, security-blind, editable by its own author, with an unmeasured test suite behind it. That is now materially repaired in code; the last mile (rotate, enforce the gate, add a second-vendor verifier, measure the suite) is operator work, specified with compensating controls and tripwires in `audit/06`.

**Do not read this as "the system is now in good shape."** Read it as: the load-bearing wall was found to be decorative, it has been rebuilt, and it will not bear load until someone bolts it to the building (branch protection) and rotates the keys. The list of what remains is in `audit/06`; work it top-down.
