"""The reviewed phrase set: the only text that may ever reach a phone.

The model selects CODES. This module owns what each code says, which numeric
slots it is allowed to carry, and whether the result can physically fit in one
SMS. Nothing assembles a sentence that is not made of fragments from here.

Fitting is handled by OMITTING whole reviewed fragments in a defined priority
order — never by cutting one in half. A required caveat is never omitted: if
the message cannot fit with its caveats, the render falls back to the minimal
template, and if even that does not fit the render FAILS. Sending a
data-quality alert with its data-quality caveat trimmed off would be worse than
sending nothing.

Three things are checked at validation time, not at send time:

  1. every fragment is representable in GSM-7 (no silent UCS-2 downgrade);
  2. the MINIMAL assembly fits at maximum slot widths — the guarantee that
     something always fits;
  3. the FULL assembly fits at maximum slot widths — the design target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.alerts.canonical import canonical_json, sha256_hex, sha256_of
from app.alerts.errors import PhraseSetInvalid
from app.alerts.gsm7 import SINGLE_SMS_SEPTETS, first_non_gsm7, septets

PHRASE_VALIDATOR_VERSION = "1"

_SLOT_RE = re.compile(r"\{([A-Z0-9_]+)\}")

#: Assembly order. A message is HEADLINE, then phrases, then facts already
#: interpolated into those, then the next-check hint, then caveats.
JOIN = " "


@dataclass(frozen=True)
class FactSpec:
    """One number the renderer may put into a message.

    `max_width` is what the worst-case fit test uses. It is a REVIEWED bound,
    not a measurement of today's value: a band edge that fits at 52.6 must
    still fit at -100.0.
    """

    fact_id: str
    label: str
    unit: str
    max_width: int
    description: str


@dataclass(frozen=True)
class FragmentSpec:
    """One reviewed text fragment."""

    code: str
    text: str
    slots: tuple[str, ...]
    kind: str                      # headline | phrase | next_check | caveat
    priority: int = 100            # lower is dropped LAST when fitting


@dataclass(frozen=True)
class ValidatedPhraseSet:
    version: str
    sha256: str
    canonical_json: str
    worst_case_test_sha256: str
    facts: dict[str, FactSpec]
    headlines: dict[str, FragmentSpec]
    phrases: dict[str, FragmentSpec]
    next_checks: dict[str, FragmentSpec]
    caveats: dict[str, FragmentSpec]
    worst_case: dict[str, int]

    def fragment(self, code: str) -> FragmentSpec | None:
        for table in (self.headlines, self.phrases, self.next_checks, self.caveats):
            if code in table:
                return table[code]
        return None

    def all_codes(self) -> set[str]:
        return set(self.headlines) | set(self.phrases) | set(self.next_checks) | set(self.caveats)


def _slots_of(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_SLOT_RE.findall(text)))


def _load_fragments(
    raw: dict[str, Any], kind: str, facts: dict[str, FactSpec], problems: list[str]
) -> dict[str, FragmentSpec]:
    out: dict[str, FragmentSpec] = {}
    for code, entry in sorted(raw.items()):
        if not isinstance(entry, dict) or "text" not in entry:
            problems.append(f"{kind} {code!r}: missing 'text'")
            continue
        text = entry["text"]
        offender = first_non_gsm7(text)
        if offender is not None:
            problems.append(
                f"{kind} {code!r}: character {offender[0]!r} is not GSM-7 — the message would "
                "become UCS-2 and no longer fit one SMS"
            )
            continue
        slots = _slots_of(text)
        unknown = [s for s in slots if s not in facts]
        if unknown:
            problems.append(f"{kind} {code!r}: references undeclared facts {unknown}")
            continue
        declared = tuple(entry.get("slots", slots))
        if set(declared) != set(slots):
            problems.append(
                f"{kind} {code!r}: declares slots {sorted(declared)} but its text uses "
                f"{sorted(slots)}"
            )
            continue
        out[code] = FragmentSpec(
            code=code, text=text, slots=slots, kind=kind,
            priority=int(entry.get("priority", 100)),
        )
    return out


def _widest(fragment: FragmentSpec, facts: dict[str, FactSpec]) -> int:
    """Septets of a fragment with every slot at its reviewed maximum width."""
    filled = fragment.text
    for slot in fragment.slots:
        filled = filled.replace("{" + slot + "}", "W" * facts[slot].max_width)
    return septets(filled)


def validate_phrase_set(raw_json: str) -> ValidatedPhraseSet:
    """Parse, check and hash a phrase set. Raises `PhraseSetInvalid`."""
    import json

    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise PhraseSetInvalid(f"phrase set is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PhraseSetInvalid("phrase set must be a JSON object")

    problems: list[str] = []
    meta = raw.get("meta") or {}
    version = meta.get("phrase_set_version")
    if not version:
        raise PhraseSetInvalid("phrase set has no meta.phrase_set_version")

    facts: dict[str, FactSpec] = {}
    for fact_id, entry in sorted((raw.get("facts") or {}).items()):
        if not fact_id.startswith("F_"):
            problems.append(f"fact {fact_id!r} must start with 'F_'")
            continue
        try:
            facts[fact_id] = FactSpec(
                fact_id=fact_id,
                label=entry["label"],
                unit=entry.get("unit", ""),
                max_width=int(entry["max_width"]),
                description=entry.get("description", ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"fact {fact_id!r} is malformed: {exc}")

    # `score` as a fact would be exactly the ambiguity the mandate forbids.
    for banned in ("F_SCORE", "F_BAND"):
        if banned in facts:
            problems.append(
                f"fact {banned!r} is forbidden — name the median and the point score separately"
            )

    headlines = _load_fragments(raw.get("headlines") or {}, "headline", facts, problems)
    phrases = _load_fragments(raw.get("phrases") or {}, "phrase", facts, problems)
    next_checks = _load_fragments(raw.get("next_check") or {}, "next_check", facts, problems)
    caveats = _load_fragments(raw.get("caveats") or {}, "caveat", facts, problems)

    if not headlines:
        problems.append("phrase set declares no headlines")
    if not caveats:
        problems.append("phrase set declares no caveats")

    if problems:
        raise PhraseSetInvalid("; ".join(problems))

    # --- worst-case fit ----------------------------------------------------
    widest_headline = max(_widest(f, facts) for f in headlines.values())
    widest_phrase = max((_widest(f, facts) for f in phrases.values()), default=0)
    widest_next = max((_widest(f, facts) for f in next_checks.values()), default=0)
    widest_caveat = max(_widest(f, facts) for f in caveats.values())
    sep = septets(JOIN)

    # MINIMAL: headline + the worst caveat. This is the floor that must always
    # fit; if it does not, no message from this set can be trusted to send.
    minimal = widest_headline + sep + widest_caveat
    # FULL: headline + one phrase + next-check + one caveat.
    full = widest_headline + sep + widest_phrase + sep + widest_next + sep + widest_caveat

    if minimal > SINGLE_SMS_SEPTETS:
        problems.append(
            f"minimal worst-case assembly is {minimal} septets, over {SINGLE_SMS_SEPTETS}: "
            "shorten the longest headline or caveat"
        )
    if full > SINGLE_SMS_SEPTETS:
        problems.append(
            f"full worst-case assembly is {full} septets, over {SINGLE_SMS_SEPTETS}: "
            "shorten a fragment rather than relying on runtime omission"
        )
    if problems:
        raise PhraseSetInvalid("; ".join(problems))

    worst_case = {
        "widest_headline": widest_headline,
        "widest_phrase": widest_phrase,
        "widest_next_check": widest_next,
        "widest_caveat": widest_caveat,
        "minimal_assembly": minimal,
        "full_assembly": full,
        "limit": SINGLE_SMS_SEPTETS,
    }
    canonical = canonical_json(raw)
    return ValidatedPhraseSet(
        version=str(version),
        sha256=sha256_hex(canonical),
        canonical_json=canonical,
        worst_case_test_sha256=sha256_of(worst_case),
        facts=facts,
        headlines=headlines,
        phrases=phrases,
        next_checks=next_checks,
        caveats=caveats,
        worst_case=worst_case,
    )
