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

The file's structure is a pydantic schema (`PhraseSetDocument`), as the
ruleset's is; the checks here are what no schema states.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.alerts.canonical import canonical_json, sha256_hex, sha256_of
from app.alerts.errors import MessageLanguageInvalid, PhraseSetInvalid
from app.alerts.gsm7 import SINGLE_SMS_SEPTETS, first_non_gsm7, septets
from app.alerts.honesty import honesty_lint

PHRASE_VALIDATOR_VERSION = "1"

_SLOT_RE = re.compile(r"\{([A-Z0-9_]+)\}")

#: Assembly order. A message is HEADLINE, then phrases, then facts already
#: interpolated into those, then the next-check hint, then caveats.
JOIN = " "


class PhraseSetMeta(BaseModel):
    phrase_set_version: str = Field(min_length=1)
    #: The default language and every language the set carries. Both are
    #: required: absent, they once meant the German-only legacy form owner
    #: ruling 3 deleted (#119 round 5).
    language: str = Field(min_length=1)
    languages: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class FactEntry(BaseModel):
    label: str
    unit: str = ""
    max_width: int
    description: str = ""


class FragmentEntry(BaseModel):
    #: language -> text. The one-string form every set before v3.5 used is
    #: refused (owner ruling 3: no backward compatibility).
    text: dict[str, str]
    #: Optional; given, it names exactly the slots the text uses.
    slots: list[str] = Field(default_factory=list)
    priority: int = 100


