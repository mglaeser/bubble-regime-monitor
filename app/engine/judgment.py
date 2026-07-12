"""Judgment-call generator: Anthropic Messages API with graceful degradation.

After each recompute, produce a <=300-character "judgment call" written for a
COMPLETE NON-EXPERT (no finance background): plain everyday language, no jargon,
naming in plain words the single biggest thing making it look more bubble-like
and the single biggest calming factor. Constraints in the prompt: NO probability
language, NO investment advice, NO price targets — an observation, not advice.

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

PROMPT_TEMPLATE = """You are writing a one-line note for a research bubble-regime monitor, for a
COMPLETE NON-EXPERT with no finance background — write it so a smart 15-year-old
could follow it.

Rules:
- Plain, everyday language. NO jargon. If a technical measure matters, say it in
  plain words instead of its name — e.g. "how pricey shares are compared with
  their own history" not "CAPE"; "how many companies are climbing together, not
  just a few" not "breadth"; "people borrowing money to buy shares" not "margin
  debt"; "the market's fear gauge" not "VIX".
- Say, in plain terms, the ONE biggest thing making it look MORE bubble-like and
  the ONE biggest thing keeping it calmer.
- One or two short sentences, at most ~280 characters total.
- Avoid raw numbers/percentiles; if one is truly needed, explain it plainly.
- NO probability wording, NO investment advice, NO price targets, NO buy/sell.
  It is a neutral observation, never a recommendation.

Readings (context for you — do NOT quote these codes or labels back to the reader):
Headline: {median} out of 100 (rough range {iqr_lo}-{iqr_hi}); status: {band}.
"How deep a fall could be" gauges: {s_scores}.
"Is a turn near" gauges: {d_scores}. Fear-gauge multiplier: {v}.
Warning flags tripped: {red_flag_detail}. Emergency override tripped: {override}.
Long-term trend (still in the market?): SPY {spy_state}, QQQ {qqq_state}.
Fast fear signals: {fast_alarm}.
"""


@dataclass
class JudgmentCall:
    text: str | None       # None when no judgment was ever generated (machine-detectable)
    stale: bool
    error_class: str | None = None  # exception class name of the last failure, if any


def run_completion(prompt: str) -> str:
    """Run the Anthropic model-fallback chain; return raw text or RAISE.

    Chain (spec 5): primary -> sonnet-5 -> sonnet-4-6, all with adaptive
    thinking + effort=max; then one plain retry with neither. The minimal
    request NEVER sends budget_tokens or temperature (both HTTP 400 with
    adaptive thinking / this effort config). Shared by the judgment call and
    the daily SMS digest."""
    import anthropic  # raises ImportError if the SDK is absent

    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
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
            log.info("completion_ok", model=model, shape="adaptive+effort")
            return text
        except Exception as exc:
            last_exc = exc
            log.warning("completion_model_failed", model=model, error_class=type(exc).__name__,
                        error=repr(exc)[:300])

    # Tertiary: plain request, no thinking/output_config, on the primary model.
    # Any exception from this final attempt propagates to the caller (which
    # applies its own degradation), carrying the accumulated failure context.
    del last_exc
    text = _call(settings.anthropic_model, thinking=False)
    log.info("completion_ok", model=settings.anthropic_model, shape="plain")
    return text


def generate(median: float, iqr: tuple[float, float], band: str,
             s_scores: dict[str, float], d_scores: dict[str, float], v: float,
             red_flag_detail: dict[str, bool], override: bool,
             spy_state: str, qqq_state: str, fast_alarm: dict[str, object],
             last_successful: str | None = None) -> JudgmentCall:
    """Call the Messages API; degrade to the last successful text on failure."""
    prompt = PROMPT_TEMPLATE.format(
        median=round(median), iqr_lo=round(iqr[0]), iqr_hi=round(iqr[1]), band=band,
        s_scores=s_scores, d_scores=d_scores, v=v,
        red_flag_detail=red_flag_detail, override=override,
        spy_state=spy_state, qqq_state=qqq_state, fast_alarm=fast_alarm,
    )
    try:
        text = run_completion(prompt)
        return JudgmentCall(text=text[:300], stale=False)
    except Exception as exc:
        log.warning("judgment_call_degraded", error_class=type(exc).__name__, error=repr(exc)[:300])
        if last_successful:
            return JudgmentCall(text=last_successful, stale=True, error_class=type(exc).__name__)
        # No prior judgment: text is null (machine-detectable), not a placeholder.
        return JudgmentCall(text=None, stale=True, error_class=type(exc).__name__)
