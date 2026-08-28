# VERIFIER_NOTES — panel round log for the open PR (standing regime)

**Standing rule (owner-directed, 2026-08-28):** when a cross-vendor panel
refutation is *disproven*, the proof lives HERE — in the PR diff, where the
next panel round reads it — with `file:line` evidence and a reproducible
check. When a refutation is *upheld*, the fix and its test pin are recorded.
A finding is never ignored: it is either fixed or disproven on the record.
This file is per-PR working state; it resets at merge.

**Anti-backdoor clause (owner-directed, 2026-08-28):** the disproof path is
NOT an escape hatch. The default disposition of every finding is **FIX**.
Before any disproof is recorded, the author must adversarially challenge it:
steelman the finding, assume the verifier is right, and try to construct the
failure it describes. A disproof is admissible only when (a) that
construction is demonstrably impossible, (b) the evidence is a *mechanical,
reproducible check* (a grep, a test, a line number — never an argument), and
(c) no partial validity remains — a finding that is even partially right
takes the fix path (see round 4: literal claim wrong, drift risk real →
fixed). If a disproof is later shown wrong, the finding is reinstated and
fixed with priority.

## PR #95 (content API v0 / shallow status page) — round log

| Rd | Verifier | Finding | Resolution |
|---|---|---|---|
| 1 | SOTA-B | Disclaimer fail-open when only the content fetch fails; `fetchJson` ignores `r.ok`; `charAt(0)==='/'` passes `//host` | **UPHELD ×3 → fixed**: disclaimer gate in `load()`; `!r.ok` throw; `sameOrigin()`. Pinned in `tests/test_frontend_shallow.py` |
| 2 | SOTA-A | Gate not reentrant (failed refresh blanks disclaimer over stale grounded values); `Cache-Control: public` on keyed replies | **UPHELD ×2 → fixed**: last-known-good CONTENT/SLOTS; auth-scoped cache header. Pinned |
| 3 | SOTA-A | Retained sections presented as current after failed status refresh | **UPHELD → fixed**: `REFRESH FAILED - showing data as of <ts>` labeling. Pinned |
| 4 | SOTA-A | "Four-key `_meta` violates mandatory five-field envelope" | **LITERAL CLAIM DISPROVEN — SPIRIT UPHELD.** Proof: `app/schemas.py:13-27` declares exactly four Meta fields; `app/routers/meta.py:66-71` serves the same four; `tests/test_api_contract.py:41,64,66` pins them. "Five" in the docstring counts the five verbatim *epistemic caveats* (`app/references.py:35-41`), not meta fields. The drift *risk* was real → content router now serializes `schemas.Meta` directly (`app/routers/content.py:_meta`) |
| 5 | SOTA-A | Overlapping `load()` calls have no commit ordering | **UPHELD → fixed**: `LOAD_SEQ` token checked after every await. Pinned (`seq!==LOAD_SEQ`) |
| 5 | SOTA-C | "`load()` calls non-existent `renderAudit()`" | **DISPROVEN.** `renderAudit` is *defined* at `app/routers/status.html:219` and *called* at `:414`. Reproduce: `grep -n "function renderAudit" app/routers/status.html` → `219`. The full pytest suite renders the page contract green (2144 passed) |
| 6 | SOTA-A | "Stale blocks/slots can accompany latest status" | **UPHELD (reconciled with round 2) → fixed**: retained content stays (never blanked, per round 2) but is labeled `CONTENT REFRESH FAILED - explanatory blocks shown from an earlier load.` whenever its refresh fails. Pinned |
| 6 | SOTA-C | "`renderAudit` called but `drawAudit` defined; `renderLegs` never defined; page crashes during hydration" | **DISPROVEN.** All three exist: `renderAudit` `:219`, `drawAudit` `:230`, `renderLegs` `:348` (called `:422`). Reproduce: `grep -n "function renderAudit\|function drawAudit\|function renderLegs" app/routers/status.html` → `219 / 230 / 348`. Note to SOTA-C: both rounds' claims assert missing definitions that a single grep refutes — please verify symbol existence against the full file, not a truncated diff window |

| 7 | SOTA-A | "Status commit ordering + partial-failure labeling broken" | **UPHELD (steelman succeeded) → fixed**: (a) the seq guard protected renders but not *state commits* — an older response could overwrite `CONTENT`/`SLOTS` globals after a newer load rendered; fetches now land in locals and commit only behind the seq guard. (b) One staleness flag conflated blocks with slots and mislabeled first-load partial failure as "from an earlier load"; now split (`BLOCKS_STALE`/`SLOTS_STALE`), each set only when *its* refresh failed *and* a previous value is genuinely retained. Both pinned |