class PhraseSetDocument(BaseModel):
    meta: PhraseSetMeta
    facts: dict[Annotated[str, Field(pattern=r"^F_")], FactEntry] = Field(default_factory=dict)
    headlines: dict[str, FragmentEntry] = Field(min_length=1)
    phrases: dict[str, FragmentEntry] = Field(default_factory=dict)
    next_check: dict[str, FragmentEntry] = Field(default_factory=dict)
    caveats: dict[str, FragmentEntry] = Field(min_length=1)

    def sections(self) -> dict[str, dict[str, FragmentEntry]]:
        """Each fragment table, by the kind of fragment it holds."""
        return {"headline": self.headlines, "phrase": self.phrases,
                "next_check": self.next_check, "caveat": self.caveats}

    @model_validator(mode="after")
    def _in_the_declared_languages(self) -> PhraseSetDocument:
        """The default language is declared, and EVERY fragment carries
        EVERY declared language and no other: a missing translation is a
        message that cannot be rendered in the operator's language, and a
        fragment is reviewed as a whole."""
        meta = self.meta
        if meta.language not in meta.languages:
            raise ValueError(
                f"meta.language {meta.language!r} is not among meta.languages {meta.languages}")
        declared = set(meta.languages)
        problems: list[str] = []
        for kind, fragments in self.sections().items():
            for code, fragment in fragments.items():
                present = {lang for lang, text in fragment.text.items() if text.strip()}
                missing, extra = sorted(declared - present), sorted(set(fragment.text) - declared)
                if missing or extra:
                    problems.append(
                        f"{kind} {code!r}: text must cover exactly the declared languages "
                        f"(missing {missing}, undeclared {extra})")
        if problems:
            raise ValueError("; ".join(problems))
        return self


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
    priority: int                  # lower is dropped LAST when fitting
    #: (language, text) for every language the set was reviewed in.
    texts: tuple[tuple[str, str], ...]


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
    language: str
    languages: tuple[str, ...]
    worst_case_by_language: dict[str, dict[str, int]]

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
    entries: dict[str, FragmentEntry], kind: str, facts: dict[str, FactSpec],
    problems: list[str], *, languages: tuple[str, ...], language: str,
) -> dict[str, FragmentSpec]:
    out: dict[str, FragmentSpec] = {}
    for code, entry in sorted(entries.items()):
        bad = False
        slots_by_language: dict[str, tuple[str, ...]] = {}
        for lang, text in entry.text.items():
            offender = first_non_gsm7(text)
            if offender is not None:
                problems.append(
                    f"{kind} {code!r} [{lang}]: character {offender[0]!r} is not GSM-7 — the "
                    "message would become UCS-2 and no longer fit one SMS"
                )
                bad = True
                continue
            # Every language, not only the active one: the operator may
            # switch by setting alone, and the renderer's lint would then
            # refuse every message this fragment is part of (#119 round 7).
            forbidden = honesty_lint(text)
            if forbidden is not None:
                problems.append(
                    f"{kind} {code!r} [{lang}]: contains forbidden vocabulary {forbidden!r} — "
                    "the score is not a probability and this service gives no advice"
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
        if "slots" in entry.model_fields_set and set(entry.slots) != set(slots):
            problems.append(
                f"{kind} {code!r}: declares slots {sorted(entry.slots)} but its text uses "
                f"{sorted(slots)}"
            )
            continue
        out[code] = FragmentSpec(
            code=code, text=entry.text[language], slots=slots, kind=kind,
            priority=entry.priority,
            texts=tuple((lang, entry.text[lang]) for lang in languages),
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
    """The operator's language, from the setting when the caller gave none.

    The validator works without a settings context - a script or a test
    outside the app's environment - but a MALFORMED setting is refused,
    not masked: one broad fallback let MESSAGE_LANGUAGE=fr validate the
    shipped set as German, wrong-language alerts instead of a rejected
    configuration (#119 round 6, SOTA-A, executed).
    """
    if explicit is not None:
        return explicit
    try:
        from app.config import get_settings

        chosen = get_settings().message_language
    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001 - only the language is ours to judge
        complaint = _complaint_about(exc, "message_language")
        if complaint is not None:
            raise MessageLanguageInvalid(f"MESSAGE_LANGUAGE is malformed: {complaint}") from exc
        return None
    return None if chosen is None else str(chosen)


def _complaint_about(exc: BaseException, field: str) -> str | None:
    """The settings validator's own message about `field`, if the failure
    names it; None when the settings failed for reasons that are not this
    field's (no environment at all, another field)."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return None
    try:
        details = errors()
    except Exception:  # noqa: BLE001 - an unreadable failure is not ours
        return None
    for detail in details or ():
        if isinstance(detail, dict) and field in tuple(detail.get("loc") or ()):
            return str(detail.get("msg") or "invalid value")
    return None


def validate_phrase_set(raw_json: str, *, language: str | None = None) -> ValidatedPhraseSet:
    """Parse, check and hash a phrase set. Raises `PhraseSetInvalid`.

    `language` selects which of the set's languages the fragments' `text`
    is in; None means the operator's setting (MESSAGE_LANGUAGE). A language
    the set does not carry falls back to the set's default, and the result
    says so in `language` - the promoted bytes are the authority, the
    setting is a preference. Every language is held to the worst-case fit.
    """
    try:
        raw = json.loads(raw_json)
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError is a ValueError, and so is an integer past Python's
        # digit limit; nesting too deep for the decoder is a RecursionError.
        # Every caller fails closed on PhraseSetInvalid alone (#175 round 3).
        raise PhraseSetInvalid(f"phrase set is not valid JSON: {exc}") from exc
    try:
        # Strict: a value is what the reviewed file says, never a coercion of it.
        document = PhraseSetDocument.model_validate(raw, strict=True)
    except ValidationError as exc:
        raise PhraseSetInvalid(f"phrase set failed schema validation: {exc}") from exc

    languages = tuple(dict.fromkeys(document.meta.languages))
    wanted = _requested_language(language)
    active = wanted if wanted in languages else document.meta.language

    problems: list[str] = []
    facts = {fact_id: FactSpec(fact_id=fact_id, **entry.model_dump())
             for fact_id, entry in sorted(document.facts.items())}
    # `score` as a fact would be exactly the ambiguity the mandate forbids.
    for banned in ("F_SCORE", "F_BAND"):
        if banned in facts:
            problems.append(
                f"fact {banned!r} is forbidden — name the median and the point score separately"
            )

    headlines, phrases, next_checks, caveats = (
        _load_fragments(entries, kind, facts, problems, languages=languages, language=active)
        for kind, entries in document.sections().items())
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
        version=document.meta.phrase_set_version,
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
