# Mandatory-event catalogue — candidates for approval

**Status: PROPOSAL. Not frozen, not wired into the gate.**

Per the standing decision, I derive the candidates and the operator approves
before anything is frozen. Nothing here is loaded by `run_replay` yet:
`--events` is still unset, so `mandatory_event_recall` continues to report
UNMEASURED rather than a number nobody agreed to.

## What the catalogue is for

`_collect_mandatory_events` measures **recall**: of the events that MUST be
detected, how many did the ruleset actually catch on replayed history. It is a
Stage 2 gate input, and it is the one measurement that can fail in the
direction nobody notices — a system that alerts on nothing scores perfectly on
every other metric in the artifact.

The implementation matches on `rule_id` against the episodes the replay opened.
An event whose window has no evaluable input is reported NOT_EVALUABLE, never
as a miss and never as a detection, because inflating recall in either
direction makes the gate meaningless.

## Tier A — the five P1 rules

A P1 is by definition an event where a miss is unacceptable. All five are
enabled and active from stage 3.

| rule_id | why a miss is unacceptable |
|---|---|
| `regime.band_to_derisk` | the action band reaching de-risk — the most consequential single signal the system produces |
| `override.fires` | an override changes the effective state regardless of score; missing it means acting on a band that no longer applies |
| `legs.faber_spy_out_high_risk` | a mechanical exit signal under high risk; the leg rules exist to be acted on the day they fire |
| `tripwire.rf4_persistent` | a red flag that PERSISTED rather than blipped — the persistence is what makes it actionable |
| `structure.s3_tier_150` | structural tier breach |

## Tier B — the three silence detectors

These are P2, and I am proposing them anyway. They detect that the system has
gone blind, and a missed blindness alert is strictly worse than a missed
market alert: the operator cannot tell it from a quiet market.

This is not hypothetical here. The 344-hour snapshot gap was a real
calculation bug that blocked snapshots, and nothing reported it — `ops.recompute_outage`
was proven blind rather than merely untested.

| rule_id | why |
|---|---|
| `ops.recompute_outage` | recomputes stopped. The failure that already happened, unreported |
| `ops.rf_input_unavailable` | a red-flag input is missing, so the tripwires above cannot fire at all |
| `ops.coverage_risk_masking` | coverage loss that could mask risk — a quiet score that is quiet because it is uninformed |

## Deliberately NOT proposed

Recall is only meaningful if the catalogue is the set of events that genuinely
must not be missed. Padding it makes the number look better and mean less.

| not proposed | reason |
|---|---|
| `regime.band_hold_to_trim`, `regime.band_trim_to_hold` | directional but not urgent; a miss is recovered at the next recompute |
| `tripwire.rf4_first` | a first occurrence may be noise. `rf4_persistent` is the one that must land |
| every `enabled: false` rule | a disabled rule cannot be mandatory while it is disabled |
| `regime.score_jump_1r`, `regime.score_trend_7d` | shipping `disabled_unpinned` by decision; not eligible |

## Draft catalogue

If approved as-is, this becomes `config/alert_mandatory_events.json` and the
gate is run with `--events`. The `occurred` field is deliberately empty: it
wants real dated instances from captured history, which is Stage 2 work, and
the current matcher does not read it.

```json
{
  "catalogue_version": "v1-draft",
  "frozen": false,
  "events": [
    {"rule_id": "regime.band_to_derisk",        "tier": "A", "occurred": []},
    {"rule_id": "override.fires",               "tier": "A", "occurred": []},
    {"rule_id": "legs.faber_spy_out_high_risk", "tier": "A", "occurred": []},
    {"rule_id": "tripwire.rf4_persistent",      "tier": "A", "occurred": []},
    {"rule_id": "structure.s3_tier_150",        "tier": "A", "occurred": []},
    {"rule_id": "ops.recompute_outage",         "tier": "B", "occurred": []},
    {"rule_id": "ops.rf_input_unavailable",     "tier": "B", "occurred": []},
    {"rule_id": "ops.coverage_risk_masking",    "tier": "B", "occurred": []}
  ]
}
```

## What I need from you

1. **Tier A** — accept all five, or remove any?
2. **Tier B** — include the silence detectors, or keep the catalogue to market events only?
3. Anything from the "not proposed" list you want pulled in?

Once you answer, freezing it is a small change: write the JSON, set
`frozen: true`, pass `--events` in the export script, and the recall figure
stops reading UNMEASURED.
