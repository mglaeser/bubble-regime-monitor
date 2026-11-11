"""Judgment-call generator: hosted LLM gateway with graceful degradation.

After each recompute, produce a <=300-character "judgment call" written for a
COMPLETE NON-EXPERT (no finance background): plain everyday language, no jargon,
naming in plain words the single biggest thing making it look more bubble-like
and the single biggest calming factor. Constraints in the prompt: NO probability
language, NO investment advice, NO price targets — an observation, not advice.

The gateway route is entirely operator-configured. The application sends one
streaming Responses request to that exact route (once more after a transient
failure, app/llm_gateway.py) and never substitutes a model; any provider/model
fallback is gateway-controlled and opaque to this service.

Degradation: on any API error/timeout, persist the last successful text with
stale:true and continue — never block the recompute or return a 500.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.config import get_settings
from app.logging_conf import get_logger
from app.redaction import sanitize

log = get_logger(__name__)

# THE GAUGES ARE NAMED IN THE TEMPLATE'S OWN WORDS. Until 2026-11 the model was handed
# {'s1': 0.97, ...} with no word on what a code measures, and asked for "the ONE
# biggest thing" in plain words. From 2026-10-16 22:00 UTC to 2026-11-10, 50 of
# production's 145 judgments computed with no warning flag active said so instead of
# answering, 21 of them as a question back ("What does each reading measure? I need
# those meanings to name the biggest bubble warning ... without guessing."); 0 of the 6
# computed with a flag active did (a flag's key names its measure). The text is served
# by /api/v1/score and is the digest's one free-text fact: the digest's model was
# handed such a note on 14 of those 25 mornings.
# The names are template text, not a field: the interpolated fields are the same twelve
# numbers and enums (AGENTS.md ground rule 1,
# tests/test_audit_v331.py::TestPromptIsNumbersOnly). One line per indicator of the
# registry's two blocks, the plain-word form of what app/references.py says it measures
# (s5 is read two years back; d2's "rollover" is the borrowing falling from its peak;
# d3's gate caps the reading); tests/test_judgment_prompt.py holds that each has its line.
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
Each gauge below reads from 0 to 1; the higher, the more bubble-like. None means no reading today.
"How deep a fall could be" gauges: {s_scores}.
  s1 = how pricey shares are compared with their own history and with interest rates
  s2 = how much of the market's value sits in its ten biggest companies
  s3 = how far chip makers' shares have outrun the rest of the market over two years
  s4 = a statistical test for tech and chip share prices climbing at an ever faster, runaway pace (prone to false alarms)
  s5 = how little extra interest lenders asked of risky companies about two years ago (very relaxed lending then reads high)
"Is a turn near" gauges: {d_scores}.
  d1 = how few companies are still climbing (many climbing together reads low)
  d2 = people borrowing money to buy shares: how fast that borrowing has grown in a year, and whether it has begun to fall from its peak
  d3 = how much of their incoming cash the biggest cloud companies spend on building out (kept low unless that spending has stopped turning into sales growth and spare cash)
  d4 = a pattern test for tech share prices speeding up toward a breaking point (prone to early and false alarms)
Fear-gauge multiplier: {v} (1.0 = calm; above 1.0 = near-term fear has risen close to or above longer-term fear, which scales the combined "turn near" reading up).
Warning flags tripped: {red_flag_detail}. Emergency override tripped: {override}.
Long-term trend (still in the market?): SPY {spy_state}, QQQ {qqq_state}.
Fast fear signals: {fast_alarm}.
"""


#: The keys the prompt's structures carry: the gauges the template names, the four
#: warning flags (aggregate.RedFlags.as_dict) and the fast alarm's readings
#: (legs.FastAlarm.as_dict, whose term structure is a word and is typed as one).
GAUGES_S = ("s1", "s2", "s3", "s4", "s5")
GAUGES_D = ("d1", "d2", "d3", "d4")
FLAGS = ("gsadf_explosive_noncontested", "semi_runup_ge_150pp", "hy_oas_widen_gt_100bps",
         "breadth_lt_50_near_ath")
FAST_SIGNALS = ("vrp", "vrp_flag", "vrp_sane", "skew")


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
    log.info("completion_ok", model=settings.llm_model)
    return result.text


def generate(median: float, iqr: tuple[float, float], band: str,
             s_scores: dict[str, float], d_scores: dict[str, float], v: float,
             red_flag_detail: dict[str, bool], override: bool,
             spy_state: str, qqq_state: str, fast_alarm: dict[str, object],
             last_successful: str | None = None) -> JudgmentCall:
    """Call the gateway; degrade to the last successful text on failure.

    EVERY VALUE IS TYPED HERE, whatever the caller hands over (AGENTS.md ground
    rule 1): a number, a truth value or one of the monitor's own words for that
    fact, by the rule the digest's facts pass (composer.typed, owner decision
    D7), each under a key this module names. A text, a structure or an unknown
    key reads None; the headline's three numbers are rounded, which refuses a
    text. Until 2026-11 the values were formatted as handed over, and only the
    template's field names were checked: a text as the band, or a note under a
    new key of the fast alarm (an open dict the API serves too), reached the
    model. Production's 615 snapshots hold only values that pass - four bands,
    IN/OUT/unknown, three term structures, numbers, truth values and None - so
    their prompts read as before, but for the fast alarm's two fixed labels
    (vrp_units, skew_label): they are text and no reading, and are not handed
    on. A term structure compute could not read ("unknown", in none of the 615)
    reads None, as a gauge without a reading does.
    """
    # imported here, as run_completion imports the gateway: the engine does not
    # load the message engine at import time
    from app.message_engine.composer import typed

    def typed_under(values: Mapping[str, object], keys: tuple[str, ...]) -> dict[str, object]:
        return {key: typed(key, values.get(key)) for key in keys}

    prompt = PROMPT_TEMPLATE.format(
        median=round(median), iqr_lo=round(iqr[0]), iqr_hi=round(iqr[1]),
        band=typed("action_band", band),
        s_scores=typed_under(s_scores, GAUGES_S), d_scores=typed_under(d_scores, GAUGES_D),
        v=typed("v", v),
        red_flag_detail=typed_under(red_flag_detail, FLAGS), override=typed("override", override),
        spy_state=typed("spy_trend", spy_state), qqq_state=typed("qqq_trend", qqq_state),
        fast_alarm={"term_structure": typed("vol_state", fast_alarm.get("term_structure")),
                    **typed_under(fast_alarm, FAST_SIGNALS)},
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
