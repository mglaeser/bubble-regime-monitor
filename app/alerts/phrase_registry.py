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
from dataclasses import dataclass, field
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
    #: In the set's ACTIVE language - the one the operator selected
    #: (MESSAGE_LANGUAGE) among those the set carries - so every reader of a
    #: fragment renders one language without knowing there are others.
    text: str
    slots: tuple[str, ...]
    kind: str                      # headline | phrase | next_check | caveat
    priority: int = 100            # lower is dropped LAST when fitting
    #: (language, text) for every language the set was reviewed in.
    texts: tuple[tuple[str, str], ...] = ()


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
    #: The language the fragments' `text` is in, and every language the set
    #: carries. The bytes (sha256, canonical_json) cover all of them: one
    #: promotion admits the whole reviewed set, and the operator picks the
    #: language by setting, not by re-promotion.
    language: str = "de"
    languages: tuple[str, ...] = ("de",)
    worst_case_by_language: dict[str, dict[str, int]] = field(default_factory=dict)

    def fragment(self, code: str) -> FragmentSpec | None:
        for table in (self.headlines, self.phrases, self.next_checks, self.caveats):
            if code in table:
                return table[code]
        return None

    def all_codes(self) -> set[str]:
        return set(self.headlines) | set(self.phrases) | set(self.next_checks) | set(self.caveats)


def _slots_of(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_SLOT_RE.findall(text)))


def _texts_of(entry: dict[str, Any], kind: str, code: str, languages: tuple[str, ...],
              default_language: str, problems: list[str]) -> dict[str, str] | None:
    """The fragment's text in every declared language, or None with a problem.

    A fragment's `text` is either one string - the set's default language,
    the form every set before v3.5 used - or an object keyed by language.
    A multilingual set must carry EVERY declared language for EVERY
    fragment: a missing translation is a message that cannot be rendered in
    the operator's language, and a fragment is reviewed as a whole.
    """
    raw = entry["text"]
    if isinstance(raw, str):
        texts = {default_language: raw}
    elif isinstance(raw, dict) and raw and all(isinstance(v, str) for v in raw.values()):
        texts = dict(raw)
    else:
        problems.append(f"{kind} {code!r}: 'text' must be a string or a language-to-text object")
        return None
    missing = [lang for lang in languages if not texts.get(lang, "").strip()]
    extra = [lang for lang in texts if lang not in languages]
    if missing or extra:
        problems.append(
            f"{kind} {code!r}: text must cover exactly the declared languages "
            f"{list(languages)} (missing {missing}, undeclared {extra})")
        return None
    return texts


def _load_fragments(
    raw: dict[str, Any], kind: str, facts: dict[str, FactSpec], problems: list[str],
    *, languages: tuple[str, ...] = ("de",), default_language: str = "de",
    language: str = "de",
) -> dict[str, FragmentSpec]:
    out: dict[str, FragmentSpec] = {}
    for code, entry in sorted(raw.items()):
        if not isinstance(entry, dict) or "text" not in entry:
            problems.append(f"{kind} {code!r}: missing 'text'")
            continue
        texts = _texts_of(entry, kind, code, languages, default_language, problems)
        if texts is None:
            continue
        bad = False
        slots_by_language: dict[str, tuple[str, ...]] = {}
        for lang, text in texts.items():
            offender = first_non_gsm7(text)
            if offender is not None:
                problems.append(
                    f"{kind} {code!r} [{lang}]: character {offender[0]!r} is not GSM-7 — the "
                    "message would become UCS-2 and no longer fit one SMS"
                )
                bad = True
                continue
            slots_by_language[lang] = _slots_of(text)
        if bad:
            continue
        slots = slots_by_language[language]
        if any(set(other) != set(slots) for other in slots_by_language.values()):
            problems.append(
                f"{kind} {code!r}: every language must use the same slots "
                f"({ {lang: sorted(v) for lang, v in slots_by_language.items()} })")
            continue
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
            code=code, text=texts[language], slots=slots, kind=kind,
            priority=int(entry.get("priority", 100)),
            texts=tuple((lang, texts[lang]) for lang in languages),
        )
    return out


def _widest(fragment: FragmentSpec, facts: dict[str, FactSpec],
            language: str | None = None) -> int:
    """Septets of a fragment with every slot at its reviewed maximum width,
    in `language` (default: the active one)."""
    filled = fragment.text if language is None else dict(fragment.texts)[language]
    for slot in fragment.slots:
        filled = filled.replace("{" + slot + "}", "W" * facts[slot].max_width)
    return septets(filled)


