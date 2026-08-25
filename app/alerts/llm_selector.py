"""The model selects CODES. It never writes a number and never writes prose.

This is the containment, and it is structural rather than instructional:

  * the model's only output is a JSON object of code identifiers;
  * every code is validated against the per-member authorization set BEFORE any
    text is assembled;
  * the renderer, not the model, interpolates every fact;
  * only codes, numeric facts, enums and preapproved labels enter the prompt.
    No scraped free text reaches the system instructions or the selectable
    content — the same rule `app/engine/judgment.py` follows for the scoring
    annotation.

**P1 never calls this.** A P1 is the message that must arrive; adding a network
call with a timeout to its critical path trades reliability for phrasing. P1s
render deterministically.

Every attempt is persisted, including timeouts and rejections, and every
attempt counts against the budget — otherwise a failing model gets unlimited
retries. Exhaustion is never a reason to delay or suppress a notification: the
deterministic template takes over.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts.canonical import new_ulid, sha256_of
from app.alerts.dto import PhraseSelection
from app.alerts.enums import LlmAttemptStatus, Priority, RenderSource
from app.alerts.errors import sanitize
from app.alerts.models import AlertLlmAttempt
from app.alerts.phrase_registry import ValidatedPhraseSet
from app.alerts.render_context import RenderContext
from app.alerts.repository import utc_ms
from app.logging_conf import get_logger

log = get_logger(__name__)

PROMPT_VERSION = "1"

SELECTION_OUTPUT_KEYS = frozenset({
    "headline_code",
    "phrase_codes",
    "fact_ids",
    "next_check_code",
    "caveat_codes",
})

SYSTEM_PROMPT = (
    "You select notification fragment CODES for a research alert. You do not "
    "write text and you do not write numbers. Reply with JSON only, using "
    "exactly the keys headline_code, phrase_codes, fact_ids, next_check_code "
    "and caveat_codes. Every value MUST come from the allowed lists you are "
    "given. Do not invent a code. Do not add commentary."
)


@dataclass(frozen=True)
class SelectionResult:
    selection: PhraseSelection | None
    render_source: str
    fallback_reason: str | None
    status: str
    duration_ms: int = 0


def _budget_used(session: Session, *, now: datetime, hours: int) -> int:
    return int(session.execute(
        select(func.count()).select_from(AlertLlmAttempt)
        .where(AlertLlmAttempt.attempted_at >= now - timedelta(hours=hours))
    ).scalar_one())


def _record(session: Session, *, delivery_id: str, now: datetime, model: str,
            status: str, duration_ms: int, context_hash: str,
            error: Exception | None = None, request_id: str | None = None) -> None:
    session.add(AlertLlmAttempt(
        attempt_id=new_ulid(utc_ms(now)),
        delivery_id=delivery_id,
        attempted_at=now,
        model=model,
        request_id=request_id,
        status=status,
        duration_ms=duration_ms,
        error_code=type(error).__name__ if error else None,
        error_message_redacted=sanitize(error) if error else None,
        context_hash=context_hash,
    ))


def build_prompt(context: RenderContext, phrase_set: ValidatedPhraseSet) -> str:
    """Codes, numbers and enums only. No free text from any external source."""
    member = context.primary
    payload = {
        "allowed_headline_codes": sorted(
            member.authorized_phrase_codes & set(phrase_set.headlines)),
        "allowed_phrase_codes": sorted(
            member.authorized_phrase_codes & set(phrase_set.phrases)),
        "allowed_next_check_codes": sorted(phrase_set.next_checks),
        "allowed_caveat_codes": sorted(phrase_set.caveats),
        "required_caveat_codes": list(member.required_caveat_codes),
        "available_fact_ids": sorted(member.authorized_fact_ids),
        "facts": dict(sorted(member.facts.items())),
        "condition_status": member.condition_status,
        "priority": member.priority,
        "bundle_size": len(context.members),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def validate_selection(raw: dict, context: RenderContext,
                       phrase_set: ValidatedPhraseSet) -> PhraseSelection:
    """Parse and authorize. A single unauthorized code rejects the whole reply."""
    selection = PhraseSelection.model_validate(raw)
    member = context.primary
    allowed_codes = member.authorized_phrase_codes

    if selection.headline_code not in allowed_codes or \
            selection.headline_code not in phrase_set.headlines:
        raise ValueError(f"headline_code {selection.headline_code!r} is not authorized")
    for code in selection.phrase_codes:
        if code not in allowed_codes or code not in phrase_set.phrases:
            raise ValueError(f"phrase code {code!r} is not authorized")
    if selection.next_check_code and selection.next_check_code not in phrase_set.next_checks:
        raise ValueError(f"next_check_code {selection.next_check_code!r} is unknown")
    for code in selection.caveat_codes:
        if code not in phrase_set.caveats:
            raise ValueError(f"caveat code {code!r} is unknown")
    for fact_id in selection.fact_ids:
        if fact_id not in member.authorized_fact_ids:
            raise ValueError(f"fact {fact_id!r} is not authorized for this member")
    missing = set(member.required_caveat_codes) - set(selection.caveat_codes)
    if missing:
        # Not an error the model can fix by retrying — the renderer adds them
        # unconditionally. Recorded so the A/B review can see it happened.
        log.info("alert_llm_omitted_required_caveats", codes=sorted(missing))
    return selection


def select_codes(
    session: Session,
    *,
    delivery_id: str,
    priority: int,
    context: RenderContext,
    phrase_set: ValidatedPhraseSet,
    now: datetime | None = None,
    settings: object | None = None,
    client: object | None = None,
) -> SelectionResult:
    """Ask the model for codes, or explain why we did not.

    Never raises. Every path returns a `SelectionResult` whose `render_source`
    tells the caller which template to use.
    """
    from app.config import get_settings

    now = now or datetime.now(UTC)
    settings = settings or get_settings()
    context_hash = context.context_hash()

    if priority == Priority.P1:
        # Deliberate: a P1 must not depend on a network call completing.
        return SelectionResult(None, RenderSource.TEMPLATE_FULL, "P1_DETERMINISTIC",
                               LlmAttemptStatus.BUDGET_SKIPPED)
    if not settings.alerts_llm_enabled or not settings.llm_configured:
        return SelectionResult(None, RenderSource.TEMPLATE_FULL, "LLM_UNAVAILABLE",
                               LlmAttemptStatus.BUDGET_SKIPPED)
    if _budget_used(session, now=now, hours=24) >= settings.alerts_llm_render_cap_24h:
        _record(session, delivery_id=delivery_id, now=now, model=settings.llm_model,
                status=LlmAttemptStatus.BUDGET_SKIPPED, duration_ms=0,
                context_hash=context_hash)
        return SelectionResult(None, RenderSource.TEMPLATE_FULL,
                               "LLM_BUDGET_EXHAUSTED", LlmAttemptStatus.BUDGET_SKIPPED)

    import time

    started = time.monotonic()
    try:
        raw, request_id = _call_model(context, phrase_set, settings, client)
        selection = validate_selection(raw, context, phrase_set)
    except TimeoutError as exc:
        duration = int((time.monotonic() - started) * 1000)
        _record(session, delivery_id=delivery_id, now=now, model=settings.llm_model,
                status=LlmAttemptStatus.TIMEOUT, duration_ms=duration,
                context_hash=context_hash, error=exc)
        return SelectionResult(None, RenderSource.TEMPLATE_FULL, "LLM_TIMEOUT",
                               LlmAttemptStatus.TIMEOUT, duration)
    except ValueError as exc:
        duration = int((time.monotonic() - started) * 1000)
        _record(session, delivery_id=delivery_id, now=now, model=settings.llm_model,
                status=LlmAttemptStatus.REJECTED, duration_ms=duration,
                context_hash=context_hash, error=exc)
        log.warning("alert_llm_selection_rejected", reason=sanitize(exc))
        return SelectionResult(None, RenderSource.TEMPLATE_FULL, "LLM_REJECTED",
                               LlmAttemptStatus.REJECTED, duration)
    except Exception as exc:
        duration = int((time.monotonic() - started) * 1000)
        _record(session, delivery_id=delivery_id, now=now, model=settings.llm_model,
                status=LlmAttemptStatus.ERROR, duration_ms=duration,
                context_hash=context_hash, error=exc)
        return SelectionResult(None, RenderSource.TEMPLATE_FULL, "LLM_ERROR",
                               LlmAttemptStatus.ERROR, duration)

    duration = int((time.monotonic() - started) * 1000)
    _record(session, delivery_id=delivery_id, now=now, model=settings.llm_model,
            status=LlmAttemptStatus.SUCCESS, duration_ms=duration,
            context_hash=context_hash, request_id=request_id)
    return SelectionResult(selection, RenderSource.LLM_SELECTION, None,
                           LlmAttemptStatus.SUCCESS, duration)


def _call_model(context: RenderContext, phrase_set: ValidatedPhraseSet,
                settings: object, client: object | None) -> tuple[dict, str | None]:
    """One structured-output call. Injected `client` keeps tests offline."""
    prompt = build_prompt(context, phrase_set)
    if client is not None:
        return client.select(system=SYSTEM_PROMPT, user=prompt), None

    from app.llm_gateway import complete

    response = complete(
        system=SYSTEM_PROMPT,
        user=prompt,
        max_tokens=min(400, settings.llm_max_tokens),
        deadline_s=float(settings.alerts_llm_timeout_s),
        json_output_keys=SELECTION_OUTPUT_KEYS,
        settings=settings,
    )
    return json.loads(response.text), response.request_id


def context_fingerprint(context: RenderContext) -> str:
    """Stable identity of what the model was shown. Used for A/B comparison."""
    return sha256_of({"context": context.context_hash(),
                      "facts": context.fact_catalog_hash(),
                      "prompt_version": PROMPT_VERSION})
