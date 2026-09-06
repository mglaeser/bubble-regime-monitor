"""Message engine — the LLM WRITES the operator's message (Phase C).

Distinct from `app.alerts.llm_selector`, where the model only selects codes
and the renderer interpolates every fact. Both exist on purpose: ruling Q41
leaves the selector untouched, and ruling Q25 directs that the no-LLM claim
be consciously amended rather than quietly contradicted. The reasoning, the
compose-before-queue decision and the P1 exemption are in
docs/MESSAGE_ENGINE.md.
"""
