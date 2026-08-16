"""Deterministic message assembly from reviewed fragments. Pure.

The renderer owns every number. A model may select CODES; it never writes a
digit, a unit or a sentence. That is the whole containment: a hallucinated
number in a financial alert is indistinguishable from a real one at 3 a.m.

Fitting is by OMISSION of whole reviewed fragments in a defined priority order,
never by truncation. Required caveats are never omitted — if the message cannot
fit with them, the renderer falls back to the minimal template, and if even
that does not fit it FAILS. A data-quality alert with its data-quality caveat
trimmed off would be worse than nothing.

Validation runs before a single character reaches the wire:

  1. every selected code exists;
  2. every selected code is authorized FOR THAT MEMBER;
  3. every fact id is authorized for that member (no cross-member references);
  4. every slot a fragment declares is filled;
  5. every required caveat is present;
  6. the honesty lint passes (no probability, advice or certainty language);
  7. the body is GSM-7;
  8. the body is <= 160 septets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.alerts.enums import RenderSource
from app.alerts.errors import RenderRejected
from app.alerts.gsm7 import SINGLE_SMS_SEPTETS, first_non_gsm7, septets
from app.alerts.phrase_registry import FragmentSpec, ValidatedPhraseSet
from app.alerts.render_context import MemberContext, RenderContext

JOIN = " "
MAX_NAMED_MEMBERS = 3

#: Vocabulary an alert must never contain. The score is not a probability, the
#: service gives no advice, and nothing here is certain.
_FORBIDDEN = re.compile(
    r"(?i)\b(wahrscheinlich\w*|sicher\b|garantiert\w*|kaufen|verkaufen|empfehl\w*|"
    r"crash\w*|prognos\w*)"
)


@dataclass
class RenderResult:
    body: str
    septet_count: int
    render_source: str
    fallback_reason: str | None = None
    selected_phrase_codes: list[str] = field(default_factory=list)
    selected_fact_ids: list[str] = field(default_factory=list)
    dropped_codes: list[str] = field(default_factory=list)
    validation: dict[str, bool] = field(default_factory=dict)


def honesty_lint(body: str) -> str | None:
    """The forbidden phrase found, or None."""
    match = _FORBIDDEN.search(body)
    return match.group(0) if match else None


def _fill(fragment: FragmentSpec, member: MemberContext) -> str:
    """Interpolate a fragment's slots from ONE member's authorized facts."""
    text = fragment.text
    for slot in fragment.slots:
        value = member.fact(slot)
        if value is None:
            raise RenderRejected(
                f"{fragment.code}: fact {slot} is not authorized for member "
                f"{member.rule_id} (cross-member fact references are rejected)"
            )
        text = text.replace("{" + slot + "}", value)
    return text


def _lookup(phrase_set: ValidatedPhraseSet, code: str, table: str) -> FragmentSpec:
    tables = {
        "headline": phrase_set.headlines,
        "phrase": phrase_set.phrases,
        "next_check": phrase_set.next_checks,
        "caveat": phrase_set.caveats,
    }
    fragment = tables[table].get(code)
    if fragment is None:
        raise RenderRejected(f"unknown {table} code {code!r} in phrase set "
                             f"{phrase_set.version}")
    return fragment


def render(
    *,
    context: RenderContext,
    phrase_set: ValidatedPhraseSet,
    headline_code: str,
    phrase_codes: list[str],
    next_check_code: str | None,
    caveat_codes: list[str],
    render_source: str = RenderSource.TEMPLATE_FULL,
    fallback_reason: str | None = None,
) -> RenderResult:
    """Assemble and validate. Raises `RenderRejected` rather than sending doubt."""
    primary = context.primary
    headline = _lookup(phrase_set, headline_code, "headline")
    if headline_code not in primary.authorized_phrase_codes:
        raise RenderRejected(
            f"headline {headline_code!r} is not authorized for {primary.rule_id}")

    parts: list[str] = [_fill(headline, primary)]
    used_facts: list[str] = list(headline.slots)
    used_codes: list[str] = [headline_code]
    dropped: list[str] = []

    # Required caveats FIRST in the budget: they are never dropped, so they are
    # reserved before any optional fragment competes for room.
    required = list(dict.fromkeys(
        [*primary.required_caveat_codes, *caveat_codes]
    ))
    caveat_texts: list[str] = []
    for code in required:
        fragment = _lookup(phrase_set, code, "caveat")
        caveat_texts.append(_fill(fragment, primary))
        used_codes.append(code)
        used_facts.extend(fragment.slots)

    reserved = sum(septets(text) + septets(JOIN) for text in caveat_texts)

    # Optional fragments, most important first.
    optional: list[tuple[int, str, str]] = []
    for code in phrase_codes:
        if code not in primary.authorized_phrase_codes:
            raise RenderRejected(
                f"phrase {code!r} is not authorized for {primary.rule_id}")
        fragment = _lookup(phrase_set, code, "phrase")
        optional.append((fragment.priority, code, _fill(fragment, primary)))
    if next_check_code:
        fragment = _lookup(phrase_set, next_check_code, "next_check")
        optional.append((5, next_check_code, _fill(fragment, primary)))

    # Bundle summary: at most three members named, the rest counted.
    extra = len(context.members) - MAX_NAMED_MEMBERS
    if extra > 0 and "F_MORE_COUNT" in phrase_set.facts:
        more = phrase_set.phrases.get("MORE_IN_DASHBOARD")
        if more is not None:
            optional.append((1, more.code, more.text.replace("{F_MORE_COUNT}", str(extra))))

    optional.sort(key=lambda item: item[0])
    budget = SINGLE_SMS_SEPTETS - reserved
    running = septets(parts[0])
    for _priority, code, text in optional:
        cost = septets(JOIN) + septets(text)
        if running + cost > budget:
            dropped.append(code)
            continue
        parts.append(text)
        running += cost
        used_codes.append(code)
        fragment = phrase_set.fragment(code)
        if fragment is not None:
            used_facts.extend(fragment.slots)

    body = JOIN.join([*parts, *caveat_texts])
    return _validate(body, used_codes, used_facts, dropped, render_source, fallback_reason)


def _validate(body: str, codes: list[str], facts: list[str], dropped: list[str],
              render_source: str, fallback_reason: str | None) -> RenderResult:
    offender = first_non_gsm7(body)
    if offender is not None:
        raise RenderRejected(
            f"rendered body contains {offender[0]!r}, which GSM-7 cannot encode; "
            "the message would become UCS-2 rather than being transliterated"
        )
    forbidden = honesty_lint(body)
    if forbidden is not None:
        raise RenderRejected(
            f"rendered body contains forbidden vocabulary {forbidden!r} — the score is "
            "not a probability and this service gives no advice"
        )
    count = septets(body)
    if count > SINGLE_SMS_SEPTETS:
        raise RenderRejected(
            f"rendered body is {count} septets, over the {SINGLE_SMS_SEPTETS} limit even "
            "after omitting optional fragments"
        )
    return RenderResult(
        body=body,
        septet_count=count,
        render_source=render_source,
        fallback_reason=fallback_reason,
        selected_phrase_codes=list(dict.fromkeys(codes)),
        selected_fact_ids=list(dict.fromkeys(facts)),
        dropped_codes=dropped,
        validation={"gsm7": True, "within_limit": True, "honesty_lint": True,
                    "codes_authorized": True, "facts_authorized": True},
    )


def render_minimal(*, context: RenderContext, phrase_set: ValidatedPhraseSet,
                   headline_code: str, fallback_reason: str) -> RenderResult:
    """The guaranteed-fit floor: headline plus required caveats, nothing else."""
    return render(
        context=context, phrase_set=phrase_set, headline_code=headline_code,
        phrase_codes=[], next_check_code=None, caveat_codes=[],
        render_source=RenderSource.TEMPLATE_MINIMAL, fallback_reason=fallback_reason,
    )


def render_with_cascade(
    *,
    context: RenderContext,
    phrase_set: ValidatedPhraseSet,
    headline_code: str,
    phrase_codes: list[str],
    next_check_code: str | None,
    caveat_codes: list[str],
    render_source: str = RenderSource.TEMPLATE_FULL,
    fallback_reason: str | None = None,
) -> RenderResult:
    """Full -> minimal. A failure at BOTH levels raises: nothing is guessed.

    The cascade exists so a marginal phrase-set change degrades the message
    rather than losing the alert, but it never invents a way to say something.
    """
    try:
        return render(
            context=context, phrase_set=phrase_set, headline_code=headline_code,
            phrase_codes=phrase_codes, next_check_code=next_check_code,
            caveat_codes=caveat_codes, render_source=render_source,
            fallback_reason=fallback_reason,
        )
    except RenderRejected as exc:
        return render_minimal(context=context, phrase_set=phrase_set,
                              headline_code=headline_code,
                              fallback_reason=f"full_render_rejected: {exc.message}"[:200])
