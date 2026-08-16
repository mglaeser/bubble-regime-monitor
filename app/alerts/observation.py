"""Provider-independent observation identity.

This is the module that decides what "a new observation" means, and getting it
wrong is the single most likely way to send a false alert. Three separate
identities, deliberately not collapsed:

    economic_observation_key   WHAT was measured, and for WHICH economic period.
                               A vendor failover, a re-publication, a parser fix
                               and a redeploy all leave this UNCHANGED.

    source_revision_key        WHICH vintage of that observation, from WHICH
                               provider. Changes on a revision or a failover.

    computation_fingerprint    Which CODE turned that vintage into a value.
                               Changes on a redeploy or a parameter change.

Confirmation counts `economic_observation_key` by default. That is why a
provider failover in the middle of a two-observation confirmation cannot
manufacture the second observation: same day, same series, same key.

A rule may opt into `distinct_source_revision` — but only explicitly, and only
when replay has approved it.

Pure module: no clock, no session, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.alerts.canonical import identity_hash, sha256_of
from app.alerts.enums import DataState

# ---------------------------------------------------------------------------
# observation domains — stable LOGICAL series identities, never provider paths
# ---------------------------------------------------------------------------
#
# `market.spy.daily_close` is the same domain whether it came from Tiingo,
# Twelve Data or Alpha Vantage. A provider name must never appear here.

DOMAIN_SNAPSHOT = "snapshot.recompute"
DOMAIN_BAND_EFFECTIVE = "snapshot.effective_action_state"
DOMAIN_BAND_BASE = "snapshot.base_action_band"
DOMAIN_BAND_SCORE = "snapshot.score_action_band"
DOMAIN_HEADLINE_MEDIAN = "snapshot.headline_median"
DOMAIN_POINT_SCORE = "snapshot.point_score"
DOMAIN_IQR = "snapshot.iqr"
DOMAIN_OVERRIDE = "snapshot.override_fired"
DOMAIN_COVERAGE = "snapshot.coverage"

DOMAIN_RF1 = "redflag.rf1.gsadf"
DOMAIN_RF2 = "redflag.rf2.semi_runup"
DOMAIN_RF3 = "redflag.rf3.hy_oas"
DOMAIN_RF4 = "redflag.rf4.breadth"

DOMAIN_BREADTH = "indicator.d1.breadth"
DOMAIN_MARGIN = "indicator.d2.finra_release"
DOMAIN_HYPERSCALER_GATE = "indicator.d3.gate"
DOMAIN_LPPLS = "indicator.d4.lppls_endpoint"
DOMAIN_LPPLS_BANDS = "indicator.d4.lppls_bands"
DOMAIN_CAPE = "indicator.s1.cape"
DOMAIN_TOP10 = "indicator.s2.top10_share"
DOMAIN_SEMIS = "indicator.s3.semi_runup"
DOMAIN_GSADF = "indicator.s4.gsadf"
DOMAIN_S5_PERCENTILE = "indicator.s5.credit_percentile"

DOMAIN_HY_OAS = "credit.hy_oas.daily"
DOMAIN_IG_OAS = "credit.ig_oas.daily"
DOMAIN_EBP = "credit.ebp.monthly"

DOMAIN_LEG_SPY_FABER = "leg.spy.faber_month_end"
DOMAIN_LEG_QQQ_FABER = "leg.qqq.faber_month_end"
DOMAIN_LEG_SPY_SMA200 = "leg.spy.sma200"
DOMAIN_LEG_QQQ_SMA200 = "leg.qqq.sma200"

DOMAIN_VIX_STATE = "vol.vix.term_structure_state"
DOMAIN_VRP = "vol.vrp.daily"
DOMAIN_SKEW = "vol.skew.daily"

DOMAIN_FALSIFICATION = "governance.falsification_outcome"
DOMAIN_SOURCE_HEALTH = "ops.source_health"
DOMAIN_WATCHDOG_SLOT = "ops.recompute_slot"

#: Every domain a rule is allowed to name. The ruleset loader rejects anything
#: outside this set, so a typo becomes a validation failure rather than a rule
#: that silently never fires.
OBSERVATION_DOMAINS: frozenset[str] = frozenset(
    {
        DOMAIN_SNAPSHOT, DOMAIN_BAND_EFFECTIVE, DOMAIN_BAND_BASE, DOMAIN_BAND_SCORE,
        DOMAIN_HEADLINE_MEDIAN, DOMAIN_POINT_SCORE, DOMAIN_IQR, DOMAIN_OVERRIDE,
        DOMAIN_COVERAGE,
        DOMAIN_RF1, DOMAIN_RF2, DOMAIN_RF3, DOMAIN_RF4,
        DOMAIN_BREADTH, DOMAIN_MARGIN, DOMAIN_HYPERSCALER_GATE, DOMAIN_LPPLS,
        DOMAIN_LPPLS_BANDS, DOMAIN_CAPE, DOMAIN_TOP10, DOMAIN_SEMIS, DOMAIN_GSADF,
        DOMAIN_S5_PERCENTILE,
        DOMAIN_HY_OAS, DOMAIN_IG_OAS, DOMAIN_EBP,
        DOMAIN_LEG_SPY_FABER, DOMAIN_LEG_QQQ_FABER, DOMAIN_LEG_SPY_SMA200,
        DOMAIN_LEG_QQQ_SMA200,
        DOMAIN_VIX_STATE, DOMAIN_VRP, DOMAIN_SKEW,
        DOMAIN_FALSIFICATION, DOMAIN_SOURCE_HEALTH, DOMAIN_WATCHDOG_SLOT,
    }
)


def canonical_economic_period(period_start: str | None, period_end: str | None) -> str:
    """The economic period an observation refers to, as canonical text.

    A daily reading has start == end. A monthly release has the month. A period
    with no end is not a period: it hashes to a distinct, stable sentinel rather
    than to the empty string, so "unknown period" can never collide with "period
    1970-01-01".
    """
    if period_end is None and period_start is None:
        return "PERIOD_UNKNOWN"
    if period_start is None:
        return f"..{period_end}"
    if period_end is None:
        return f"{period_start}.."
    if period_start == period_end:
        return str(period_end)
    return f"{period_start}..{period_end}"


def economic_observation_key(
    observation_domain_id: str,
    *,
    period_start: str | None = None,
    period_end: str | None = None,
) -> str:
    """WHAT was measured and for WHICH period. Provider-independent by design."""
    return identity_hash(
        observation_domain_id, canonical_economic_period(period_start, period_end)
    )


def source_revision_key(
    econ_key: str,
    *,
    provider_id: str | None,
    provider_vintage: str | None,
    source_payload_sha256: str | None,
) -> str:
    """WHICH vintage, from WHICH provider. Changes on revision or failover."""
    return identity_hash(econ_key, provider_id, provider_vintage, source_payload_sha256)


def computation_fingerprint(
    revision_key: str,
    *,
    algorithm_version: str,
    parameter_sha256: str,
    code_revision: str,
) -> str:
    """Which code produced the value. Changes on redeploy or parameter change."""
    return identity_hash(revision_key, algorithm_version, parameter_sha256, code_revision)


@dataclass(frozen=True)
class Evidence:
    """One typed, point-in-time piece of evidence inside an alert input sidecar.

    Everything a rule is allowed to read about the world arrives as one of
    these. A rule never reaches past it to a provider.
    """

    observation_domain_id: str
    value: float | str | bool | None
    unit: str | None = None
    source_id: str | None = None
    provider_id: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    published_at: str | None = None
    observed_at: str | None = None
    economic_observation_key: str = ""
    source_revision_key: str = ""
    computation_fingerprint: str = ""
    data_state: str = DataState.FRESH
    freshness_reason_code: str | None = None
    known_issue_code: str | None = None
    known_issue_active: bool = False
    distance_to_threshold: float | None = None
    attribution: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def available(self) -> bool:
        """False when the value could not be observed at all.

        Deliberately NOT the same as "false" or "zero": an unavailable value
        yields UNKNOWN, never NORMAL.
        """
        return self.value is not None and self.data_state != DataState.MISSING

    def as_dict(self) -> dict[str, Any]:
        return {
            "observation_domain_id": self.observation_domain_id,
            "source_id": self.source_id,
            "provider_id": self.provider_id,
            "value": self.value,
            "unit": self.unit,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "published_at": self.published_at,
            "observed_at": self.observed_at,
            "economic_observation_key": self.economic_observation_key,
            "source_revision_key": self.source_revision_key,
            "computation_fingerprint": self.computation_fingerprint,
            "data_state": str(self.data_state),
            "freshness_reason_code": self.freshness_reason_code,
            "known_issue_code": self.known_issue_code,
            "known_issue_active": self.known_issue_active,
            "distance_to_threshold": self.distance_to_threshold,
            "attribution": self.attribution,
            "metadata": self.metadata or {},
        }


def build_evidence(
    observation_domain_id: str,
    value: float | str | bool | None,
    *,
    observed_at: str,
    unit: str | None = None,
    source_id: str | None = None,
    provider_id: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    published_at: str | None = None,
    data_state: str = DataState.FRESH,
    freshness_reason_code: str | None = None,
    known_issue_code: str | None = None,
    known_issue_active: bool = False,
    distance_to_threshold: float | None = None,
    attribution: str | None = None,
    algorithm_version: str = "1",
    parameters: dict[str, Any] | None = None,
    code_revision: str = "",
    provider_vintage: str | None = None,
    source_payload_sha256: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Evidence:
    """Build one evidence item with all three identities filled in.

    Callers never compute the keys by hand — that is how a domain and a period
    drift apart.
    """
    if observation_domain_id not in OBSERVATION_DOMAINS:
        raise ValueError(f"unknown observation domain {observation_domain_id!r}")
    econ = economic_observation_key(
        observation_domain_id, period_start=period_start, period_end=period_end
    )
    revision = source_revision_key(
        econ,
        provider_id=provider_id,
        provider_vintage=provider_vintage,
        source_payload_sha256=source_payload_sha256,
    )
    fingerprint = computation_fingerprint(
        revision,
        algorithm_version=algorithm_version,
        parameter_sha256=sha256_of(parameters or {}),
        code_revision=code_revision,
    )
    return Evidence(
        observation_domain_id=observation_domain_id,
        value=value,
        unit=unit,
        source_id=source_id,
        provider_id=provider_id,
        period_start=period_start,
        period_end=period_end,
        published_at=published_at,
        observed_at=observed_at,
        economic_observation_key=econ,
        source_revision_key=revision,
        computation_fingerprint=fingerprint,
        data_state=data_state,
        freshness_reason_code=freshness_reason_code,
        known_issue_code=known_issue_code,
        known_issue_active=known_issue_active,
        distance_to_threshold=distance_to_threshold,
        attribution=attribution,
        metadata=metadata,
    )
