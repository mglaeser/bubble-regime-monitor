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
    text: str | None       # None when no judgment was ever generated (machine-detectable)
    stale: bool
    error_class: str | None = None  # exception class name of the last failure, if any


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
    except Exception as exc:  # SDK absent
        log.warning("judgment_call_degraded", error_class=type(exc).__name__, error=repr(exc)[:200])
        if last_successful:
            return JudgmentCall(text=last_successful, stale=True, error_class=type(exc).__name__)
        return JudgmentCall(text=None, stale=True, error_class=type(exc).__name__)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    # Model fallback chain (spec 5): primary -> sonnet-5 -> sonnet-4-6, all
    # with adaptive thinking + effort=max; then one plain retry with neither.
    # The minimal request NEVER sends budget_tokens or temperature (both HTTP
    # 400 with adaptive thinking / this effort config).
    models = [settings.anthropic_model, "claude-sonnet-5", "claude-sonnet-4-6"]
    last_exc: Exception | None = None

    def _call(model: str, thinking: bool) -> str:
        base: dict[str, object] = {
            "model": model,
            "max_tokens": settings.anthropic_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if thinking:
            try:
                resp = client.messages.create(
                    **base, thinking={"type": "adaptive"},
                    output_config={"effort": settings.anthropic_effort})
            except TypeError:
                # SDK predates the kwargs: pass them straight through.
                resp = client.messages.create(
                    **base, extra_body={"thinking": {"type": "adaptive"},
                                        "output_config": {"effort": settings.anthropic_effort}})
        else:
            resp = client.messages.create(**base)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise ValueError("empty completion")
        return text

    for model in models:
        try:
            text = _call(model, thinking=True)
            log.info("judgment_call_ok", model=model, shape="adaptive+effort")
            return JudgmentCall(text=text[:300], stale=False)
        except Exception as exc:
            last_exc = exc
            log.warning("judgment_model_failed", model=model, error_class=type(exc).__name__,
                        error=repr(exc)[:300])

    # Tertiary: plain request, no thinking/output_config, on the primary model.
    try:
        text = _call(settings.anthropic_model, thinking=False)
        log.info("judgment_call_ok", model=settings.anthropic_model, shape="plain")
        return JudgmentCall(text=text[:300], stale=False)
    except Exception as exc:
        last_exc = exc
        log.warning("judgment_call_degraded", error_class=type(exc).__name__, error=repr(exc)[:300])

    err_class = type(last_exc).__name__ if last_exc else "Unknown"
    if last_successful:
        return JudgmentCall(text=last_successful, stale=True, error_class=err_class)
    # No prior judgment: text is null (machine-detectable), not a placeholder.
    return JudgmentCall(text=None, stale=True, error_class=err_class)
