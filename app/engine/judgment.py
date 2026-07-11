"""Judgment-call generator: Anthropic Messages API with graceful degradation.

After each recompute, produce a <=300-character plain-English "judgment call"
naming the single dominant driver and the single biggest counter-signal.
Constraints in the prompt: NO probability language, NO investment advice, NO
price targets — presented as observation, not recommendation.

API notes (verified July 2026): model claude-opus-4-8 (released 28 May 2026);
thinking={"type": "adaptive"} + output_config={"effort": "max"};
budget_tokens / temperature return HTTP 400 on Opus 4.7+ — do NOT set them.

Degradation: on any API error/timeout, persist the last successful text with
stale:true and continue — never block the recompute or return a 500.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)

PROMPT_TEMPLATE = """You are annotating a research bubble-regime monitor. Given the readings below,
write ONE sentence (<=300 characters) that names the single dominant driver and
the single biggest counter-signal. Constraints: NO probability language, NO
investment advice, NO price targets. Present as observation, not recommendation.

Headline median: {median} (IQR {iqr_lo}-{iqr_hi}); action band: {band}.
Block S sub-scores: {s_scores}. Block D sub-scores: {d_scores}. V multiplier: {v}.
Red flags fired: {red_flag_detail}. Override fired: {override}.
Trend states (Faber): SPY {spy_state}, QQQ {qqq_state}. Fast alarm: {fast_alarm}.
"""


@dataclass
class JudgmentCall:
    text: str
    stale: bool


def generate(median: float, iqr: tuple[float, float], band: str,
             s_scores: dict[str, float], d_scores: dict[str, float], v: float,
             red_flag_detail: dict[str, bool], override: bool,
             spy_state: str, qqq_state: str, fast_alarm: dict[str, object],
             last_successful: str | None = None) -> JudgmentCall:
    """Call the Messages API; degrade to the last successful text on failure."""
    settings = get_settings()
    prompt = PROMPT_TEMPLATE.format(
        median=round(median), iqr_lo=round(iqr[0]), iqr_hi=round(iqr[1]), band=band,
        s_scores=s_scores, d_scores=d_scores, v=v,
        red_flag_detail=red_flag_detail, override=override,
        spy_state=spy_state, qqq_state=qqq_state, fast_alarm=fast_alarm,
    )
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model=settings.anthropic_model,  # alias 'opus'
            max_tokens=settings.anthropic_max_tokens,
            thinking={"type": "adaptive"},  # adaptive thinking (July 2026)
            output_config={"effort": settings.anthropic_effort},
            # NOTE: do NOT set budget_tokens or temperature — both return
            # HTTP 400 on Opus 4.7+
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise ValueError("empty completion")
        return JudgmentCall(text=text[:300], stale=False)
    except Exception as exc:
        log.warning("judgment_call_degraded", error=str(exc))
        if last_successful:
            return JudgmentCall(text=last_successful, stale=True)
        return JudgmentCall(text="(judgment call unavailable)", stale=True)
