"""Build the point-in-time alert input sidecar from a COMMITTED snapshot.

This is the only place the alert system touches scoring data, and it reads —
never computes. The band decomposition, the red-flag contract, the coverage
verdict and the leg states were all decided by the scoring layer and persisted;
this module copies them into an immutable sidecar with provenance attached.

A replay reads the sidecar. It never reaches a provider, and structurally it
cannot: nothing here imports one.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.alerts import observation as obs
from app.alerts.canonical import canonical_json, sha256_hex
from app.alerts.dto import (
    ALERT_INPUT_SCHEMA_VERSION,
    AlertInput,
    EvidenceModel,
    RedFlagFactModel,
    recompute_input_identity,
    watchdog_input_identity,
)
from app.alerts.enums import DataState, Evaluability, InputOrigin
from app.alerts.observation import build_evidence
from app.models import Snapshot

#: Bumped when the BUILDER changes what it extracts from a snapshot, so a
#: fingerprint reflects the code that produced the value.
BUILDER_CODE_REVISION = "1"


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _leg_state(trend_states: dict[str, Any], asset: str, leg: str) -> str | None:
    value = (trend_states or {}).get(asset, {}).get(leg)
    return None if value in (None, "") else str(value)


def _coverage_data_state(degraded: bool) -> str:
    return DataState.STALE if degraded else DataState.FRESH


def _indicator_payloads(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    """Per-indicator payloads, merged from both block JSON blobs."""
    out: dict[str, dict[str, Any]] = {}
    for block in (snapshot.block_s or {}, snapshot.block_d or {}):
        for ind_id, payload in (block.get("indicators") or {}).items():
            if isinstance(payload, dict):
                out[ind_id] = payload
    return out


_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_QUARTER_LABEL = re.compile(r"^(\d{4})-Q([1-4])$")


def _period_is_future(period: Any, computed_at: str | None) -> bool:
    """Is this economic period labelled after the moment we observed it?

    ONLY answers for labels that are lexically comparable to an ISO date, i.e.
    `YYYY-MM` or `YYYY-MM-DD`. A quarterly label like `2026-Q2` is a perfectly
    valid economic period — it is the confirmation key for "once per filing" —
    but comparing it lexically to `2026-08-21` puts `Q` (0x51) above `0` (0x30),
    so every valid past filing reads as future. That flagged the snapshot
    ineligible and suppressed the alert, using the very check meant to protect
    it. Anything not shaped like a date is not comparable, and an
    incomparable label is not evidence of a bad vintage.
    """
    if not period or not computed_at:
        return False
    text = str(period)
    quarter = _QUARTER_LABEL.match(text)
    if quarter:
        # A quarter is future while the quarter it NAMES has not ENDED, and that
        # must be decided on the same day granularity as an ISO label. Comparing
        # end MONTHS let a quarter read as closed throughout its own final month
        # — 2026-Q3 on 2026-09-01 has 29 days still to run, and a month-level
        # test called it past. Compare the quarter's last day instead.
        year, q = quarter.group(1), int(quarter.group(2))
        return f"{year}-{('03-31', '06-30', '09-30', '12-31')[q - 1]}" > computed_at[:10]
    if not _ISO_PREFIX.match(text):
        return False
    return text > computed_at[:10]


def _evidence_data_state(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Freshness of one indicator reading, with a reason code.

    An UNDATED reading is `UNKNOWN_AGE`, not `FRESH` — the same v3.7.3/A-01
    convention the coverage gate uses. Treating "we do not know how old this
    is" as fresh is how a hold source silently stops holding.
    """
    if payload.get("dropped"):
        return DataState.MISSING, "indicator_dropped"
    stale = payload.get("stale")
    if stale is None:
        return DataState.UNKNOWN_AGE, "reading_date_unknown"
    if stale:
        return DataState.STALE, "past_freshness_sla"
    return DataState.FRESH, None


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def build_alert_input(snapshot: Snapshot, *, built_at: datetime,
                      service_version: str,
                      falsification_events: list[dict[str, Any]] | None = None) -> AlertInput:
    """Assemble the sidecar for one committed snapshot.

    `built_at` is passed in rather than read from a clock so a reconstruction
    or a replay produces the same bytes twice.
    """
    computed_at = _iso(snapshot.computed_at)
    observed_at = computed_at or _iso(built_at) or ""
    indicators = _indicator_payloads(snapshot)
    ineligibility: list[str] = []

    # --- typed red flags -------------------------------------------------
    meta = snapshot.red_flag_meta or {}
    flags_raw = meta.get("flags") or {}
    red_flags = [
        RedFlagFactModel(**flag) for _fid, flag in sorted(flags_raw.items())
        if isinstance(flag, dict)
    ]
    if not red_flags:
        ineligibility.append("no_typed_red_flag_contract")
    if snapshot.effective_action_state is None:
        ineligibility.append("no_typed_action_state")

    # --- indicator evidence ----------------------------------------------
    evidence: list[EvidenceModel] = []

    def add(domain: str, value: Any, *, unit: str | None = None,
            source_id: str | None = None, period_start: str | None = None,
            period_end: str | None = None, data_state: str = DataState.FRESH,
            reason: str | None = None, provider_id: str | None = None,
            attribution: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        item = build_evidence(
            domain, value, observed_at=observed_at, unit=unit, source_id=source_id,
            provider_id=provider_id, period_start=period_start, period_end=period_end,
            data_state=data_state, freshness_reason_code=reason,
            attribution=attribution, code_revision=BUILDER_CODE_REVISION,
            metadata=metadata,
        )
        evidence.append(EvidenceModel(**item.as_dict()))

    _INDICATOR_DOMAINS: tuple[tuple[str, str, str | None], ...] = (
        ("d1", obs.DOMAIN_BREADTH, "percent"),
        # d2 is NOT here: its generic `value` is the year-on-year percentage,
        # while the rollover tripwire reads the MULTIPLIER. Publishing one
        # under the other's name is handled explicitly below.
        ("d4", obs.DOMAIN_LPPLS, "confidence"),
        ("s1", obs.DOMAIN_CAPE, None),
        ("s2", obs.DOMAIN_TOP10, "percent"),
        ("s3", obs.DOMAIN_SEMIS, "pp"),
        ("s4", obs.DOMAIN_GSADF, "stat"),
        ("s5", obs.DOMAIN_S5_PERCENTILE, "percentile"),
    )
    for ind_id, domain, unit in _INDICATOR_DOMAINS:
        payload = indicators.get(ind_id)
        if payload is None:
            add(domain, None, unit=unit, source_id=ind_id, data_state=DataState.MISSING,
                reason="indicator_absent")
            continue
        state, reason = _evidence_data_state(payload)
        as_of = payload.get("as_of")
        add(domain, _numeric(payload.get("value")), unit=unit, source_id=ind_id,
            period_start=as_of, period_end=as_of, data_state=state, reason=reason,
            provider_id=payload.get("data_source"),
            metadata={"sub_score": payload.get("sub_score"),
                      "quality": payload.get("quality"),
                      "state": payload.get("state")})
        if as_of and computed_at and as_of > computed_at[:10]:
            # A reading dated after the moment we observed it is a bad vintage
            # or clock skew — recorded, never silently used.
            ineligibility.append(f"period_label_future:{ind_id}")

    # d2 publishes the ROLLOVER MULTIPLIER, not the year-on-year percentage.
    #
    # `tripwire.margin_rollover` watches d2_multiplier cross 1.0 upward. The
    # generic loop would publish the indicator's `value`, and for d2 that is the
    # margin-debt YoY percentage — roughly 49, permanently far above 1.0 — so
    # the rule could never observe the 0.6 -> 1.0 rollover it exists to report,
    # while being marked READY.
    #
    # The tri-state matters as much as the quantity. `rollover_confirmed_calendar`
    # returns None on a FINRA publication gap, meaning "cannot assert". Scoring
    # must pick a number and collapses that to the no-rollover multiplier;
    # alerting must NOT inherit the collapse, or the tripwire reads a missing
    # publication as a definite "no rollover". Unknown is not normal.
    d2 = indicators.get("d2") or {}
    d2_state, d2_reason = _evidence_data_state(d2) if d2 else (DataState.MISSING,
                                                               "indicator_absent")
    d2_assertable = d2.get("rollover_assertable")
    d2_multiplier = _numeric(d2.get("multiplier")) if d2_assertable is True else None
    if not d2:
        d2_value, d2_state, d2_reason = None, DataState.MISSING, "indicator_absent"
    elif d2_assertable is None:
        # A row persisted before the typed payload existed. Historical rows are
        # NOT_EVALUABLE for this rule, never a fabricated number (mandate 5.4).
        d2_value, d2_state, d2_reason = None, DataState.MISSING, "typed_rollover_absent"
    elif d2_multiplier is None:
        d2_value, d2_state, d2_reason = None, DataState.MISSING, "rollover_not_assertable"
    else:
        d2_value = d2_multiplier
    d2_period = d2.get("release_period") or d2.get("as_of")
    add(obs.DOMAIN_MARGIN, d2_value, unit="multiplier", source_id="d2",
        period_start=d2_period, period_end=d2_period,
        data_state=d2_state, reason=d2_reason,
        provider_id=d2.get("data_source"),
        metadata={"yoy_pct": d2.get("yoy_pct"),
                  "rollover_state": d2.get("rollover_state"),
                  "sub_score": d2.get("sub_score")})
    # The same vintage check the generic loop applies. Lifting d2 out of that
    # loop silently dropped it — a reading dated after the moment we observed
    # it is a bad vintage or clock skew, and it must mark the INPUT ineligible
    # rather than being evaluated as ordinary fresh evidence.
    if _period_is_future(d2_period, computed_at):
        ineligibility.append("period_label_future:d2")

    # d3 is a GATE (a decision), not a level.
    d3 = indicators.get("d3") or {}
    d3_state, d3_reason = _evidence_data_state(d3) if d3 else (DataState.MISSING, "indicator_absent")
    # `IndicatorOutput.payload()` FLATTENS `extra` into the top level, so the
    # gate arrives as a top-level key. Reading `payload["extra"]["gate_fired"]`
    # returned None on every snapshot that has ever existed, which rendered as
    # "gate not persisted" while the rule stayed marked READY.
    gate = d3.get("gate_fired")
    # NOT a filing period: EDGAR provenance carries only `as_of`, the reading
    # date. Naming it a filing period would invent an identity the source does
    # not provide, and the "once per new filing" confirmation this rule needs
    # cannot be built from a reading date — every recompute would look like a
    # new filing. The gate VALUE is now readable; its confirmation key is not.
    d3_period = d3.get("as_of")
    add(obs.DOMAIN_HYPERSCALER_GATE, gate if isinstance(gate, bool) else None,
        source_id="d3", period_start=d3_period, period_end=d3_period,
        data_state=d3_state if isinstance(gate, bool) else DataState.MISSING,
        reason=d3_reason if isinstance(gate, bool) else "gate_state_not_persisted",
        metadata={"issuers_used": d3.get("issuers_used"),
                  "issuers_full": d3.get("issuers_full"),
                  "filing_period_available": bool(d3.get("filing_period_available"))})
    # Checked against the ISO reading date, not the filing LABEL: `as_of` is a
    # real date, `filing_period` is "2026-Q2" and means something else.
    if _period_is_future(d3.get("as_of"), computed_at):
        ineligibility.append("period_label_future:d3")

    # --- legs --------------------------------------------------------------
    legs: list[EvidenceModel] = []
    trend = snapshot.trend_states or {}
    for asset, faber_domain, sma_domain in (
        ("SPY", obs.DOMAIN_LEG_SPY_FABER, obs.DOMAIN_LEG_SPY_SMA200),
        ("QQQ", obs.DOMAIN_LEG_QQQ_FABER, obs.DOMAIN_LEG_QQQ_SMA200),
    ):
        for domain, leg in ((faber_domain, "faber_10mo"), (sma_domain, "sma200")):
            state = _leg_state(trend, asset, leg)
            item = build_evidence(
                domain, state, observed_at=observed_at, source_id=f"{asset}.{leg}",
                period_start=computed_at, period_end=computed_at,
                data_state=DataState.MISSING if state in (None, "unknown") else DataState.FRESH,
                freshness_reason_code=None if state not in (None, "unknown") else "leg_state_unknown",
                code_revision=BUILDER_CODE_REVISION,
            )
            legs.append(EvidenceModel(**item.as_dict()))

    # --- volatility --------------------------------------------------------
    credit_sidecar: list[EvidenceModel] = []
    v_item = build_evidence(
        obs.DOMAIN_VIX_STATE, snapshot.v_state or None, observed_at=observed_at,
        source_id="v", period_start=computed_at, period_end=computed_at,
        data_state=DataState.FRESH if snapshot.v_state else DataState.MISSING,
        code_revision=BUILDER_CODE_REVISION,
    )
    credit_sidecar.append(EvidenceModel(**v_item.as_dict()))

    # HY OAS distance is READ from the persisted rf3 metadata — never recomputed.
    rf3 = flags_raw.get("rf3") or {}
    hy_item = build_evidence(
        obs.DOMAIN_HY_OAS, None, observed_at=observed_at, unit="bps", source_id="s5.hy_oas",
        period_start=rf3.get("period_start"), period_end=rf3.get("period_end"),
        data_state=DataState.MISSING, freshness_reason_code="level_not_persisted_on_snapshot",
        distance_to_threshold=rf3.get("distance_to_threshold"),
        code_revision=BUILDER_CODE_REVISION,
    )
    credit_sidecar.append(EvidenceModel(**hy_item.as_dict()))

    # --- eligibility -------------------------------------------------------
    if snapshot.alert_contract_version is None:
        ineligibility.append("pre_stage0_snapshot")
    eligibility = Evaluability.EVALUABLE
    if "pre_stage0_snapshot" in ineligibility or "no_typed_action_state" in ineligibility:
        eligibility = Evaluability.NOT_EVALUABLE
    elif ineligibility:
        eligibility = Evaluability.PARTIAL

    return AlertInput(
        schema_version=ALERT_INPUT_SCHEMA_VERSION,
        input_identity=recompute_input_identity(snapshot.id, snapshot.methodology_sha256),
        origin=InputOrigin.RECOMPUTE,
        snapshot_id=snapshot.id,
        computed_at=computed_at,
        built_at=_iso(built_at) or "",
        expected_recompute_slot=_iso(snapshot.expected_recompute_slot),
        prev_snapshot_id=snapshot.prev_snapshot_id,
        service_version=service_version,
        methodology_version=snapshot.methodology_version,
        methodology_sha256=snapshot.methodology_sha256,
        headline_median=snapshot.median,
        point_score=snapshot.point_score,
        iqr_lo=snapshot.iqr_lo,
        iqr_hi=snapshot.iqr_hi,
        band5=snapshot.band5,
        band95=snapshot.band95,
        score_action_band=snapshot.score_action_band,
        base_action_band=snapshot.base_action_band,
        effective_action_state=snapshot.effective_action_state,
        band_suppressed_by_coverage=bool(snapshot.band_suppressed_by_coverage),
        data_degraded=bool(snapshot.data_degraded),
        override_fired=bool(snapshot.override_fired),
        override_required_count=snapshot.override_required_count,
        override_fireable_universe_count=snapshot.override_fireable_universe_count,
        red_flags=red_flags,
        coverage=(snapshot.data_freshness or {}).get("_coverage", {}),
        blocks={"S": (snapshot.block_s or {}).get("value"),
                "D": (snapshot.block_d or {}).get("value")},
        indicators=evidence,
        legs=legs,
        fast_alarm=snapshot.fast_alarm or {},
        credit_sidecar=credit_sidecar,
        release_calendar=[],
        falsification_events=list(falsification_events or []),
        source_health_summary={"data_degraded": bool(snapshot.data_degraded),
                               "coverage_data_state": _coverage_data_state(
                                   bool(snapshot.data_degraded))},
        evaluation_eligibility=eligibility,
        ineligibility_reasons=sorted(set(ineligibility)),
        reconstructed=False,
    )


def build_watchdog_input(
    *,
    expected_slot_key: str,
    missed_slots: int,
    built_at: datetime,
    service_version: str,
    last_snapshot: Snapshot | None,
) -> AlertInput:
    """A sidecar for "no snapshot arrived", built without a snapshot.

    The identity is derived from the SLOT, so a watchdog that fires twice for
    the same missed slot produces the same input identity and cannot open a
    second outage episode.
    """
    observed_at = _iso(built_at) or ""
    item = build_evidence(
        obs.DOMAIN_WATCHDOG_SLOT, float(missed_slots), observed_at=observed_at,
        unit="slots", source_id="watchdog", period_start=expected_slot_key,
        period_end=expected_slot_key, code_revision=BUILDER_CODE_REVISION,
    )
    return AlertInput(
        input_identity=watchdog_input_identity(expected_slot_key),
        origin=InputOrigin.WATCHDOG,
        snapshot_id=None,
        computed_at=_iso(last_snapshot.computed_at) if last_snapshot else None,
        built_at=observed_at,
        expected_recompute_slot=expected_slot_key,
        service_version=service_version,
        methodology_version=last_snapshot.methodology_version if last_snapshot else None,
        methodology_sha256=last_snapshot.methodology_sha256 if last_snapshot else None,
        indicators=[EvidenceModel(**item.as_dict())],
        evaluation_eligibility=Evaluability.PARTIAL,
        ineligibility_reasons=["watchdog_input_carries_no_scoring_state"],
    )


def serialize(alert_input: AlertInput) -> tuple[str, str]:
    """Canonical payload bytes and their hash, for persistence."""
    payload = canonical_json(alert_input.model_dump(mode="json"))
    return payload, sha256_hex(payload)
