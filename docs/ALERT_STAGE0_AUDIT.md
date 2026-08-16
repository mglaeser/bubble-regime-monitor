# Alert system — Stage 0 audit and typed snapshot contract

Audit of `mglaeser/bubble-regime-monitor` @ `main` (`8d2c3a9`), service version
**3.8.0**, against the *bubblegauge Alert & SMS System — Final Integrated
Implementation Mandate (v3.2-FINAL)*. This is the Stage 0 gate artifact: what
the repository actually looks like, where the mandate's assumptions hold, where
they drifted, and what Stage 0 changed.

Nothing in Stage 0 sends anything. No alert table, router, job or rule engine
exists yet — Stage 0 only makes the scoring layer *legible* to one.

---

## 1. Baseline facts — verified, not assumed

| Mandate §2.1 claim | Verified | Evidence |
|---|---|---|
| `Settings.service_version == "3.8.0"` | ✅ | `app/config.py:93` |
| `Snapshot` is append-style with an integer PK | ✅ | `app/models.py:20` — autoincrement PK, insert-only path in `persist_snapshot` |
| Snapshots carry `methodology_sha256` / `methodology_version` | ✅ | `app/models.py:46-47`, written from the loader at `app/services/compute.py` |
| `action_band` is a display string with degraded variants | ✅ | `app/services/compute.py` writes `"suppressed (block degraded)"` / `"de-risk (data degraded)"` |
| `red_flag_detail` holds booleans only | ✅ | `RedFlags.as_dict()` → `dict[str, bool]` (`app/engine/aggregate.py:160`) |
| Legacy SMS path = `sms_report.py`, `services/digest.py`, `notify/sipgate.py`, `daily_sms` job, `POST /api/v1/admin/send-sms` | ✅ | all five present |
| SQLite pragmas: WAL, foreign_keys, synchronous=NORMAL, recursive_triggers | ✅ | `app/db.py:37-45` |
| `busy_timeout` **not** configured | ✅ (drift confirmed) | absent from `_set_sqlite_pragmas`; must be added before concurrent alert writes |
| Alembic authoritative, `create_all` as boot fallback | ✅ | `app/db_migrate.py:75-86` |
| Protected reads fall back to the admin key | ✅ | `app/security.py:48-60` — `require_read_access` compares against `admin_api_key` |
| No alert router / ORM model exists | ✅ | none in `app/routers/`, `app/models.py` |

### Drift found against the mandate

1. **The action band is derived from the Monte Carlo *median*, not the point
   score.** `app/services/compute.py`: `band = action_band_with_override(mc.median, red_flags)`.
   The mandate is right to forbid a bare `score` field (§10.5) — the two values
   genuinely differ (golden fixture: point 52.43 vs median 52.56). Every band
   rule reads `headline_median`; `point_score` is a separate typed fact.
2. **The mandate's `rf1..rf4` names do not exist in the codebase.** The frozen
   artifact keys them `gsadf_explosive_noncontested`, `semi_runup_ge_150pp`,
   `hy_oas_widen_gt_100bps`, `breadth_lt_50_near_ath`. Stage 0 introduces the
   `rf1..rf4` alert-facing identities *with an explicit persisted mapping*
   (`FLAG_IDS` in `app/engine/snapshot_contract.py`, `source_key` on every
   persisted fact) rather than leaving the alert layer to guess.
3. **`rf1` non-fireability is not a governance constant, it is a runtime
   setting.** `GSADF_CONTESTED` defaults `true`, which makes rf1 structurally
   incapable of firing — but an operator can flip it. Per mandate §5.3 the
   alert layer must not hardcode this, so fireability is computed per run and
   persisted (`fireable`, `state=BLOCKED`).
4. **The recompute cron was a literal in `app/scheduler.py`.** The watchdog and
   `expected_recompute_slot` need the same schedule; three copies would drift.
   Stage 0 moves it to `app/engine/recompute_slots.py` and the scheduler now
   reads from there.
5. **The mandate's Alembic revision numbers are stale.** Head was `0006`
   (`0006_replay_evidence`), so the Stage 0 revision is `0007`, not the
   assumed-free `0007` — coincidentally the same, but verified rather than
   assumed. Later stages must re-check head before numbering.

---

## 2. What Stage 0 adds

### 2.1 `app/engine/snapshot_contract.py` — pure, non-scoring

Derives the typed decomposition by **calling** the authoritative band
functions in `app/engine/aggregate.py`. It restates no threshold and no
formula; re-implementing one would be a defect even if the result matched
(invariant 2).

```
score_action_band       aggregate.action_band(headline_median)
base_action_band        aggregate.action_band_with_override(headline_median, red_flags)
effective_action_state  de-risk    when the override wins under degraded data
                        suppressed when coverage suppresses the base decision
                        base_action_band otherwise
```

The legacy `action_band` column keeps its exact current value and rendering.
The alert layer must never parse it.

### 2.2 Typed red-flag contract

`red_flag_meta` is one object per flag plus the override arithmetic:

```jsonc
{
  "contract_version": 1,
  "flags": {
    "rf3": {
      "flag_id": "rf3", "source_key": "hy_oas_widen_gt_100bps",
      "active": false, "fireable": true, "state": "INACTIVE",
      "distance_to_threshold": -83.0, "unit": "bps",
      "period_start": "2026-08-14", "period_end": "2026-08-14",
      "published_at": null, "observed_at": "2026-08-15T10:00:00+00:00",
      "data_state": "FRESH"
    }
  },
  "override_required_count": 3,
  "override_fireable_universe_count": 3,
  "override_fired": false
}
```

Per-flag objects live under `flags` rather than at the top level (the mandate's
§5.3 example shows them at top level, but the same object must also carry the
three required top-level fields — nesting removes the key-collision risk).
Examples never override a typed contract (§0.1), and the choice is recorded
here.