def _requested_language(explicit: str | None) -> str | None:
    """The operator's language, from the setting when the caller gave none."""
    if explicit is not None:
        return explicit
    try:
        from app.config import get_settings

        chosen = get_settings().message_language
        return None if chosen is None else str(chosen)
    except Exception:  # noqa: BLE001 - a validator must work without settings
        return None


def validate_phrase_set(raw_json: str, *, language: str | None = None) -> ValidatedPhraseSet:
    """Parse, check and hash a phrase set. Raises `PhraseSetInvalid`.

    `language` selects which of the set's languages the fragments' `text`
    is in; None means the operator's setting (MESSAGE_LANGUAGE). A language
    the set does not carry falls back to the set's default, and the result
    says so in `language` - the promoted bytes are the authority, the
    setting is a preference. Every language is held to the worst-case fit.
    """
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
    # Absent keys take the legacy defaults (a German-only set); a key that
    # is PRESENT must be well-formed. `or` conflated the two, so an explicit
    # empty inventory was normalized to the default instead of refused
    # (#119 round 5, SOTA-A, executed).
    default_language = meta.get("language", "de")
    if not isinstance(default_language, str) or not default_language:
        raise PhraseSetInvalid("meta.language must be a non-empty language code")
    declared = meta.get("languages", [default_language])
    if (not isinstance(declared, list) or not declared
            or any(not isinstance(lang, str) or not lang for lang in declared)):
        raise PhraseSetInvalid("meta.languages must be a non-empty list of language codes")
    languages = tuple(dict.fromkeys(str(lang) for lang in declared))
    if default_language not in languages:
        raise PhraseSetInvalid(
            f"meta.language {default_language!r} is not among meta.languages {list(languages)}")
    wanted = _requested_language(language)
    active = wanted if wanted in languages else default_language

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

    def load(section: str, kind: str) -> dict[str, FragmentSpec]:
        return _load_fragments(raw.get(section) or {}, kind, facts, problems,
                               languages=languages, default_language=default_language,
                               language=active)

    headlines = load("headlines", "headline")
    phrases = load("phrases", "phrase")
    next_checks = load("next_check", "next_check")
    caveats = load("caveats", "caveat")

    if not headlines:
        problems.append("phrase set declares no headlines")
    if not caveats:
        problems.append("phrase set declares no caveats")

    if problems:
        raise PhraseSetInvalid("; ".join(problems))

    # --- worst-case fit, in EVERY language --------------------------------
    # The operator may switch language by setting alone, so a set is only as
    # safe as its widest language: each is held to the limit, and the digest
    # the registry stores is over the maximum across languages.
    sep = septets(JOIN)
    by_language: dict[str, dict[str, int]] = {}
    for lang in languages:
        widest_headline = max(_widest(f, facts, lang) for f in headlines.values())
        widest_phrase = max((_widest(f, facts, lang) for f in phrases.values()), default=0)
        widest_next = max((_widest(f, facts, lang) for f in next_checks.values()), default=0)
        widest_caveat = max(_widest(f, facts, lang) for f in caveats.values())
        # MINIMAL: headline + the worst caveat. This is the floor that must
        # always fit; if it does not, no message from this set can be trusted
        # to send.
        minimal = widest_headline + sep + widest_caveat
        # FULL: headline + one phrase + next-check + one caveat.
        full = widest_headline + sep + widest_phrase + sep + widest_next + sep + widest_caveat
        tag = f" [{lang}]" if len(languages) > 1 else ""
        if minimal > SINGLE_SMS_SEPTETS:
            problems.append(
                f"minimal worst-case assembly{tag} is {minimal} septets, over "
                f"{SINGLE_SMS_SEPTETS}: shorten the longest headline or caveat"
            )
        if full > SINGLE_SMS_SEPTETS:
            problems.append(
                f"full worst-case assembly{tag} is {full} septets, over {SINGLE_SMS_SEPTETS}: "
                "shorten a fragment rather than relying on runtime omission"
            )
        by_language[lang] = {
            "widest_headline": widest_headline,
            "widest_phrase": widest_phrase,
            "widest_next_check": widest_next,
            "widest_caveat": widest_caveat,
            "minimal_assembly": minimal,
            "full_assembly": full,
            "limit": SINGLE_SMS_SEPTETS,
        }
    if problems:
        raise PhraseSetInvalid("; ".join(problems))
    worst_case = {
        key: max(values[key] for values in by_language.values())
        for key in next(iter(by_language.values()))
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
        language=active,
        languages=languages,
        worst_case_by_language=by_language,
    )