| 8 | SOTA-A | 2xx shape-miss unclassified; gate omits status-DOM purge; 60s interval invalidates >60s replies | **UPHELD ×3 (steelman succeeded on all) → fixed**: (a) a 2xx whose body fails shape validation is now classified as FAILURE (`if(!newBlocks) blocksFailed = true`), so retained content gets the stale label instead of silently pairing with fresh status; (b) a content payload commits ONLY if it carries a non-empty disclaimer (response-shape validator), and if the disclaimer is ever unavailable the gate now `purgeGrounded()`s every previously-rendered section before disabling display; (c) `load()` is single-flight with 30s fetch bounds — slow replies commit, ticks never stack, the flight always terminates. All pinned |

## PR #97 (content namespace v1) — round log

| Rd | Verifier | Finding | Resolution |
|---|---|---|---|
| 1 | SOTA-A/B | `gauge_display()` prefix `gauge.display.` vs artifact namespace `gauge.` — score `data.display` empty with the artifact shipped in the same PR, and the unit test masked it with a fabricated slug; banner placeholder `Jan 2026` fabricated a date; `analytics.tail.*` max_len 7 vs regex worst case 8 | **UPHELD ×3 → fixed**: prefix matches the ledger namespace; the deck is pinned NON-EMPTY against the real shipped artifact; banner slots carry the true editorial values; length bound dominates the regex |
| 2 | SOTA-A | Artifact failure coupling; false zero fallbacks; invalid numeric-domain acceptance; unknown-band HOLD default | **UPHELD ×4 → fixed**: (a) a malformed/unreadable artifact degrades to built-ins at version 0 instead of raising into `/score`+`/content` handlers (never-500 invariant); (b) numeric placeholders now carry the TRUE frozen editorial values (tail stats, explosiveness stats, clock values, hedge scores 0.88…0.11) — zero-shaped placeholders fabricated measurements; (c) hedge-score regex tightened to `^(0\.\d{2}\|1\.00)$` — 1.50 was regex-valid but domain-impossible; (d) the band map keyed `derisk` while the real band value is `de-risk` (plus full `suppressed (block degraded)`) — the highest-severity band missed the lookup and `\|\|hold` client patterns would render HOLD; keys fixed and pinned |

| 3 | SOTA-A | Artifact-health validation root-only and exception-incomplete: `blocks: []` serves built-ins while advertising v1; deep-nested JSON raises uncaught RecursionError into request handlers | **UPHELD ×2 → fixed**: deep validation (blocks must be a dict, version an int ≥ 1 — else the WHOLE artifact degrades to v0) and `RecursionError` joins the catch. Both pinned with hostile-file tests. Also adds the `site.disclaimer` canonical alias the companion dashboard's frozen contract requires |

| 4 | SOTA-A | Member corruption retains v1; lone-surrogate payload 500s at response encoding; boolean version emitted | **UPHELD ×3 → fixed**: all-or-nothing member validation (any corrupt member degrades the whole artifact — partial content never served under the artifact's version); whole-artifact UTF-8 round-trip at load (`json.dumps(...).encode` — lone surrogates rejected where the try/except can see them); `type(version) is int` (bool is an int subclass in Python). The loader now returns only the normalized `{content_version, blocks}` pair. All pinned with hostile-file tests |

| 5 | SOTA-A + SOTA-C | Non-finite numbers survive preflight (Starlette's `allow_nan=False` encoder 500s on cached v1 data); nested-item validation container-shallow (`table` holding a string item advertises v1) | **UPHELD ×2 → fixed**: `json.load(parse_constant=reject)` refuses Infinity/NaN at load; `_valid_member` validates every ITEM per kind (links/catalog/table items must be non-empty dicts, list items strings-or-dicts, map values non-empty strings). Both pinned with hostile-file tests |

| 6 | SOTA-A + SOTA-C (convergent) | `1e400` overflows to `float('inf')` via `parse_float`, bypassing `parse_constant`; `json.dumps` default `allow_nan=True` emits it, so preflight passes and the `allow_nan=False` response encoder 500s | **UPHELD → fixed**: the load-time round-trip now runs under the response encoder's own strictness (`allow_nan=False`) — any non-finite float anywhere in the artifact rejects it whole at load. Pinned with a `1e400` hostile-file test |

| 7 | SOTA-B (+A) | The round-2 band-key fix was applied to `gauge.band.oneliner` only — the sibling `gauge.splash.band_blurb` shipped the same `derisk` defect, masked by an instance-scoped test | **UPHELD → fixed class-wide**: the sibling map is re-keyed (its `fallback` entry — "Not scored — inputs degraded." — was the suppressed-state text and now backs both suppressed keys), and the pin test scans EVERY band-shaped map in the artifact for the real band strings, so no future sibling can hide |

## Reviewer guidance for subsequent rounds

- The **disclaimer gate**, **last-known-good + stale labeling**, **commit
  ordering**, and **auth-scoped caching** are deliberate, test-pinned design
  decisions reconciling rounds 1–6. Findings that require *removing* one of
  these properties to satisfy another are answered by this log.
- Everything grounded on the page hydrates from `/api/v1/content/*` or
  `/api/v1/status`; `tests/test_frontend_shallow.py` enforces the banned-literal
  list and the required wiring markers.
