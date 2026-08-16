"""The alert system.

Alerting CONSUMES committed scoring outcomes. It never modifies a sub-score,
quality, coverage, renormalization, band edge, red-flag rule, override
decision, Monte Carlo distribution, headline, methodology version or
falsification clock — and it never re-implements one of those formulas, even
when the result would match.

Layering (enforced by tests, not convention):

  pure        enums, errors, canonical, dto, gsm7, observation, calendars,
              primitives, evaluators, dominance, state_machine, planner,
              budgets, quiet_hours, renderer
              -> no Session, no datetime.now(), no sipgate, no Anthropic

  impure      repository, input_builder, dispatcher, sender, llm_selector,
              watchdog, digest, recovery, health

Nothing here is wired into scoring. Nothing here sends anything until an
operator promotes artifacts and flips ALERTS_MODE by hand.
"""

from __future__ import annotations
