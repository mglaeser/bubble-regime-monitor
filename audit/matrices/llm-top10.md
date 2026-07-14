# OWASP LLM Top-10 coverage matrix (finding C-05)

Every category mapped to a control **and** a test/justification. An empty cell would be a finding; a control with no test is a finding in disguise. Architecture: numbers-in / short-text-out, hosted API, **no tools, no RAG, no untrusted free-text input** (`audit/00`).

| # | Category | Applies here? | Control | Test / justification |
|---|---|---|---|---|
| LLM01 | Prompt injection | **Minimal** | Structural: prompt is numbers/enums only; model has no tools/actions. | `audit/03` C-07/A-10 PASS. Fix: add a regression test asserting no external free-text field enters `PROMPT_TEMPLATE`. |
| LLM02 | Sensitive-information disclosure | **Minimal** | No secrets/PII in the prompt (`judgment.py:139`); output is a public disclaimered note. | C-24 PASS (verified prompt fields). |
| LLM03 | Supply chain | **YES** | Dependency pinning + existence gate (**currently missing**). | **B-04/C-03 PARTIAL** — Wave-1 fix (lockfile + pip-audit + existence check). |
| LLM04 | Data & model poisoning | **N/A (model)** / data via feeds | No training/fine-tuning (C-21 N/A). Upstream data feeds could be poisoned → shifts a number, not the model. | Multi-source fallback + provenance + science audit. |
| LLM05 | Improper output handling | **YES (low)** | `_clean_completion` shape validation; disclaimer; degrade-to-template. **No factual grounding check.** | **C-38 PARTIAL** — add a deterministic sanity check that the note's direction/band matches the snapshot. |
| LLM06 | Excessive agency | **N/A** | Model has zero tools/permissions/autonomy. | C-06/C-12 N/A (repo-wide: no tool_use). |
| LLM07 | System-prompt leakage | **Minimal** | Prompt contains no secret/rule that is security-load-bearing (all controls are in code). | C-24 PASS. |
| LLM08 | Vector/embedding weaknesses | **N/A** | No embeddings/vector store. | C-32 N/A. |
| LLM09 | Misinformation | **YES (low)** | Disclaimers + "not a probability" framing + open methodology + science audit; **no groundedness gate** on the note. | **C-38 PARTIAL** — same fix as LLM05; single-user, disclaimered → low stakes. |
| LLM10 | Unbounded consumption | **Minimal** | `max_tokens=8000` per call; 2 scheduled calls/day; single-flight recompute lock. | B-08 PARTIAL — add a provider spend cap for defence in depth. |

**Residual live categories:** LLM03 (supply chain — Wave 1), LLM05/LLM09 (improper output / misinformation — Wave 3 groundedness check). Everything else is N/A-by-architecture or minimal, argued above.
