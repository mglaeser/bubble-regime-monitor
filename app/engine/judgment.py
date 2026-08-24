"""Judgment-call generator: hosted LLM gateway with graceful degradation.

After each recompute, produce a <=300-character "judgment call" written for a
COMPLETE NON-EXPERT (no finance background): plain everyday language, no jargon,
naming in plain words the single biggest thing making it look more bubble-like
and the single biggest calming factor. Constraints in the prompt: NO probability
language, NO investment advice, NO price targets — an observation, not advice.

The gateway route is entirely operator-configured. The application sends one
streaming Responses request to that exact route and never substitutes a model;
any provider/model fallback is gateway-controlled and opaque to this service.

Degradation: on any API error/timeout, persist the last successful text with
stale:true and continue — never block the recompute or return a 500.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.logging_conf import get_logger
from app.redaction import sanitize

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


def _clean_completion(text: str, limit: int = 300) -> str:
    """Trim to <= limit chars on a SENTENCE boundary and guarantee terminal
    punctuation, so the stored judgment never ends mid-word (the old text[:300]
    hard cut produced fragments like '...leaving the band at')."""
    text = " ".join(text.split()).strip()
    if len(text) <= limit and text[-1:] in ".!?":
        return text
    clipped = text[:limit]
    for end in (". ", "! ", "? ", ".", "!", "?"):
        idx = clipped.rfind(end)
        if idx >= 40:  # keep a meaningful sentence, not a stub
            return clipped[: idx + 1].strip()
    cut = clipped.rsplit(" ", 1)[0].rstrip(",;:- ")  # no boundary: last whole word + period
    return (cut + ".") if cut else clipped


def run_completion(prompt: str) -> str:
    """Run the one configured gateway route; return raw text or raise safely.

    Shared by the judgment call and the daily digest. There is deliberately no
    application-side model fallback list: the configured gateway route owns
    provider failover, while callers retain their deterministic/stale fallback.
    """
    from app.llm_gateway import complete

    settings = get_settings()
    result = complete(user=prompt, max_tokens=settings.llm_max_tokens, settings=settings)
    log.info("completion_ok", model=settings.llm_model, wire=result.wire)
    return result.text


def generate(median: float, iqr: tuple[float, float], band: str,
             s_scores: dict[str, float], d_scores: dict[str, float], v: float,
             red_flag_detail: dict[str, bool], override: bool,
             spy_state: str, qqq_state: str, fast_alarm: dict[str, object],
             last_successful: str | None = None) -> JudgmentCall:
    """Call the gateway; degrade to the last successful text on failure."""
    prompt = PROMPT_TEMPLATE.format(
        median=round(median), iqr_lo=round(iqr[0]), iqr_hi=round(iqr[1]), band=band,
        s_scores=s_scores, d_scores=d_scores, v=v,
        red_flag_detail=red_flag_detail, override=override,
        spy_state=spy_state, qqq_state=qqq_state, fast_alarm=fast_alarm,
    )
    try:
        text = run_completion(prompt)
        return JudgmentCall(text=_clean_completion(text, 300), stale=False)
    except Exception as exc:
        log.warning("judgment_call_degraded", error_class=type(exc).__name__,
                    error=sanitize(exc, limit=300))
        if last_successful:
            return JudgmentCall(text=last_successful, stale=True, error_class=type(exc).__name__)
        # No prior judgment: text is null (machine-detectable), not a placeholder.
        return JudgmentCall(text=None, stale=True, error_class=type(exc).__name__)