Two state axes are kept **separate**, because collapsing them is exactly the
failure the mandate's `UNKNOWN`-is-not-normal invariant guards against:

| axis | values | meaning |
|---|---|---|
| `state` | `ACTIVE` / `INACTIVE` / `UNKNOWN` / `BLOCKED` | `UNKNOWN` = required input unavailable; `BLOCKED` = governance forbids firing (contested GSADF) |
| `data_state` | `FRESH` / `STALE` / `UNKNOWN_AGE` / `MISSING` | `UNKNOWN_AGE` keeps "old" distinct from "undated" (the v3.7.3/A-01 convention) |

`published_at` is **always null**: no upstream vendor publication timestamp is
available to this service, and inventing one would poison confirmation
semantics later.

### 2.3 New snapshot columns (migration `0007`)

`prev_snapshot_id`, `expected_recompute_slot`, `alert_contract_version`,
`score_action_band`, `base_action_band`, `effective_action_state`,
`band_suppressed_by_coverage`, `data_degraded`, `red_flag_meta`,
`override_required_count`, `override_fireable_universe_count`.

`expected_recompute_slot` is defined as **the first scheduled slot strictly
after `computed_at`** — i.e. when the successor snapshot is due. That is the
value the watchdog's missed-slot count needs, and it is always in the future
relative to the row that carries it.

### 2.4 Backfill — deliberately conservative (§5.4)

| Legacy `action_band` | score | base | effective | degraded | suppressed |
|---|---|---|---|---|---|
| `hold` / `trim` / `de-risk`, no override | = band | = band | = band | false | false |
| `hold` / `trim` / `de-risk`, override fired | **null** | = band | = band | false | false |
| `suppressed (block degraded)` | **null** | **null** | `suppressed` | true | true |
| `de-risk (data degraded)`, override fired | **null** | `de-risk` | `de-risk` | true | false |
| `de-risk (data degraded)`, no override | **null** | **null** | `de-risk` | true | false |
| anything else | **null** | **null** | **null** | false | false |

`score_action_band` stays null wherever an override fired: the override can
raise the decision above what the median alone implied, so the median-only band
is not recoverable from history.

`red_flag_meta` stays `{}` for every historical row and
`alert_contract_version` stays null — per-flag fireability was never recorded
and **must not be invented**. Rows without it are `NOT_EVALUABLE` for any rule
that needs typed flag metadata, and must be reported as such rather than
counted as recall.

`prev_snapshot_id` and `expected_recompute_slot` *are* backfilled: the first is
the append-order predecessor, the second a pure function of `computed_at` and
the fixed cron. Neither infers anything about scoring.

### 2.5 Migration mechanics note

`prev_snapshot_id` is added with SQLite-native DDL
(`ALTER TABLE ... ADD COLUMN ... REFERENCES snapshots (id)`). Alembic routes a
column-level `ForeignKey` through `ALTER ... ADD CONSTRAINT`, which SQLite has
no syntax for, and batch mode would rewrite the entire `snapshots` table for
one nullable column. The inline form is what `create_all` produces, so the two
bootstrap paths enforce the same constraint — which
`tests/test_migrations.py::test_migrations_match_models` checks.

---

## 3. Scoring isolation — what is asserted

`tests/test_alert_snapshot_contract.py`:

- `test_golden_fixture_byte_identical` — deterministic 52.43, seeded MC median
  52.5605, IQR (50.0439, 54.9951) unchanged.
- `test_frozen_methodology_hash_unchanged` — artifact untouched.
- `test_typed_snapshot_fields_do_not_enter_scoring` — AST check that no scoring
  module imports the typed contract. The dependency runs one way only.
- Band decomposition, override precedence, coverage suppression, and the
  fail-dangerous "override wins under degraded data" rule are asserted
  independently of any display string.
- Backfill conservatism and Alembic up/down round-trip.

---

## 4. Blockers and open `[PIN]`s carried into Stage 1

These are recorded, not guessed at. Every one keeps its rule **disabled**.

| Item | Rules blocked | Why it cannot be resolved in code |
|---|---|---|
| `regime.score_jump_1r`, `regime.score_trend_7d` deltas | both | no operator artifact defines a material 1-recompute / 7-day point-score move |
| `legs.faber_prewarning` distance | that rule | needs both a `[PIN]` distance and a month-end proximity input that is not currently computed |
| `structure.s3_approach_100`, `structure.s5_percentile_jump` | both | unpinned |
| `dynamics.breadth_downtrend` EWMA/delta | that rule | needs a calibration artifact (§22), which does not exist |
| `vol.skew_extreme` threshold | that rule | unpinned |
| `cal.finra_release` / `cal.ebp_release` materiality | both | unpinned **and** no release-calendar registry exists |
| C-01, C-03, C-10, C-11, C-13, C-14, C-20 | all | unpinned thresholds |
| `credit.leverage_escalation`, `credit.watchlist_stale` | both | there is no typed leverage-watchlist input in this service at all |
| `falsification_tracking_since` | falsification-clock reporting | already `<PIN>` in `frozen_methodology.json` `_meta`; must not be backdated |
| EWMA / CUSUM (Stage 6) | all statistical monitors | no immutable calibration artifact exists |

Additionally: **`busy_timeout` is unset** (`app/db.py`). Stage 1 must set it
before any concurrent alert writer exists, and `GET /api/v1/alerts/health` must
report the effective value.

---

## 5. Explicitly *not* done in Stage 0

No alert tables, no rules file, no evaluator, no delivery path, no LLM change,
no scheduler job, no API surface, no change to the legacy daily digest. Live
feature flags do not exist yet; when they arrive they default off.
