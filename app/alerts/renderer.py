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
from typing import Any

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
    represented_member_ids: list[str] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)


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
    if not context.members:
        raise RenderRejected("a market render requires at least one member")
    primary = context.primary
    if primary.headline_code is not None and primary.headline_code != headline_code:
        raise RenderRejected(
            f"headline {headline_code!r} does not match the origin contract for "
            f"{primary.rule_id}")
    headline = _lookup(phrase_set, headline_code, "headline")
    if headline_code not in primary.authorized_phrase_codes:
        raise RenderRejected(
            f"headline {headline_code!r} is not authorized for {primary.rule_id}")

    named: list[tuple[MemberContext, str, FragmentSpec, str]] = [
        (primary, headline_code, headline, _fill(headline, primary))
    ]
    for member in context.members[1:MAX_NAMED_MEMBERS]:
        code = member.headline_code
        if not code:
            raise RenderRejected(
                f"bundled member {member.rule_id} has no origin headline contract")
        fragment = _lookup(phrase_set, code, "headline")
        if code not in member.authorized_phrase_codes:
            raise RenderRejected(
                f"headline {code!r} is not authorized for bundled member "
                f"{member.rule_id}")
        named.append((member, code, fragment, _fill(fragment, member)))

    used_facts: list[str] = []
    used_codes: list[str] = []
    dropped: list[str] = []

    # Required caveats are collected PER MEMBER.  Generic identical fragments
    # are shown once; a future caveat containing member facts is retained once
    # per distinct filled text.  No member's fact is ever used to fill another
    # member's caveat.
    caveat_parts: list[tuple[MemberContext, str, FragmentSpec, str]] = []
    seen_caveats: set[tuple[str, str]] = set()
    for member in context.members:
        required = list(member.required_caveat_codes)
        if member is primary:
            required.extend(caveat_codes)
        for code in dict.fromkeys(required):
            if code not in member.authorized_phrase_codes:
                raise RenderRejected(
                    f"caveat {code!r} is not authorized for {member.rule_id}")
            fragment = _lookup(phrase_set, code, "caveat")
            filled = _fill(fragment, member)
            identity = (code, filled)
            if identity not in seen_caveats:
                caveat_parts.append((member, code, fragment, filled))
                seen_caveats.add(identity)

    # Prefer the documented number of named members, but never cut a member
    # clause or a required caveat to make it fit.  When full clauses cannot all
    # fit, the omitted members are represented by one exact dashboard count.
    chosen_named = min(MAX_NAMED_MEMBERS, len(context.members))
    summary_text: str | None = None
    summary_fragment: FragmentSpec | None = None
    for candidate in range(chosen_named, 0, -1):
        omitted = len(context.members) - candidate
        candidate_summary: str | None = None
        candidate_fragment: FragmentSpec | None = None
        if omitted:
            candidate_fragment = phrase_set.phrases.get("MORE_IN_DASHBOARD")
            if candidate_fragment is None:
                raise RenderRejected(
                    "bundle overflow has no reviewed MORE_IN_DASHBOARD fragment")
            candidate_summary = candidate_fragment.text.replace(
                "{F_MORE_COUNT}", str(omitted))
        mandatory = [item[3] for item in named[:candidate]]
        if candidate_summary:
            mandatory.append(candidate_summary)
        mandatory.extend(item[3] for item in caveat_parts)
        if septets(JOIN.join(mandatory)) <= SINGLE_SMS_SEPTETS:
            chosen_named = candidate
            summary_text = candidate_summary
            summary_fragment = candidate_fragment
            break

    prefix_parts = [item[3] for item in named[:chosen_named]]
    tail_parts = ([summary_text] if summary_text else []) + [
        item[3] for item in caveat_parts]
    for _member, code, fragment, _text in named[:chosen_named]:
        used_codes.append(code)
        used_facts.extend(fragment.slots)
    if summary_text and summary_fragment is not None:
        used_codes.append(summary_fragment.code)
        used_facts.extend(summary_fragment.slots)
    for _member, code, fragment, _text in caveat_parts:
        used_codes.append(code)
        used_facts.extend(fragment.slots)

    # Optional fragments, most important first.
    optional: list[tuple[int, str, str]] = []
    for code in phrase_codes:
        if code not in primary.authorized_phrase_codes:
            raise RenderRejected(
                f"phrase {code!r} is not authorized for {primary.rule_id}")
        fragment = _lookup(phrase_set, code, "phrase")
        optional.append((fragment.priority, code, _fill(fragment, primary)))
    if next_check_code:
        if next_check_code not in primary.authorized_phrase_codes:
            raise RenderRejected(
                f"next-check {next_check_code!r} is not authorized for "
                f"{primary.rule_id}")
        fragment = _lookup(phrase_set, next_check_code, "next_check")
        optional.append((5, next_check_code, _fill(fragment, primary)))

    optional.sort(key=lambda item: item[0])
    running = septets(JOIN.join([*prefix_parts, *tail_parts]))
    selected_optional: list[str] = []
    for _priority, code, text in optional:
        cost = septets(JOIN) + septets(text)
        if running + cost > SINGLE_SMS_SEPTETS:
            dropped.append(code)
            continue
        selected_optional.append(text)
        running += cost
        used_codes.append(code)
        selected_fragment = phrase_set.fragment(code)
        if selected_fragment is not None:
            used_facts.extend(selected_fragment.slots)

    body = JOIN.join([*prefix_parts, *selected_optional, *tail_parts])
    represented = [member.episode_id for member in context.members]
    named_ids = [item[0].episode_id for item in named[:chosen_named]]
    return _validate(
        body, used_codes, used_facts, dropped, render_source, fallback_reason,
        represented_member_ids=represented,
        named_member_ids=named_ids,
        overflow_count=len(context.members) - chosen_named,
    )


def _validate(body: str, codes: list[str], facts: list[str], dropped: list[str],
              render_source: str, fallback_reason: str | None, *,
              represented_member_ids: list[str], named_member_ids: list[str],
              overflow_count: int) -> RenderResult:
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
        represented_member_ids=list(represented_member_ids),
        validation={
            "gsm7": True,
            "within_limit": True,
            "honesty_lint": True,
            "codes_authorized": True,
            "facts_authorized": True,
            "all_members_represented": True,
            "represented_member_ids": list(represented_member_ids),
            "named_member_ids": list(named_member_ids),
            "overflow_count": overflow_count,
        },
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
