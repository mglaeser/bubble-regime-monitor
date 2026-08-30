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

| 8 | SOTA-A | Raw-depth preflight ≠ wrapped response (near-limit artifact passes raw dump, 500s inside `{data:{...},meta}`); unknown/degraded band still defaults to HOLD in `\|\|hold` clients when a band map carries no correct fallback | **UPHELD ×2 → fixed**: explicit iterative depth bound (≤32, no recursion — decoupled from interpreter limits entirely); every band-shaped map now ships a `fallback` entry carrying the suppressed-state text, and the class-wide pin test requires it — an unrecognized band renders "Not scored" copy, never HOLD |

| 9 | SOTA-A | Suppressed/unknown VERDICT fallback maps to HOLD — the extracted verdict tables faithfully preserved the source generator's own conflated row `"hold (and any unknown band)"` | **UPHELD → fixed at the semantics level**: the conflated rows split into pure `hold` rows plus an explicit `unknown` row ("Not scored — the current reading could not be classified.") in both verdict tables; pinned so no verdict table may bundle unknown with a band or give an unknown row action copy. (The same fix must reach the save-haven source generator in the wiring PRs — noted for PR-1b.) |

| 10 | SOTA-A | v1 completeness unchecked (truncated valid subset serves under v1); stale freshness copy ("≤ 6 weeks old" with July dates self-invalidates); band-map scan detects by `hold`+`trim` presence so a map missing those keys self-excludes | **UPHELD ×3 → fixed**: artifact self-attests `block_count` (mismatch degrades whole, shipped file pinned ≥211); the freshness placeholder states dates and asserts no recency claim (the verbatim BLOCK text mirrors the live site and is replaced by this very slot — noted); the known band maps are pinned by SLUG and must be band-shaped, with the generic scan kept on top. All pinned |

| 11 | SOTA-A + SOTA-C | `lru_cache` froze artifact health at first load (runtime change never re-examined); count-without-manifest lets count-consistent junk serve v1; generic tail regex admits domain-impossible values (999%); the served BLOCK still carried the self-invalidating "≤ 6 weeks old" claim | **UPHELD ×4 → fixed**: mtime/size-keyed cache (health re-examined on any file change at ~stat cost, handlers otherwise I/O-free); `REQUIRED_FILE_SLUGS` anchored in code (junk artifacts fail the manifest); per-stat domain regexes (hit 0–100%, betas signed d.dd, lambda [0,1]); the block's recency claim replaced by a dated statement (the wiring PRs propagate this to the site, which renders from this API). All pinned incl. a runtime-corruption re-examination test |

| 12 | SOTA-A + SOTA-C | Cache key (mtime,size) spoofable by timestamp-preserving copies; required slugs unchecked for KIND; cache publication non-atomic under threads; blocks/version read twice per response (torn pairing); `100.99%`/`1.50` regex-legal; CI pins can short-circuit the loader | **UPHELD ×6 → fixed**: cache keyed (mtime_ns, size, inode) — atomic-rename replacement always changes inode; in-place all-three-preserved rewrite is a filesystem-level act outside this control's threat model, on the record; `REQUIRED_FILE_SLUG_KINDS` validates kind, not just presence; cache published as one tuple under a lock (double-checked); `dashboard_payload()` builds each response from ONE artifact snapshot (call-counted pin); hit bounded to 0–100%, lambda to [0,1]; a pin now runs the REAL loader end-to-end on the shipped file. All pinned |

| 13 | SOTA-A | Cache key-to-file binding (stat-then-open TOCTOU: a rename between the two caches one file's content under another's key, and a quick revert makes the mismatch stick); artifact schema still slug-porous | **UPHELD ×2 → fixed**: key and content now bind to the SAME open file descriptor (`fstat` on the handle the loader reads from); slug-shape validation added (`^[a-z0-9_.\-]{1,120}$`) — which immediately caught a real defect in the shipped artifact itself (a slug carrying a leaked extractor note, now renamed `atlas.aggregate.stats`). The control proved itself on first contact. Both pinned |

| 14 | SOTA-A + SOTA-C | Band maps declare `suppressed`/`fallback` keys the verdict tables have no rows for (those states resolved to generic copy); `_placeholder` extractor scaffolding shipped in served tables; stat-tile editorial undated in machine-readable form | **UPHELD ×3 → fixed**: verdict tables gain `suppressed` rows (block-degraded copy) and `fallback` rows (unknown copy) — band vocabulary is CLOSED across all band-keyed structures, pinned by a closure test (map keys ⊆ table rows); all `_placeholder` scaffolding stripped from every served table, pinned; `atlas.aggregate.stats` carries machine-readable `as_of: 2026-07` + a note pointing live values to the metrics feed |

| 15 | SOTA-A | "vN integrity, freshness, slug, CI gates" — four class names, no construction | **PROOF OF PRIOR RESOLUTION** (anti-backdoor clause satisfied: steelman attempted per class, no concrete failure constructable; B and C verified the same controls in detail this round). Evidence per class: **vN integrity** — `block_count` self-attestation + `REQUIRED_FILE_SLUG_KINDS` manifest + all-or-nothing member/item validation (`app/content_registry.py:_load_artifact`), pinned by `test_truncated_artifact_fails_completeness_attestation`, `test_junk_artifact_with_matching_count_fails_required_slugs`, `test_required_slug_with_wrong_kind_degrades`, `test_member_corruption_degrades_whole_artifact`. **freshness** — no served text asserts recency (rounds 10/11/14 fixes); THIS round additionally stamps machine-readable `as_of: 2026-07` on all 13 dated editorial blocks (pairs, verdicts, methods, phases, experts, stats). **slug** — `_SLUG_RE` schema (round 13), pinned by `test_malformed_slug_degrades_whole_artifact`; every shipped slug conforms (`jq -r '.blocks\|keys[]' \| grep -vE '^[a-z0-9_.\-]+$'` → empty). **CI gates** — `test_shipped_artifact_passes_the_full_loader` runs the real loader end-to-end on the shipped file (round 12). If a concrete instance in any class exists, name it and it will be fixed under the fix-first default |

| 16 | SOTA-A + SOTA-C | Runtime loader weaker than the round claims: closure/completeness lived in CI pins only (a 6-block artifact serves at v1); the charset slug regex passes degenerate slugs — and the shipped artifact carried two (`…r1..24`, `…rule1..rule4`) | **UPHELD ×3 → fixed, and round 15's proof CORRECTED on the record** (the anti-backdoor clause's reinstate-with-priority case: my "no construction possible" was wrong for the slug class — A's word had a live instance the loose regex passed). Fixes: `_MIN_BLOCKS = 200` floor and `_bands_closed()` closure now fire IN THE LOADER, not only in CI; the slug regex is structured (dot-separated non-empty segments — `..`, leading/trailing dots, bare prefixes all rejected); both degenerate shipped slugs renamed. All pinned with hostile-file tests |

| 17 | SOTA-A + SOTA-C | (a) Unchecked band hashing: an unhashable `band` value raised TypeError out of `_bands_closed`'s set build, past the load-time try/except — every artifact-backed route 500s; (b) incomplete band closure: closure was relative to whatever the oneliner self-declared, so a v1 artifact could omit the de-risk copy from every band block at once, and `gauge.splash.band_blurb` was entirely unvalidated at runtime (C convergent: `\|\|hold` clients render HOLD for the highest-severity band); (c) stale recency assertion: frozen editorial asserting calendar recency ("right now (≤ 6 weeks old)", "Today's surge") served with no freshness marker; (d) test preconditions short-circuit: the hostile-file battery's tiny fixtures predate `_MIN_BLOCKS`/`REQUIRED_FILE_SLUG_KINDS` and degrade for several reasons at once — loader controls stayed CI-green when removed | **UPHELD ×4 → fixed** (verified by control-deletion audit, see below): (a) band-table rows are str-checked BEFORE any hashing; a non-str band is corruption and degrades the artifact whole — no code path from artifact content to an exception remains; (b) the band vocabulary is CANONICAL in code (`_CANONICAL_BANDS`: the `models.py:61-65` action states + degraded-suppression/fallback sentinels + `unknown`) — every band map (oneliner AND splash) must cover all of it, every verdict table must cover it plus any extra declared band; the shipped maps gain `unknown` entries ("Not scored" copy, no action framing); (c) all 24 calendar-anchored blocks now carry `as_of: 2026-07` (37 stamped total), and a pin test enforces: any block matching the recency lexicon must be stamped or on the reviewed LIVE-referential allowlist (8 gauge blocks whose "now" refers to the live payload they ride); (d) the battery is rebuilt on a baseline that passes EVERY loader control, one violation per test, with a positive-control test pinning the baseline itself — plus new single-violation tests for min-blocks, wrong-kind, junk-slug, splash closure, coherent-omission (A's exact bypass), extra-declared-band, and unhashable-band. Control-deletion audit run locally: removing `_bands_closed` → 5 red; reverting the str-guard → red; dropping `_MIN_BLOCKS` → red; dropping `block_count` attestation → red; unstamping one dated block → red |

| 18 | SOTA-A (B + C approve) | Full ledger: (1) count-only completeness — five required slugs + 200 junk fillers serve v1 with all 206 remaining editorial blocks missing; (2) verdict-row schema unchecked — a band-only row serves v1 with no `template`/`trend_broken`, clients render empty verdicts; (3) `gauge.reg.s5` ("Today's spreads are near 25-year tights") exempted as live-referential on the as_of allowlist — a frozen market fact served under every score snapshot undated | **UPHELD ×3 → fixed** (control-deletion audit re-run, incl. one fixture I had to correct: the missing-template row also lacked its trend selector, so deleting the template control stayed green until the fixture was made single-violation): (1) completeness is the FULL code-anchored manifest (`app/content_manifest.py`, all 211 slugs WITH kinds, generated from the shipped artifact and pinned `manifest == shipped` exactly) — the round-16 `_MIN_BLOCKS` floor is strictly subsumed (manifest coverage ⇒ ≥211 blocks) and removed; A's exact fixture (5 band slugs + 206 count-consistent fillers) is now a hostile test, as is a single missing editorial slug; (2) verdict rows validate their full schema in the loader — `band` str + non-empty `template` + `trend_broken` ∈ {yes,no,any}; the case-keyed distance table's rows likewise (non-empty `case` + `template`) — each clause with its own single-violation test; (3) reg.s5's claim is dated in copy ("As of Jul 2026, spreads were near…") and stamped `as_of: 2026-07`; it is off the allowlist, and the remaining 7 allowlisted blocks were re-reviewed after the miss (each "today/now" is genuinely live-referential). Deletion audit: manifest loop → 2 red; manifest −1 entry → 5 red; template clause → 1 red; trend clause → 2 red; distance schema → 1 red |

| 19 | SOTA-A (B + C approve) | (1) `trend_broken` hashed before type check — `x in frozenset` hashes x, so a valid-JSON `[]` raised TypeError into score/dashboard/dynamic (the round-17 band crash class, REINTRODUCED by round 18's own schema fix one line below the guarded site); (2) `atlas.matrix.legend` ("… current institutional view only") served undatable frozen guidance — bare "current" was not in the round-17 recency lexicon | **UPHELD ×2 → fixed**: (1) isinstance-str before membership; hostile test pins that an unhashable trend selector degrades and never raises (deletion audit: bare-membership revert → red). The registry was re-swept for other artifact-value hashing sites: none remain (band and trend are the only artifact scalars that reach a hash-based operation, both now type-guarded first). The lesson is logged: every new validation clause must be written against hostile types, not just hostile values; (2) legend stamped `as_of: 2026-07`; the pin lexicon widened to `current(ly)?` — which pulled `gauge.verdict.detail` into scope (live-referential "the current reading", same justification as `lead`, added to the reviewed allowlist); unstamp audit → red |

| 20 | SOTA-A (C approves; B errored infra-side) | (1) verdict closure ignores TREND coverage — an artifact stripped of every `yes` row loads at v1, so a fired hold/trim/de-risk state has no truthful template; (2) `atlas.matrix.cash.ai2026` ("The live phase-1 haven… ICI, 1 Jul 2026") serves frozen guidance with no `as_of` and no recency-lexicon word — it evades the scan | **UPHELD ×2 → fixed**: (1) per-band trend coverage in the loader — every band must resolve for both trend states (an `any` row, or `yes` AND `no` rows); hostile test = A's exact scenario (action band with only `no` rows degrades); deletion audit → red; (2) the class, not the instance: the `ai2026` column IS the frozen present-cycle assessment, so ALL 18 family members are stamped and pinned by a family rule (`ai2026` in slug ⇒ `as_of`, lexicon or not); a full `2026`-text sweep also stamped `classification.universal.4`/`falsified.0` and the self-dating Jul-2026 backfill/badge copy (20 newly stamped, 59 total); citation-only years (key_sources, reg.s4's "2026 study", epistemic caveats) stay timeless on review; unstamp audit → red |

| 21 | SOTA-A (B + C approve, both high confidence) | The gold-lead clock placeholder served the frozen Jul-2026 Granger window as deictic relative time ("now -> +19 mo") on `/content/dynamic` — after Jul 2026 every request silently moves the implied market-peak window forward, machine-undatable | **UPHELD → fixed at class level**: (1) the placeholder is absolute ("Jul'26 - Feb'28" — the analysis month plus its own +19-month bound); (2) `DynamicSlot` carries `as_of`, emitted on the wire — ALL 38 frozen-editorial placeholder slots (banner trio, tail stats, explosiveness rows, clock values, hedge scores) stamped `2026-07`; (3) standing pins: no dynamic placeholder may use deictic time (now/today/ago/from now), and any non-pending editorial placeholder must be machine-dated. Deletion audit: deictic revert → red; unstamped clock group → red |

| 22 | SOTA-A (B + C errored infra-side) | Six defects, five convicting the battery of violating its OWN round-17 single-violation contract — fixtures written in rounds 17-18 now also trip the distance-row/trend schema added in rounds 18-20, so deleting the control each test pins stays CI-green (nested-item, Infinity/1e400, depth, wrong-kind ×2 incl. an unmigrated round-12 fixture, unhashable-band missing its trend selector); plus the round-16 slug rewrite silently DROPPED round 13's frozen {1,120} length cap. A pre-refuted both available defenses (guard-order argument; intentional-supersession argument) against the frozen round-13/16 records | **UPHELD ×6 → fixed**: length cap restored via lookahead (120 loads, 121 degrades, pinned); all defective fixtures moved onto an ORDINARY manifest table (`analytics.fans`) or completed to true single-violation shape; the round-12/13 stragglers rebuilt on the baseline. The audit itself was the rot: re-ran control-deletion for ALL 14 loader controls against the repaired battery — 13/13 red (manifest → 5, roundtrip/member/slug-regex → 2 each, rest → 1). The 14th, `parse_constant`, stays green under deletion because the round-6 strict round-trip subsumes it — recorded honestly as deliberate redundancy, not fabricated as pinned. STANDING RULE: the deletion audit re-runs over the FULL control set whenever a loader control is added, since new guards can silently multi-violate old fixtures |

| 23 | SOTA-A (B + C approve; loader controls now uncontested) | Pure content ledger: (1) `playbook.methods` carries M0-M10 = ELEVEN rows while the intro copy says "Ten ordered screens"/"ten-screen checklist" — wrong methodology count, faithfully extracted from a defect in the source site itself; (2) "challenged since April 2025" (classification.universal.1), (3) "Leverage is at a record" (gauge.reg.d2), (4) the frozen hedge-label map `gauge.series.suffix` ("— feared market", "— challenged") — all served undated | **UPHELD ×4 → fixed**: copy corrected to "Eleven ordered screens (M0-M10)"/"eleven-screen checklist" with a count-consistency pin (row ids M0..M10 == 11 must agree with the copy; the SOURCE-SITE defect at dashboard.tsx:1130-1132 is flagged for the wiring PRs alongside verdictOf); all three blocks stamped `as_of: 2026-07` (62 total); the recency lexicon gains frozen-STATE markers (`at a record|record high|all-time|since <word> 20xx`) — post-widening sweep shows zero unstamped matches; `gauge.series.suffix` pinned by a frozen-label rule (the gauge-page twin of the ai2026 family, invisible to any lexicon). All remaining unstamped `gauge.reg.*` glosses read one-by-one: timeless methodology questions, on the record. Deletion audit: unstamp d2 → red; unstamp series.suffix → red; revert Eleven→Ten → red |

| 24 | SOTA-A single defect (B + C approve, high confidence, both attest the full loader chain) | `analytics.bsadf.expl` carries the measured Jul-2026 BSADF verdict ("exactly on the borderline — some runs flag it, some don't") with no `as_of` and no lexicon word — clients cannot stale-label the bubble-alarm verdict | **UPHELD → fixed at family level**: the ENTIRE `analytics.*` namespace describes the frozen Jul-2026 battery run (its own intro says so), so all 5 remaining unstamped members are stamped (`intro.methods`, `fans.expl`, `fans.analogs`, `bsadf.expl`, `hedgeweight.formula` — 67 stamped total) and a family rule pins it exactly like `ai2026`: `analytics.` prefix ⇒ machine-dated, lexicon or not. Census floors in pins are now set from MEASURED counts (a guessed floor mis-fired twice this loop, on the record). Deletion audit: unstamp bsadf → red |

| 25 | SOTA-A + SOTA-B + SOTA-C — **PANEL APPROVED** (A refuted=False high, B approved, C timed out); then a rebase onto main (#99 panel-workflow change, no file overlap) retriggered the gate and the fresh round found: `os.fstat` sat OUTSIDE the `try` that guarded `open()` — an fstat failure on the descriptor (EIO on a failing mount, revoked fd) escaped into score/dashboard/dynamic as a 500 | **UPHELD → fixed, and the CLASS swept**: the guard now spans the entire descriptor lifetime — open, fstat, load, AND the implicit `close()`, which can raise EIO just as well (sibling found by sweeping A's class, not reported); three pins (registry-level, route-level 200s, close-failure) all go red when the guard is narrowed back. Every filesystem-level fault degrades to built-ins at v0; the artifact path now has no unguarded I/O site (open/fstat/json.load all inside the same except). NOTE for the record: the approving round and this refuting round reviewed IDENTICAL content — the panel is a sampling process, so a clean verdict is a floor on quality, never a proof of absence; the fix stands on its own merit |

| 26 | SOTA-A (B approves; C timed out) | Transient read EIO cached under a STABLE stat key: `_load_artifact` caught `OSError` internally and returned a degraded artifact, which `_file_artifact` then cached under the file's (mtime,size,inode) key — recovery does not change that key, so a single I/O blip pinned v0 / an empty gauge deck until the artifact was rewritten or the process restarted | **UPHELD → fixed**: `OSError` is removed from the inner catch so an I/O fault propagates to the descriptor guard and caches under the **None** key — the next request re-reads, and recovery alone suffices with no operator action. Content faults keep caching under the real key (deterministic; re-parsing corrupt bytes every request would be waste) — both halves pinned. **The round-22 standing rule earned its keep this round**: the mandatory full-control re-run caught MY OWN new test as vacuous — the first draft monkeypatched `_load_artifact`, bypassing the very except clause it claimed to pin (restoring `OSError` there left CI green). Rewritten to inject the fault at the real `fh.read()` boundary; the mutation now goes red. Also recorded honestly: dropping the explicit `_cache = (None, …)` invalidation leaves CI green because a fault short-circuits before the key comparison — defensive clarity, not a pinned control |

| 27 | SOTA-A (B approves; C timed out) | The v1 manifest was enforced as a SUBSET — "every declared slug is present with its kind" — so an artifact carrying EXTRA undeclared blocks loaded at v1 and was published by `/content/dashboard`, and under a `gauge.` prefix rode `score`'s display deck: injected content bypassing every review pin (as_of stamps, family rules, band vocabulary — all of which are CI pins against the SHIPPED artifact, not runtime guards) | **UPHELD → fixed**: the served block set must now EQUAL the manifest (`blocks.keys() != REQUIRED_FILE_SLUG_KINDS.keys()` degrades whole), consistent with the round-18 framing that the manifest *is* the meaning of v1 completeness and must be extended in the same PR that adds content. Two pins: an injected editorial block degrades the artifact; an injected `gauge.` block never reaches the display deck. Mandatory full-control re-run (round-22 rule) — 12/14 red, and it confirmed the predicted consequence: the artifact-side **slug regex is now subsumed** by equality (a non-manifest slug can no longer appear) and stays green under deletion. Rather than claim a pin it no longer earns, the term is retained as defense-in-depth, documented as subsumed in-line, and the schema is pinned where it still bites — `test_manifest_slugs_are_structurally_valid` over the manifest itself, which is code and could otherwise canonicalize a malformed slug. The 120-char cap stays independently red (pinned as a unit test on the regex). Subsumed-but-retained controls now stand at two, both on the record: `parse_constant` (round 22) and this |

| 2 | SOTA-A ×4 (B approves, high confidence; C timed out) | Four validator escapes, all operator-visible: (1) a leading adverb walks past the sentence-START imperative anchor — **"Now hold positions."** returns OK; (2) banned phrases matched a single literal space, so **"Market will  crash."** passes; (3) the trailing-dot exponent **"51.e2"** splits into the grounded `51` plus the grounded `2` while denoting 5100; (4) **bidi controls pass** — `U+202E 51 U+202C` carries grounded digits and satisfies the grounding check yet RENDERS to the operator as `15` | **UPHELD ×4 → fixed.** (1) the imperative test no longer depends on WHERE the verb sits but on whether it takes an OBJECT — the state sense is always followed by punctuation or a continuation word ("band is hold,", "moved hold to trim"), and those are pinned so the gate cannot start rejecting the monitor's own vocabulary; (2) banned phrases are whitespace-flexible; (3) exponent forms are matched first and whole, including the trailing-dot spelling, with the plain branch second so a sentence-final `51.` still yields `51`; (4) **every Cf format character is refused** except VS16 and ZWJ, which are legitimate inside an emoji sequence — the ungrounded value here was assembled purely from display order, so a grounding check on code points alone could never catch it. Deletion audit: all four go red when reverted |

| 3 | SOTA-A ×4 (B and C both errored infra-side) | (1) `"now"` was exempted as a non-object, so **"Reduce now."** validated as an observation; (2) U+200D was allowed globally, so a joiner between letters renders **"Sell holdings"** to the operator while matching neither the lexicon nor the imperative gate; (3) the numeral scanner is ASCII-only — U+2212 made `\u221251` read as the grounded `51`, and `\u00bd` carries a value with no digits at all; (4) in `consecutive_technical_errors` the LIMIT was applied BEFORE in-flight rows were skipped, so a burst of unresolved claims filled the scan window, hid the strike run, and the breaker permitted ASK | **UPHELD ×4 → fixed.** (1) the verb sets are now split by what they MEAN here: only `hold`/`trim` are band names and need the object test, while `sell|buy|reduce|exit|increase|…` are never states of this monitor, so any occurrence is advice — no adverb exemption to abuse; (2) joiners are permitted only when genuinely adjacent to an emoji, refused between letters; (3) non-ASCII digits, `No`/`Nl` numerics and unicode minus signs are refused outright — none can be grounded against ASCII facts, and the deception is in the glyph rather than the value; (4) the in-flight exclusion moved INTO the query, so the limit counts resolved rows as intended. Deletion audit: all four go red. **Standing observation for later rounds:** every escape in rounds 2-3 has the same shape — text that RENDERS as one thing and MATCHES as another (bidi, joiners, unicode numerics, spacing). New validation should be written against that class, not against individual strings |

| 4 | SOTA-A ×5 + SOTA-C ×2 (B approves) | (1) advice-regex grammar gaps — **"Hold 2 positions."** (object starts with a digit) and **"Consider selling."** (advice with no imperative verb) both return OK; (C) `_NON_OBJECT` contained the prepositions `on`/`in`/`at`/`to`, so **"Hold on tight."** and **"Hold in line."** pass; (2) **no English-only gate exists at all** — "Bitte kaufen." validates on both channels, against ruling Q30; (3) arithmetic escapes grounding — facts 51 and 2 admit **"51*2"**, denoting an ungrounded 102; (4) Mn/Me screening incomplete — **VS15 hides a letter**, plus a keycap claim; (5) the content cap trusts the caller's `iteration`, so a fourth attempt passing `iteration=1` gets a fresh allowance | **UPHELD ×6 → fixed; one cited case did NOT reproduce and is pinned instead.** The advice gate is **inverted from a deny-list to an ALLOW-list**: rounds 2-4 each defeated the deny-list because it must enumerate every way English attaches an object, while the STATE sense has only a few shapes ("band is hold", "moved hold to trim", band verb followed by punctuation). Anything else containing a band verb is advice, and `sell|buy|reduce|…` are advice unconditionally including gerunds. (2) an English backstop word-list (German-weighted — the phrase set this programme replaces was German); the prompt remains the primary guarantee, this is the net. (3) arithmetic between digits refused. (4) **the screen now covers Cf AND Mn/Me: VS16 is category `Mn`, not Cf, so the round-2 allowance for it inside a Cf-only scan was DEAD CODE** and VS15 sailed through — the real defect was mine, one round older than reported. (5) the cap is derived from rows via `content_attempts()`, with the caller's count still honoured when larger. **Not reproducible:** the keycap half of defect 4 — two keycaps already fail the allowlist and three fail the count; verified by executing the exact scenario, and pinned so it stays true. Deletion audit: all six go red, the allow-list turning SIX tests red |

| 6 | SOTA-A ×4 + SOTA-C (B approves) | (C) **VS16 allowed unconditionally** — "Se\ufe0fll holdings." passes iMessage validation and RENDERS as "Sell holdings.", delivering unapproved advice on a live channel; (A1) `RF4_ALL_CLEAR`'s fallback asserts breadth recovery, but the flag can clear on index distance alone; (A2) a fallback never CLOSED a compose, so an exhausted trigger stayed capped and fallback-only forever; (A3) `.51` lost its leading dot and read as the grounded `51`; (A4) forecast/modal verbs absent — "Market will fall." validated (the lexicon caught only "will crash") | **UPHELD ×5 → fixed.** (C) the guard now asks whether the COMBINED glyph is allowlisted — the naive "base is not a letter" test fails both ways, since U+2139 IS a letter and the base of 'ℹ️'. ZWJ had been guarded this way since round 3; allowing its sibling unconditionally was my own inconsistency, and the reasoning in the comment ("it only makes a glyph more visible") was simply wrong. (A1) the fallback now claims only that the flag no longer meets its trigger definition. (A2) a `FALLBACK_USED` outcome closes the compose. (A3) leading-dot decimals are matched first and whole. (A4) forecasting is advice in the other direction and is refused. **Two defects the panel did NOT report, found by the new prompt-library contract test:** (i) the round-5 arithmetic guard banned `/` between digits, which would have REJECTED THE OPERATOR'S 08:00 DIGEST — its fallback is `{median}/{score_scale_max}`, i.e. "51/100", the digest's own score notation; `/` now counts only when spaced; (ii) **10 of the 32 prompts instructed the model to use an emoji set the validator rejects** (`🔸`/`🗓` vs the contract's `▪️`/`🕒`/`ℹ️`) — an obedient model would have been format-rejected every time, burning content iterations before falling back. All aligned, with a test asserting prompt text == channel contract == validator. **The deletion audit again caught what the panel could not:** three of the five fixes had NO tests, so reverting them left CI green — now pinned, and all seven controls go red |

| 7 | SOTA-A ×3 + SOTA-B ×3 (convergent) | **My round-6 claim "prompt text == channel contract == validator, all aligned" was FALSE.** The round-6 fix replaced ONE exact string and the contract test matched ONE phrasing (`allowlist: …`), so three other phrasings survived across 14 prompts — `✅` in BAND_TO_HOLD/OVERRIDE_RESOLVES, a `📊 📅 🔎 ⏱ 🔧 ✅` "neutral set" in twelve more, and two prompts that shipped **literal `\u{1F4CA}` ESCAPE TEXT** in place of emoji. B spelled out the cost: FORMAT_REJECTED rows count toward `content_attempts`, so an obedient model's compose deterministically exhausts to the fallback while spending budget and pacing slots. Also (A1) the context-free terminal exemption accepted **"Now hold."**, and (A2) grounding was digit-only, so **"Score ninety-nine."** passed | **UPHELD ×3 → fixed.** Every phrasing normalised to the single channel allowlist (16 fields across the library), and **the contract test now scans actual emoji CHARACTERS rather than a phrase pattern** — the phrase-matching version is exactly why the round-6 claim was wrong — plus a new test forbidding literal escape text. (A1) the terminal shortcut is gone: a terminator is necessary but never sufficient, something must MARK the state. (A2) spelled-out numbers are refused, with `one`/`two`/`second` deliberately excluded as ordinary English. **Fixing A1 broke the operator's own digest** — `bubblegauge 51/100 trim.` puts a band name after a score with no marker — so a preceding NUMBER now counts as state context. That is the SECOND time in two rounds that hardening nearly blocked the 08:00 digest; the risk in this validator is not what it rejects deliberately but what it rejects by accident, and the prompt-library contract tests exist to catch precisely that. **Process note:** I restored the library from the pristine copy mid-round after a bulk edit damaged it (stripping emoji left sentences like "Emoji allowlist adds because…"), which silently reverted the round-6 RF4 fix — caught by its own test, re-applied |

| 8 | SOTA-A ×2 (**B and C both approve**) | (1) bare `to` counted as state context, so **"Remember to hold."** validated as a marker-backed state; (2) U+FF0B (fullwidth plus) was absent from the operator checks — **`51＋2`** validated while denoting 53 | **UPHELD ×2 → fixed.** (1) `to` earns the marker role only inside a transition — a band word or a movement verb must precede it ("moved hold TO trim") — which keeps the real construction while closing the imperative; (2) the sign test is now **category-driven** (`Sm` and not ASCII) exactly as the dash test became in round 5. **This is the third time enumerating Unicode was wrong** (U+FE63 round 5, U+FF0B round 8), so the class is closed rather than the instance; ASCII operators stay with the arithmetic gate, which is what distinguishes the digest's "51/100" from "51 / 2". Deletion audit: both go red. **Convergence:** defect counts across the loop are 8 → 4 → 4 → 7 → 9 → 5 → 3+3 → 2, and this is the first round where both non-required verifiers approved |

| 9 | SOTA-A ×4 + SOTA-C (fail-open) | (C) **`reserve()` has no reconciliation**: a worker that dies mid-call leaves its claim IN_FLIGHT forever, and the two halves of the governor then disagree about it in the WORST direction — `spend_today` COUNTS it (the daily budget leaks) while the strike scan SKIPS it (the technical errors that killed the worker never register, so the breaker cannot open). Fail-open plus a silent budget drain. (A1) a non-ASCII separator assembles a third value from two grounded ones — `51\uff0e2` displays 51.2; (A2) `zero` missing from the spelled-number gate; (A3) a band verb's GERUND evaded every advice gate — "Keep holding your positions." validated; (A4) FORMAT rows exhaust the iteration cap but never struck, so a model returning malformed output forever fell back with the breaker shut | **UPHELD ×5 → fixed.** (C) `reap_stale_claims()` resolves any claim older than a 900 s TTL into the technical error it almost certainly was, and `decide()` runs it BEFORE reading any state, so an abandoned claim now reaches the breaker instead of hiding from it. (A1) a non-ASCII separator between digits is refused (ASCII `.`/`,` stay inside `_NUMERAL_RE`, attached to their numeral). (A2) added. (A3) band-verb gerunds and `keep|continue|start|stop|begin + …ing` are advice — a state is NAMED, never performed. (A4) ruling Q38 counts an EXHAUSTED ATTEMPT, and format rejections exhaust the cap exactly as content rejections do; both now accumulate toward one strike. Deletion audit: all five go red |

| 10 | SOTA-A ×4 + SOTA-C ×2 | (A1) reaping runs INSIDE `reserve()`'s savepoint, so a non-ASK verdict rolls it back and restores the abandoned claims just recognised as failures — the breaker then reports closed; (A2) every reaped claim was stamped `now − TTL`, so a just-expired failure skipped its backoff while a day-old one started a fresh ~24h cooldown; (A3) **`de-risk` was in NEITHER the band list nor the action list** — "De-risk your portfolio." validated as an observation; (A4) the arithmetic class matched exactly ONE operator, so `51**2` passed while denoting 2601; (C1) `trimmed`/`held` sat in the ACTION verbs, so **"The band was trimmed." was REJECTED** — a false positive I introduced in round 5; (C2) "The score is 51." was claimed to fail grounding | **UPHELD ×5 → fixed; C2 NOT REPRODUCIBLE.** (A1) `reserve()` reaps BEFORE opening the savepoint, so the reconciliation outlives a rolled-back claim. (A2) each claim now ends at its OWN expiry (`started_at + TTL`). (A3) `de-risk` joins the band vocabulary and takes the object test — the highest-severity band was the one place an instruction would carry most weight. (A4) one-or-more operators. (C1) past participles of band names belong to the STATE vocabulary; the imperative uses stay caught by the passive-framing pattern and the object test. (C2) executed exactly as cited — it validates, and `test_sentence_final_period_does_not_break_grounding` already covered it; now pinned explicitly as non-reproducible per the disputed-finding protocol. Deletion audit: all five go red. **Note:** A1 and A2 were both consequences of my own round-9 fix — a new control's INTERACTIONS need auditing, not just the control |

| 11 | SOTA-A ×3 (B and C approve) | (1) `"(?:ing)?"` on an e-ending verb only ever produces *reduceing*, so **"Try reducing positions."** validated; (2) the `/` rule demanded spaces on BOTH sides, so **"51 /2"** evaded it while denoting 25.5; (3) **historical strikes were regrouped against the CURRENT cap** — widening 3→4 turned five exhausted composes into three strikes and REOPENED a breaker that had legitimately tripped | **UPHELD ×3 → fixed.** (1) gerunds are spelled out, not derived by suffix concatenation. (2) `/` is arithmetic whenever EITHER side is spaced; only the tight `51/100` digest notation stays exempt. (3) strikes are delimited by the engine's own `FALLBACK_USED` marker — the row where it records giving up, which is precisely ruling Q38's "exhausted attempt" and which no cap can re-interpret. **The first fix was incomplete and my own test caught it:** the scan WINDOW was still sized from the cap, so NARROWING it shrank the window until a tripped breaker looked closed — the same defect one layer down, which would have shipped had I only tested the widening direction. The window is now a documented constant (`_STRIKE_SCAN_ROWS`). A structural consequence: `FALLBACK_USED` must be visible to the strike scan but must NOT pace the next request (no model call happened at that step), so the scan has its own outcome set rather than reusing `_PACING_OUTCOMES`. Deletion audit: all controls go red, including both directions of the cap change |

| 12 | SOTA-A ×4 (C approves) | (1) `BUDGET_SKIPPED` RESET the strike run despite making no request — four errors, a skip and a fifth error left a five-strike breaker closed; (2) the **fixed 500-row scan window** cannot serve a larger threshold, so 501 consecutive errors reported the breaker closed; (3) bare `move`/`shift` counted as transition context, so **"Move to trim."** validated as a state report; (4) the tight-slash exemption applied in EVERY context, so **"The quotient is 51/2."** passed as a computed value | **UPHELD ×4 → fixed. Two are recurrences of classes I had already "closed".** (1) a budget skip is neither a strike nor evidence of recovery — it is now skipped, matching the reasoning that keeps it out of the pacing floor. (2) last round I made the window a CONSTANT to stop the iteration cap rewriting history; that was wrong in the other direction. The window is now `max(floor, threshold × rows-per-strike)`: it may depend on the BREAKER THRESHOLD (which sets how many strikes must be visible) but never on the ITERATION CAP (which would re-interpret history), and the maximum keeps it monotonic so lowering the threshold cannot shrink it. (3) only INFLECTED movement verbs describe something that HAS happened; the bare imperatives are out. (4) the tight `a/b` exemption exists for the digest alone and now requires the denominator to be a DECLARED SCALE — a fact whose name says maximum/total/count — which the digest template provides and an arbitrary quotient does not. Deletion audit: all four go red. **Two of my own fixtures were also wrong** (no declared scale; failures placed 33h back where the cooldown had legitimately expired) — both corrected, and neither would have been caught without running the audit in both directions |

| 13 | SOTA-A x4 (C approves) | (1) `BUDGET_SKIPPED` rows were skipped in PYTHON, so they still filled the query's LIMIT - 600 of them hid five real strikes and `decide` permitted ASK; (2) a bare `recommend` match missed its inflections, so **"Cash is recommended."** validated; (3) U+001C..U+001E (file/group/record separators) are line breaks a CR/LF/NEL list misses - a multiline iMessage validated; (4) **`51//100`** used the digest's scale exemption to launder floor division | **UPHELD x4 -> fixed. Defect 1 is the IDENTICAL mistake round 9 fixed in the same function:** filtering after the LIMIT means excluded rows still consume window slots. Round 9 moved the `IN_FLIGHT` exclusion into the query for exactly that reason; round 12's budget-skip fix then reintroduced it as a Python `continue`. Now excluded in the query. The standing rule earns a sharper statement: **a row that must not affect the answer must not occupy a slot in the window either.** (2) `recommend\w*`; (3) the C0 separators join the line check; (4) the scale exemption requires EXACTLY ONE slash - a declared scale must not launder an operator. Deletion audit: all four go red |

| 14 | SOTA-A x3 (B and C approve) | (1) the state-marker regex matched a word SUFFIX - the alternative `at` matched the tail of "Repeat", so **"bubblegauge: Repeat de-risk."** read as a marked state; (2) `count` was accepted as a SCALE name, so a shown red-flag count of 2 legitimised **"51/2"** as a score; (3) the strike window ignored iteration WIDTH - a compose costs a row per iteration plus its fallback marker, so five 125-reject composes need 630 rows and were counted as four strikes | **UPHELD x3 -> fixed.** (1) `\\b` on both marker searches: a marker must be a whole word. (2) the digest divides BY A TOTAL, never by a count - `red_flag_count/red_flag_total` - so `count` is out of the scale-name pattern; a live counter must not become a denominator. (3) `per_strike` now takes the maximum of the row budget and `cap + 1`. **Depending on the cap for SIZE is safe where depending on it for GROUPING was not (round 11):** every term only ever WIDENS the window and the floor means no config change can shrink it, so history stays un-reinterpretable. Deletion audit: all three go red. **Two self-inflicted stumbles:** a double-escaped `\\b` briefly broke 32 tests, and I repeated round 12's fixture error of placing rows outside the cooldown window - both caught by the suite before push |

| 15 | SOTA-A x3 + SOTA-C (B approves) | (1) the strike window SHRINKS with the current iteration cap - lowering 125 to 3 hides the fifth historical strike and permits ASK mid-cooldown; (2) a non-overlapping slash scan misses **chained** division, so `51/100/100` validated; (3) the advice deny-list omits plain imperatives such as **"Dump your portfolio."**; (C) the scale exemption has **no numerator/denominator PAIRING check** - "Score 51/4" passes on a median of 51 and a red-flag total of 4, denoting 12.75 | **UPHELD x4 -> fixed. Defect 1 is the SAME CLASS for the third round from a third direction** (sized by cap r11 - history re-interpretable; fixed at 500 r12 - large thresholds unreachable; widened by cap r14 - lowering it shrinks again). Every settings-derived window is wrong in one direction, so the dependency is GONE: `consecutive_strikes` reads only the rows since the last SUCCESS, because a success is the one thing that resets a run, and the constant is demoted to a pure safety valve. (2) a chain is never a score. (3) the deny-list grew, and the gerund list with it. (C) only the pairings the DIGEST WRITES are a score - `(median, score_scale_max)` and `(red_flag_count, red_flag_total)` - because a bare denominator check let any grounded numerator ride any declared scale. Deletion audit: all four go red. **Also fixed: a genuinely flaky test of my own** - the daily-budget test placed rows 60 minutes back on a floating clock, so it failed whenever the suite ran shortly after midnight UTC, which is exactly when it did. **And a false alarm I nearly reported:** three unrelated alert tests failed in a full run, purely because I had two complete suites competing (956s vs 218s); isolated, they pass 37/37, and a clean run is 2383 passed |

| 16 | SOTA-A x3 (B and C approve) | (1) **"Move all funds to cash."** validated - round 12 removed `move`/`shift` from the STATE markers but never added them to the advice side, so they sat in NEITHER list; (2) the C1 block (U+0080..U+009F) is category `Cc`, which the `Cf`/`Mn`/`Me` scan never looked at; (3) **the floor had become a CEILING** - the breaker threshold is unbounded, so 20,001 consecutive errors could not be counted by a 20,000-row scan and the breaker stayed shut | **UPHELD x3 -> fixed.** (1) the movement verbs get their OWN group, without the `(?:s|ed)?` suffix the action verbs carry: adding them naively made `shift`+`ed` match and broke the legitimate "Band shifted to trim" - the same distinction round 10 drew for `trimmed`/`held`, rediscovered the hard way. (2) `Cc` joins the scan. (3) the window is `max(floor, (threshold + 1) x (cap + 2))`: large enough to SEE the required strikes, and every term only GROWS, so lowering either setting can never drop below the floor - the property rounds 11-15 kept breaking. Deletion audit: all three go red - **but only after fixing a test of mine that was green for the wrong reason.** The first C1 probe was "Band trim<C1> ok.", which an unrelated STATE gate rejects, so removing the `Cc` check left it passing; the probe now carries no band verb and asserts on the reason string |

| 17 | SOTA-A x5 + SOTA-C (B approves) | (A1) the strike window still used the CURRENT cap for durable history - five 5,000-reject composes become four visible strikes once the cap drops; (A2) `breaker_is_open` never reaps, so five expired claims report the breaker CLOSED to any status reader; (A3) the arithmetic gate demanded a DIGIT right after the operator, so **"Value 51+(-2)."** evaded it while denoting 49; (A4) `close` missing from the command verbs - **"Close every position."** validated; (A5) the breaker's own reason said "after 5 technical errors" for five exhausted-CONTENT strikes - a false diagnosis handed to the operator mid-incident; (C) the tight-slash branch was said to kill the score-pair exemption, failing every `51/100` message | **UPHELD x5 -> fixed; C NOT REPRODUCIBLE.** (A1) **fourth appearance of this class** (r11/12/14/15/17): history is written under the OLD settings, so no window derived from the CURRENT ones can be guaranteed to cover it. The derivation is GONE - the window is a flat memory guard and correctness comes from bounding the run by DATA (rows since the last success). (A2) the status path reaps too. (A3) `_OPERAND` admits a bracket or sign on the right-hand side. (A4) added, with the gerunds. (A5) the reason now says "consecutive strikes (exhausted composes or technical failures)" - a breaker message is a diagnosis and must not invent a cause. (C) executed exactly as cited: BOTH slash branches require whitespace on at least one side, so a tight pair never matches, and the digest validates - pinned with an explicit regex assertion per the disputed-finding protocol. Deletion audit: all five go red |

| 18 | SOTA-A x2 (B and C approve) | (1) the fixed 1,000,000-row scan makes any LARGER configured threshold unreachable - 1,000,001 consecutive errors with `BREAKER_STRIKES=1,000,001` still report the breaker closed; (2) the arithmetic and score scans required UNWRAPPED digit operands, so **"Value (51)/(2)."** conveyed an ungrounded 25.5 | **UPHELD x2 -> fixed, and the five-round argument is over.** Rounds 11/12/14/15/17/18 raised the same window from six directions, which finally says the fix was at the WRONG LAYER: derive the window from settings and history written under the OLD ones may not fit; fix the window and an UNBOUNDED setting outruns it. Both are true at once, so **no window can be correct while the inputs are arbitrary integers** - therefore the INPUTS are clamped (`_effective_strikes`, `_effective_cap`). A breaker needing a million consecutive failures is a typo, not a policy, and left unbounded it silently DISABLES the breaker: the worst possible reading of an operator's mistake. A test asserts the scan covers the worst run the clamps allow. (2) `_LHS` admits a closing bracket, matching `_OPERAND`'s opening one. Deletion audit: all three go red - **after the audit caught that nothing pinned the CAP clamp**, only the threshold clamp. Also moved a module-level `assert` into the suite: bandit forbids it in app code, and rightly, since `assert` is stripped under `-O` |

| 19 | SOTA-A x4 (B and C approve) | (1) **the P1 short-circuit sat AFTER stale-claim reaping and the reservation flush**, so a locked or unavailable database could delay - or fail - the one message class that may never wait; (2) direct-object imperatives and modal forecasts passed: **"Keep your positions."** and **"Markets may fall."**; (3) the strike reset compared `started_at` with a strict `>`, so a technical error written in the SAME SQLite instant as the success it followed was invisible and the breaker reported closed; (4) **the state parser did not know the prompts' own "is now <band>" construction** - the library writes it six times, so "level is now trim." was CONTENT_REJECTED | **UPHELD x4 -> fixed.** (1) both `decide()` and `reserve()` answer a P1 BEFORE touching the database; the whole point of decision 2 in docs/MESSAGE_ENGINE.md is that this message never waits on anything slow, and putting the check after a reap quietly broke that. Pinned by a test that COUNTS reap calls. (2) modal forecasts join the forecast gate, and a direct-object imperative needs no listed verb. (3) the bound tie-breaks on `id`. (4) `(?:is|was|are|were)\\s+now` is a marker, while bare "now" still is not - round 7's finding survives, pinned. **This is the FOURTH time hardening has misfired against LEGITIMATE text** (r6 digest slash, r7 digest band-after-score, r10 'trimmed', r19 'is now'); that failure mode is more dangerous than an escape because it degrades silently - retries burn, strikes accumulate, and the operator sees only fallbacks. Deletion audit: all four go red |

| 20 | SOTA-A x3 (B and C approve) | (1) `invest` in neither verb list - **"Invest all savings."** validated on both channels; (2) ASCII `x` omitted from the arithmetic gate - **"51x2"** denoted 102 while every symbol class missed the letter people actually type; (3) the cardinals `one`/`two` were excluded from the number-word gate, so **"There is one warning flag."** stated a quantity with no fact behind it | **UPHELD x3 -> fixed.** (1) added, with the gerunds. (2) `\\d[xX]\\d`, bounded by digits so it cannot touch words. (3) **A overturned a judgement I had made and documented**: I left the cardinals out as "ordinary English", which was wrong for a quantity. Adding them immediately broke a SHIPPED fallback - S3_TIER says "over two years", the lookback the methodology defines - so the rule is now sharper than either position: a cardinal before a TIME UNIT is methodology, anywhere else it is an ungrounded quantity, and ordinals stay out entirely because "second reading" counts nothing. The regression was caught by the prompt-library contract test, which is precisely the job it was added for in round 6. Deletion audit: all four go red, including the time-unit exemption |

| 21 | SOTA-A x3 (C approves) | (1) arithmetic in WORDS was unhandled - "51 divided by 2" carries no operator yet denotes 25.5; (2) **the round-20 time-unit waiver was context-free**, so "The decline lasted two days." asserted an observed duration with no fact behind it; (3) the pacing row was chosen by START time while the pause is measured from COMPLETION - a claim reaped late finished after a later-STARTED success, so the success won the ordering and the technical error silently lost its 120 s backoff | **UPHELD x3 -> fixed.** (1) a prose-arithmetic gate bounded by digits on both sides, so ordinary comparisons ("the gap between 51 and 100") do not trip it. (2) the waiver is now ADJECTIVAL only: "a two-year lookback" names the rule's own window, a bare "lasted two days" is a claim - and the S3 fallback was REWORDED to match rather than widening the rule to fit the text. This is the second round running where my own fix created the next defect. (3) `last_attempt` orders by `COALESCE(finished_at, started_at)`, matching `_dwell_from`. **My test for (3) was itself wrong** and failed honestly: I asserted the reaped failure "completed last" while setting timings where it completed FIRST (start 60 min back + 15 min TTL = 45 min, versus a success starting at 40). Corrected to 16 min back, so it is recorded as finishing 1 min ago. The deletion audit tells me whether a control is PINNED; only getting the scenario physically right tells me whether it is CORRECT. Deletion audit: all three go red |

| 22 | SOTA-A x2 (B approves) + SOTA-C repeat | (A1) `content_attempts` ordered only by `started_at`, so colliding boundary/rejection rows could be read in either order, undercounting spent attempts and admitting a request PAST the cap; (A2) **"Protect your portfolio."** validated - advice that names no trade; (C) the tight-slash branch was said, for the SECOND time, to reject "62/100" and make the score-pair exemption dead code | **UPHELD x2 -> fixed; C REFUTED AGAIN, on fresh evidence.** (A1) tie-break on `id` - round 19 fixed exactly this in the strike scan and I left its sibling untouched, which is the second time a fix has been applied to one of two identical call sites (cf. r14 breaker sizing). (A2) protective verbs join the advice gates: telling the operator to protect something is still telling them what to do. (C) **re-verified against the CURRENT regex rather than citing the round-17 disproof** — round 18 rewrote `_ARITHMETIC_RE`, so the earlier refutation did not carry over automatically and had to be re-run. Every slash branch requires whitespace on one side or a literal bracket, so a tight pair matches none; the digest validates, and the pin now ALSO asserts the exemption is not dead code by checking a genuine quotient is still refused. Deletion audit: both go red |

| 23 | SOTA-A x2 + SOTA-C (B approves) | (A1) a SIGN after a tight slash - "51/+2" carried no whitespace and no bracket, so every branch missed it; (A2) `acquire` absent from the trade verbs; (C) the bare ASCII hyphen between digits is unmatched, so "51-2" denotes an ungrounded 49 | **UPHELD x3 -> fixed. C's EXAMPLE was already refused; C's CLASS was real** - "51-2" fails on its own because `-2` is not a grounded token, but with a NEGATIVE fact in scope (a tail beta) the identical text passes and denotes 49. Checking that surfaced a FALSE POSITIVE C had not mentioned: **"the scale runs 0-100" was being REJECTED**, and the prompt library writes exactly that notation. So the hyphen is now told apart THREE ways rather than patched in either direction: `2026-08` is a date and left alone; `0-100` ascends, so it is a range and both ends ground independently; `51-2` descends, so it is a subtraction and refused. A degenerate `51-51` counts as a range - the digest's own "range {iqr_lo}-{iqr_hi}" can have equal bounds, and a subtraction yielding zero is not a message anyone writes. **The lesson for disputed findings:** accepting or rejecting the cited example wholesale would have been wrong BOTH ways here - the example failed, the class held, and the investigation found a defect in the opposite direction. Deletion audit: all three go red |

| 24 | SOTA-A x3 + SOTA-C | (A1) `avoid` absent - **"Avoid equities."** validated; (A2) a BRACKETED subtraction "51-(2)" slipped past the digit-hyphen-digit scan; (A3) the strike-run boundary used START order, so an error that started before a success but FINISHED after it was excluded and a threshold-1 breaker stayed closed; (C) the Q27 cap change (160 -> 150) was said to truncate existing failure alerts and lose tail content | **UPHELD x3 -> fixed; C REFUTED on execution.** (A1) avoidance is advice - telling the operator what NOT to do is still telling them. (A2) the bracketed form is the same subtraction written differently. (A3) **THIRD function to carry this ordering assumption** (r21 pacing row, r22 `content_attempts` tie-break, r24 the strike boundary); every one of those queries now orders by COMPLETION, since that is what `_dwell_from` measures. (C) executed rather than argued: the alert SYSTEM uses a hardcoded 160 and is untouched, and `failure_alert` COMPOSES within the limit instead of chopping afterwards - byte-identical text at either setting - with a documented truncation order that drops the error detail before the timeline. Q27 is binding regardless; the claim was that it MAIMS existing alerts, and it does not. Pinned by a test asserting the timeline survives. Deletion audit: all three go red |

| 25 | SOTA-A x3 (B and C approve) | (1) multi-word commands - **"Get out now."** - which single-verb lists structurally cannot express; (2) the ASCII-x branch demanded a bare digit, so **"51x(2)"** denoted 102; (3) **grounded numerals are reduced to an UNBOUND SET**, so a next-check fact of `08:30` contributed the tokens `08` and `30`, and those alone validated the FALSE time **"08:08"** | **UPHELD x3 -> fixed. (3) is the deepest grounding hole in the loop and a CLASS, not a typo.** Flattening every fact into a bag of numeral fragments loses both BINDING (which fact a token came from) and MULTIPLICITY, so any compound value can be reassembled into a different one — here a claim about when the monitor next runs, at a time no fact supports, passing every gate. Compound forms (`HH:MM[:SS]`) must now appear VERBATIM in the facts. It is the same shape as the arithmetic defects — a new value built from grounded pieces — but applied to a FACT rather than an operator, which is exactly why no arithmetic gate caught it. (1) `_COMMAND_PHRASES` carries the multi-word forms. (2) the x branch takes `_OPERAND`, like every other operator. Deletion audit: all three go red |

| 26 | SOTA-A x3 (B approves) | (1) **the round-25 class, still open on DATES** - a fact of `2026-08-01` supplies every fragment for the FALSE `2026-01-08`; (2) prose exponentiation - "51 to the power of 2" conveys 2601; (3) `trade` absent - **"Trade your holdings."** validated | **UPHELD x3 -> fixed. (1) cost a whole round to a mistake I had already named.** Round 25 identified fragment-recombination as a CLASS and then fixed it for TIMES only; the identical hole on dates was found by the panel rather than by me. The compound pattern now lives in ONE constant (`_COMPOUND_RE`: dates, year-months, times, slash-dates) with a test asserting every form is matched there, so a fourth form is ADDED rather than discovered as a fourth instance. The hyphen triage also had to learn that a date component pair is not a range. (2) prose exponentiation joins the word-arithmetic gate, including the unary "squared"/"cubed" which take no second number. (3) added. **My own fix created a false positive, caught before push:** adding `scale` to the command verbs rejected "The scale runs 0-100." - in this domain it is a NOUN far more often than a command, and the reasoning is now recorded in the source so it is not re-added. Deletion audit: all three go red |

| 27 | SOTA-A x3 (C approves) | (1) `withdraw` absent - **"bubblegauge: Withdraw all funds."** validated; (2) a spelled sign flips a grounded value - fact 51 admits **"reading is minus 51"**, reporting -51; (3) **the technical backoff REPLACED the global 300 s floor**, so a request was admitted 120 s after a 5xx | **UPHELD x3 -> fixed. (3) is a rule I implemented BACKWARDS, defended by a test I wrote in round 1.** The owner's wording is "technical 4xx/5xx -> wait MIN 2 min" — an ADDITIONAL minimum on top of the 5-minute floor, not a licence to ask sooner; only the format retry is an explicit exception. The pause is now `max(floor, backoff)`, and `test_technical_backoff_clears_after_two_minutes` — which asserted the wrong behaviour and had passed for 27 rounds — is rewritten to state the rule as given. **A wrong test does not merely miss a defect; it actively defends one**, which is why the panel could not surface this until it read the spec rather than the suite. (2) is the recombination class in prose form. (1) added. **The audit also caught a no-op mutation of mine:** the `withdraw` deletion string did not match the source line, so the control was never actually tested — re-run properly, it goes red. Deletion audit: all three go red |

| 28 | SOTA-A x3 + SOTA-C x2 (B approves) | (A1) the numeric-prefix state waiver accepted a BARE figure, so **"Instruction: at 51 hold."** validated; (A2) a decimal middle operand hid a chain - `51/100.0/4`; (A3 + C2, **CONVERGENT**) compound grounding used SUBSTRING membership, so a fact of `08:12:30` admitted the false `12:30` and `2026-08-01` admitted the partial `2026-08`; (C1) the digest-slash claim, for the THIRD time | **UPHELD x3 -> fixed; C1 refuted a third time.** (A3/C2 is the first time two verifiers converged independently, and it is the third variation of the RECOMBINATION class in four rounds — times (r25), dates (r26), now substrings of a compound. The facts' own compounds are enumerated with the same pattern and matched WHOLE.) (A1) was my round-7 waiver, added so the digest's "51/100 trim" would validate; a bare figure wears the same shape while carrying an instruction, so only a score-PAIR or a percentage qualifies now. (A2) the chain guard sees decimals. (C1) re-verified against the CURRENT regex rather than citing rounds 17 or 22 — the code changed twice in between, and a stale refutation would be worthless. Deletion audit: all three go red |

| 29 | SOTA-A x3 (B approves) | (1) the banned lexicon matched exact words, so **"Probabilities changed."** walked past a ban on "probability"; (2) **"Take a long position."** - the direct-object gate did not know indefinite articles; (3) the minus branch demanded SYMMETRIC spacing, so **"51- 2"** passed while "51 - 2" was refused | **UPHELD x3 -> fixed. (3) is the FOURTH appearance of "fixed one of two siblings"** (r14 breaker sizing, r22 tie-break, r24 completion ordering, r29 operator spacing): round 11 taught the SLASH that either side spaced counts and its minus sibling never learned it. Every operator now shares ONE spacing rule (`_EITHER_SIDE_SPACED`) rather than each carrying a copy. (1) the ban is on the CONCEPT, so stems take an inflection suffix. (2) articles added. **Two self-inflicted delays, both recorded:** a patch aborted on a stale anchor so only part of the fix landed until I re-checked; and shortening the stem to `probabilit` broke the SINGULAR because the suffix group lacked `y` — caught by my own new test, not the panel. A round-20 comment had also left the arithmetic block's indentation malformed, now cleaned, and the regex needed restructuring because ruff targets py3.11 where an f-string expression may not contain a backslash. Deletion audit: all three go red |

| 30 | SOTA-A x2 (B approves) | (1) `odds` and `go long` absent from the content lists; (2) **a DECIMAL subtraction wearing a range's clothes** - matching bare integers made "51.0-2.0" look like the ascending pair `0-2`, so it was classified a range while conveying 49 | **UPHELD x2 -> fixed.** (2) the hyphen operands are decimal-aware, and the guards exclude an adjoining DECIMAL POINT so a fractional tail cannot masquerade as a whole operand. **My first attempt created a fresh hole:** excluding ANY following dot stopped "Score 51-2." from being seen at all, because of the sentence-final period — caught by the round-23 test before push. That is the third time in this loop a fix has opened a hole the previous round closed, and each time an existing test caught it, which is the argument for the suite growing rather than being trimmed. Deletion audit: both go red |

## Reviewer guidance for subsequent rounds

- The **disclaimer gate**, **last-known-good + stale labeling**, **commit
  ordering**, and **auth-scoped caching** are deliberate, test-pinned design
  decisions reconciling rounds 1–6. Findings that require *removing* one of
  these properties to satisfy another are answered by this log.
- Everything grounded on the page hydrates from `/api/v1/content/*` or
  `/api/v1/status`; `tests/test_frontend_shallow.py` enforces the banned-literal
  list and the required wiring markers.

## PR #100 (message engine foundation) — round log

| # | Verifier | Finding | Resolution |
|---|---|---|---|
| 1 | SOTA-A ×6 + SOTA-B + SOTA-C | (A1) `decide()` has no atomic reservation — two workers both pass the gates and both call the model inside the 300 s floor or above the daily cap; (A2) the breaker scan was hard-limited to 50 rows, so any `BREAKER_STRIKES` above 50 could never be reached — a breaker configured never to open; (A3) pacing dwell anchored to `started_at`, so a slow request consumes its own backoff and can eat a 24 h cooldown; (A4) the imperative gate required "your"/"the", so **"Hold positions."** delivered operator advice; (A5) the single-line check tested only CR/LF, so U+2028/U+2029 render extra lines; (A6) the numeral regex omitted exponents, so `51e2` tokenised as grounded `51` + grounded `2` while denoting 5100; (B) **a `.venv` symlink was committed** — dangling on every fresh clone, and on the author's host it resolved site-packages from a DIFFERENT branch's venv; (C) the 30 s format pause trusted the caller's hint, so a retry could fire 30 s after an unrelated trigger's OK row, straight through the global floor | **UPHELD ×8 → all fixed.** (A1) `reserve()` makes the claim part of the checked state: the attempt row is INSERTED FIRST inside a savepoint — which is what takes the write lock — the gates are then evaluated with that row excluded, and the savepoint rolls back unless the verdict is ASK, so nothing is written unless a call really happens. A new `IN_FLIGHT` outcome paces concurrent callers and is explicitly neutral for the breaker run. (A2) both sizing call sites now derive the scan from the setting. (A3) dwell anchors to `finished_at` when present. (A4) a bare sentence-initial imperative is rejected while the STATE sense ("band is hold") still passes — band names are never banned words. (A5) U+2028/2029/VT/FF/NEL added. (A6) exponents are part of the numeral token. (B) untracked, and the root cause fixed: `.gitignore` held `.venv/` **with a trailing slash**, which matches directories only, so a symlink slipped past — now both forms are ignored and a test asserts nothing under `.venv` is tracked. (C) the short pause is earned only when the newest row IS that trigger's own format rejection. **The mandatory control-deletion audit then caught what the panel had not:** the breaker-sizing test pinned only `breaker_is_open`, leaving the independent `decide()` copy free to regress with CI green — closed, and all eight controls now go red when reverted |

## PR #100 round 31 — admission gate (decision 5 had no code)

Self-review finding, not a panel one. `docs/MESSAGE_ENGINE.md` decision 5
described the engine calling `live_admission_blockers` before every send, and
`grep -rn admission app/message_engine/` returned nothing. A documented
control that does not exist is worse than an undocumented gap: it reads as
covered in review.

Implemented in `app/message_engine/gate.py` with three controls, each pinned
by a mutation that turns the suite red — fail-closed evaluation (AH1), no P1
bypass (AH2), blockers actually honoured (AH3), refusal reasons preserved
(AH4), gate ordered before the transport (AH5).

**The first run of that audit was void, and the harness bug is worth
recording.** It restored with `git checkout -- app/`, which cannot restore an
UNTRACKED file, and `gate.py` was new. Every mutation stayed applied and
compounded into the next; two later mutations then failed to apply at all
because an earlier one had already rewritten the line they matched on. The
tell was the final line: "restored" reported 9 failures instead of the
baseline. The harness now snapshots and restores by copy and runs a
self-check first — mutate the untracked file, confirm red, restore, confirm
the exact baseline count returns — so a harness that cannot restore reports
itself instead of quietly manufacturing a clean-looking result.

## PR #100 round 32 — the panel was right on all four

combo/SOTA-A refuted (high) with four defects; combo/SOTA-C refuted (high) and
landed independently on the same core one. Every one reproduced. Nothing here
was disputed.

**Defect 2 (SOTA-A) / SOTA-C — normal operation looked like a broken provider.**
`_fallback()` wrote `FALLBACK_USED` for EVERY refusal, and `FALLBACK_USED` is a
strike. SOTA-C's scenario, executed verbatim:

    first compose  -> generated
    paced compose 1..5 -> fallback (pacing: 300s floor)
    outcomes: ['ok', 'fallback_used' x5]
    consecutive_strikes = 5   (threshold 5)
    breaker_is_open = True
    one hour later -> fallback (breaker open ...)

Five triggers inside the five-minute floor — an ordinary burst — opened the
24-hour breaker. Three further facts came out of executing it:

  * while the breaker was open, every suppressed trigger wrote another strike,
    so the state fed itself;
  * ONE gateway timeout cost TWO strikes (the TECHNICAL_ERROR row plus the
    fallback row), so a five-strike breaker opened after three real failures;
  * a paced refusal RESET the attempt budget, which is defect 2's "retries
    reset" clause.

I checked whether the lockout was permanent rather than 24h. **It is not** —
`last_attempt()` reads only pacing outcomes, so the cooldown runs from the last
genuine ask and the engine recovers at exactly 86400s. SOTA-C's number was
exact; my attempted escalation was wrong.

Fixed with a new outcome, `NOT_ASKED`: the engine was not PERMITTED to ask
(pacing, disabled, P1, budget, breaker-open). It strikes nothing, closes
nothing, and is skipped by `content_attempts`. `FALLBACK_USED` keeps its round-6
meaning — the engine asked and gave up — and is now written only for a genuinely
exhausted compose.

**Defect 1 — compound facts leaked their fragments.** `grounded_numerals()`
harvested `08` and `30` out of `F_NEXT_CHECK = "08:30"`, so the invented "30
warning signs are lit." validated. `F_NEXT_CHECK` is in the live fact set, so
this was reachable in production, not in principle. The fix had to be
SYMMETRIC: neither side stripped compounds, and those same leaked fragments
were what let a legitimate "next 14:00 UTC" pass — stripping only the facts
side rejected every message rendering a time it was correctly given (11 tests
went red and said so).

**Defect 3 — lock blast radius, and a P1 fast path that wasn't.** `reserve()`
takes SQLite's write lock at its flush and nothing committed it until the
caller's `session_scope` exited, so the lock spanned `complete()` — up to the
full 60s deadline — blocking every unrelated writer in the process, the alert
dispatcher included. The claim is now committed before the model call, which is
also what makes it visible to the concurrent worker it exists for; a process
death mid-call leaves IN_FLIGHT for `reap_stale_claims()`, which already exists
for exactly that. Separately `compose()` ran two SELECTs before the governor's
P1 short-circuit, defeating it; a P1 now returns before any query.

**Defect 4 — the quiet period started at the wrong instant.** `moment` is
captured before the call and was stored as `finished_at`, so pacing ran from
when the request was ISSUED. `_DEADLINE_S` is 60.0 and the floor is 300s, so
SOTA-A's "only 240s of the configured 300s" is exact. The failure time is now
measured with a monotonic clock, which stays deterministic under an injected
clock and is truthful in production.

MUTATION AUDIT (restore by copy; self-check first) — all nine red, baseline
353 recovered: R1 every refusal writes FALLBACK_USED, R2 NOT_ASKED back in the
strike scan, R3 NOT_ASKED counted as a spent attempt, R4 gateway error
double-strikes, R5/R6 either side stops stripping compounds, R7 P1
short-circuit removed, R8 write lock held across the call, R9 pre-call moment
stored as finished_at.

## PR #100 round 33 — four defects the ROUND-32 FIXES introduced

combo/SOTA-C now APPROVES and names the round-32 repairs as verified.
combo/SOTA-A refuted (high) with four new defects. All four reproduced; none
disputed. Every one is a consequence of the previous round's fix, which is the
argument for re-running the whole panel after a repair rather than only the
tests that were red.

**Defect 4 — the filter went behind the LIMIT.** `content_attempts()` excluded
NOT_ASKED in Python, after `.limit()`. With 200 paced refusals on top of three
genuine rejections it returned **0**, and `decide()` answered ASK past the
content cap. This is round 13's defect exactly — BUDGET_SKIPPED was moved into
the query for this precise reason, and the comment saying so sits four lines
above the code I wrote. Now filtered in the query.

**Defect 3 — the format-retry gate went dead.** Every rejection is now followed
by the NOT_ASKED row of the fallback that same compose returns, so
`_last_failure_class()` (newest row, LIMIT 1) never saw the rejection and
always answered None. The configured 30-second format retry could not fire at
all. NOT_ASKED rows are now invisible to that query: the question is "how did
the last ATTEMPT end", and a refusal the engine issued to itself is not one.

**Defect 2 — the fallback never had to satisfy the channel contract.** The
generated path is validated and rejected on overrun; the fallback path, which
is the one taken when something is already wrong, had no check. MY FIRST PROBE
FOUND NOTHING because it injected hostile values into slots those templates do
not contain. Sweeping every slot of every shipped fallback found **40**
violations — worst a 432-character body against a 150-character SMS cap — plus
newline injection splitting one message into several. Substituted values are
now single-line, and the text is clipped to the channel cap with an ellipsis.
Clipped rather than rejected: there is nothing to fall back TO from here.

**Defect 1 — the P1 fast path still wrote.** Round 32 moved the QUERIES off it
but still recorded an audit row, and `session.add()` + `flush()` takes SQLite's
write lock, so the message that must arrive could block behind an unrelated
writer. A P1 now touches the database not at all. Losing the row costs nothing
real: `message_engine_attempts` records what the engine did with the MODEL, and
a P1 never reaches it; the delivery is recorded by the alert system, where a
P1's audit trail belongs.

**A control of mine was vacuous, and the audit caught it.** The first version
of the fallback-contract test called `composer._fit()` directly, so deleting
its CALL SITE left the suite green — it proved the function worked and nothing
used it. Rewritten to drive `compose()`. Same trap as the red-line-5 work
earlier today: testing the helper instead of the wiring.

MUTATION AUDIT (restore by copy, self-check first) — 13 mutations, all red,
baseline 365 recovered: R1/R2/R4-R9 (round 32, re-run in full) and S1 filter
behind the LIMIT, S2 failure class blinded, S3 fallback cap call site removed,
S4 control characters pass through, S5 P1 writes a row.

## PR #100 round 34 — two regressions in the round-33 repair, two older gaps

combo/SOTA-C approves. combo/SOTA-A refuted (high) with four; combo/SOTA-B
refuted (medium) and found the WORST one independently. All four reproduced.

**The SMS clip I added last round broke the SMS contract, twice.** Fixing "the
fallback violates the channel contract" with code that violates the channel
contract is worth naming as the mistake it was.

  * `_fit()` marked the cut with "…", which is not in GSM-7. `septets()`
    RAISES on it — `Gsm7Error character '…' at position 144 is not GSM-7 (the
    message would become UCS-2 and no longer fit one SMS)` — and the validator
    rejects it. The "guaranteed delivery" fallback would have taken the
    transport down or forced the exact multipart spill the 150 cap exists to
    prevent.
  * `_fit()` compared `len()` against `sms_max_len` while the contract counts
    SEPTETS. The extended-GSM set costs two septets per character: 140 code
    points of "€" measured 280 septets and passed unclipped.

`app/alerts/gsm7.py` has had `is_gsm7`, `septets` and `first_non_gsm7` all
along; the alert path uses them. Now so does this one, with an ASCII "..."
marker for SMS and the typographic ellipsis kept for iMessage.

**Only ONE of the three resolve paths had been fixed.** Round 32's defect 4
repair went to the technical-error path; OK and the rejections still stamped
`finished_at` with the pre-call moment, so a successful 60-second call
shortened the next 300-second floor to 240 exactly as before. All three now
stamp the measured finish.

**Eighteen prompts mandate an output format nothing parsed.** Ten end with
"Exactly two lines and nothing else ... SMS: <sms body> then IMESSAGE:
<imessage body>", eight with a differently-worded equivalent, and fourteen say
nothing at all. `compose()` took `answer.strip()` as the message, so a model
that OBEYED was handed to the validator as one multiline over-length string
and rejected every time — those eighteen triggers could never produce
generated text. Parsed rather than re-authored: both shapes are legitimate and
the library is ratified, so the model is judged on what it was asked for.

**Stative directives were not directives.** "Move to cash." was refused;
"Stay in cash.", "Stay out of the market." and "Remain in cash until the band
clears." all validated. Telling the operator to STAY somewhere is as much an
instruction as telling them to move. Added as a CLASS per round 29, matching
the bare imperative only, so "The band stays hold." is untouched.

I checked that against the shipped library BEFORE concluding, because round 6/7
was exactly this mistake: hardening that silently refuses the library it ships
with. Identical refusals before and after (two, both artifacts of the probe
supplying "38" where the template renders "38%"). A first attempt at that check
substituted the band name into every slot and produced ten false refusals —
the test was wrong, not the rule.

MUTATION AUDIT — six, all red, baseline 389 recovered: T1 ellipsis marker
returns, T2 SMS measured in code points, T3 non-GSM-7 characters not dropped,
T4 labelled reply unparsed, T5 OK path back to the pre-call moment, T6 stative
directives allowed.

## PR #100 round 35 — three vendors, and the finding was MY revert

All three refuted. Defects 1, 2 and 4 were not new work at all: the branch was
REVERTING merged main.

  app/main.py            crash.klee.me restored to the CORS allowlist (#101 undone),
                         with the comment explaining its removal deleted alongside it
  tests/test_cors.py     the regression test that blocks re-adding it, deleted (21 lines)
  app/alerts/cutover.py  _HEARTBEAT_FRESH deleted, collapsing the WEEKLY digest's
                         freshness window from 8 days to 2 hours, which refuses
                         `cutover apply` roughly 6.5 days out of 7
  tests/test_alert_cutover.py   89 lines of the tests pinning that, deleted

Cause: `git reset --soft origin/main` in a worktree that predated those merges.
`--soft` moves HEAD and leaves the working tree alone, so `add -A && commit`
commits the OLD tree against the NEW base and every file the merges added is
staged as a deletion.

What makes this worth writing down is not the mistake but the miss after it. I
had already diagnosed this exact hazard an hour earlier on the same branch,
recorded it, and repaired one instance — the two files from #102. I then saw
deletions fall from 323 to 167 and accepted the number without asking what the
remaining 167 were. Checking the count is not the control; checking WHAT was
deleted is.

The `_HEARTBEAT_FRESH` deletion is the same control I had found as staged
damage in another worktree earlier the same day and correctly discarded there.
It reached a PR anyway, by a different route.

All four files restored from origin/main and verified: no klee.me entry in the
allowlist, _HEARTBEAT_FRESH present, cors and cutover suites green. The four
remaining deletions on this branch are deliberate and each is a single line —
schema revision 0017 -> 0018 (the message_engine_attempts migration) and
sms_max_len 160 -> 150 (ruling Q27).

**Defect 3 was genuine and new.** 20 shipped prompts spell out an
"SMS: <...>" output line and only 8 also spell out "IMESSAGE: <...>", so for
twelve triggers a compliant reply carries no iMessage body and the round-34
parser handed over the SMS one: 150 ASCII characters on a channel that allows
200 and two emoji. Composing is per-channel, so asking for both was always
redundant; `_prompt_for()` now ends with an explicit single-channel
instruction that overrides the library's format, and the parser stays as a
belt-and-braces reader for a model that labels anyway.

## PR #100 round 36 — the worst defect of the whole review, and it was mine

SOTA-B and SOTA-C approve; SOTA-A refuted (high) with four. All four reproduced.

**Defect 3 — dropping a character changed a VALUE.** Round 34's repair made the
SMS fallback GSM-7-safe by deleting every character outside the set. U+2212
MINUS SIGN is outside the set, so

    "Momentum -51 points."   (written with a typographic minus)
    -> "Momentum 51 points."

was transmitted: the same magnitude with the opposite meaning, by a monitor
whose entire job is to say which way a number moved. Deleting a character is
harmless for decoration and catastrophic for a sign, and the round-34 fix did
not distinguish them. Meaning-bearing characters are now TRANSLITERATED to
their exact ASCII counterparts first (minus, en/em dash, plus-minus, quotes),
and only genuine decoration is dropped — replaced with a SPACE, so removing it
cannot fuse "51" and "2" into "512".

**Defect 1 — every fact went to the model.** `_prompt_for()` pasted the
caller's whole dict, ignoring each entry's declared `grounding_fields`, so
anything the caller happened to be carrying was transmitted whether the trigger
needed it or not. Now only declared fields are visible, and it fails CLOSED: an
entry declaring nothing sends nothing, because a missing contract costs a
fallback while failing open costs a disclosure. Safe to restrict because every
shipped fallback slot is already inside its trigger's declared fields — checked,
and now asserted.

**Defect 2 — separators outside C0.** U+2028 LINE SEPARATOR, U+2029 PARAGRAPH
SEPARATOR and U+0085 NEXT LINE are not in the range the round-33 sanitiser
matched, and renderers treat all three as newlines.

**Defect 4 — a bare imperative on a position.** "Keep cash." carried no banned
verb and no advice framing. Rounds 29 and 34 had each added one more spelling
of a concept the verb list missed, so this keys on the OBJECT instead: a
clause-initial verb whose object is a position or an instrument is an
instruction about that position, whatever the verb.

**Two of my own controls were wrong, and the audit caught both.**
The decoration test spaced its probe out ("51 * 2"), so deleting the character
could not fuse anything and the control passed while proving nothing — U2
survived until the probe was tightened to "51*2". And the secret-gate suite failed on my own
fixture, which paired a secret-sounding fact name with a password-shaped value.
The repo's scanner read that as a leaked credential, which is the scanner
working. The test needs a value it can FIND, not one that looks stolen, so it
uses a plainly-synthetic marker.

Writing THIS note failed the same gate a second time, because quoting the
offending literal reproduces it. A description of a secret-shaped string must
not contain one.

## PR #100 round 37 — a verb list failed for the third round running

SOTA-B and SOTA-C approve. SOTA-A refuted (high) with two; both reproduced.

**Defect 2 — "choose cash." validated.** Round 29 added inflections to the
banned-verb list; round 34 added the stative forms; round 36 announced it was
keying on the OBJECT to escape the enumeration trap and then gated on an
enumeration anyway. So `choose`, `pick`, `select`, `prefer` and `opt` all
walked through.

The rule now names NO VERBS AT ALL. What identifies an imperative is its
SHAPE: English imperatives are subjectless, so a clause that opens with one
word, names a position, and ends there is an instruction about that position.
A declarative puts its verb after the subject ("Cash is 20%.", "Gold rose."),
so the position is not in second place and the clause does not end on it. A
test asserts the pattern contains no verb literals, because reintroducing a
list would pass every example above while leaving the next unlisted verb open.

**Defect 1 — a mandate nothing read back.** BASE_BAND_MOVED's prompt says the
message MUST plainly state that data is incomplete. `validate()` is
deliberately trigger-blind — it enforces the channel contract and the house
style, which are the same for every message — so nothing checked the trigger's
own requirement, and "bubblegauge: data is complete." passed as generated
while contradicting the one thing it was required to say.

The prompt's prose mandate now has a machine-checkable twin (`must_mention`),
enforced after validation as a CONTENT rejection so it is retried under the
iteration rules rather than silently sent. Three further controls: the
trigger's own fallback must satisfy its mandate (or the mandate is
unmeetable), a trigger without one is unaffected, and any prompt saying
MANDATORY CAVEAT in prose must declare a checkable form — which is this defect
generalised, so it cannot reappear in a new trigger.

MUTATION AUDIT — four, all red, baseline 452 recovered: W1 the shape rule
reverts to a verb list, W2 the clause-end anchor drops, W3 the mandate is not
enforced, W4 the declared mandate is removed from the library.

## PR #100 round 38 — two seams left by the round-36/37 fixes, and a fourth enumeration

SOTA-B and SOTA-C approve. SOTA-A refuted (high) with three; all reproduced.

**Defect 1 — the prompt and the validator disagreed about the facts.** Round 36
restricted the PROMPT to each entry's declared `grounding_fields` and left
validation reading the caller's whole dict. A numeral present only in an
undeclared fact therefore counted as grounded: "bubblegauge: reading 73."
validated with 73 nowhere the model could have seen it. A model cannot be
credited for matching data it was never shown. Both halves now derive from one
`visible_facts()`, so the asymmetry cannot reopen. Worth recording that this
risk was NOTED while implementing round 36 and then not closed.

**Defect 2 — a substring is not a claim.** Round 37's mandate check asked
whether the required phrase appeared. "data is not incomplete" and "data is no
longer incomplete" both contain it while saying the opposite of what the
trigger mandates. Now negation-aware.

**Defect 3 — the objects were an enumeration too.** Round 37 removed the VERB
list and kept an OBJECT list, so "Choose safer assets." validated. The first
repair enumerated adjective ENDINGS, which caught "safer" and missed "quality"
— the same trap one level down. Modifiers are now COUNTED rather than
recognised. The residual limit is written up in docs/MESSAGE_ENGINE.md
decision 9 rather than papered over.

**A control of mine was vacuous for the third time on this branch.** The
undeclared-fact test handed the filtered dict straight to `validate()`, so
reverting `compose()` to pass everything left the suite green — it proved the
helper worked and that nothing used it. Every "X now happens" control has to
exercise the entry point that is supposed to do X. The mutation to design is
"delete the call, keep the function".

MUTATION AUDIT — four, all red, baseline 481 recovered: X1 validation reads
the full dict again, X2 the negation guard drops, X3 modifiers go back to
adjective morphology, X4 the widened object class is removed.

## PR #100 round 39 — the round-32 lock fix was committing callers' transactions

SOTA-B and SOTA-C approve. SOTA-A refuted (high) with five; all reproduced.

**Defect 5 — compose() committed the caller's whole session.** Round 32 stopped
SQLite's write lock being held across a 60-second model call by committing.
`compose()` receives the CALLER's session, so every unrelated pending write in
that unit of work became durable and a caller that meant to roll back on a
later error no longer could. The trade was backwards: a stuck lock DELAYS, a
premature commit CORRUPTS. It now commits only when the caller's session was
empty on entry, and otherwise holds the lock — bounded by the stale-claim
reaper and the deadline.

Two attempts. The first guard inspected the session at commit time, but
`reserve()` flushes first, so the caller's pending objects had already left
`session.new` and the guard saw a clean session. The check has to happen at
ENTRY, before this function writes anything.

**Defect 4 — a message could DISPLAY a different number than it contained.**
U+202E RIGHT-TO-LEFT OVERRIDE survived sanitisation, so "51" renders as "15" on
a channel that honours Unicode — the text unchanged, the reader misinformed.
Same class as round 36's sign inversion. All bidi and invisible format controls
are stripped now.

**Defect 3 — the denial can FOLLOW the phrase.** Round 38's negation guard
looked only backwards, so "incomplete data is not present" and "incomplete data
has been ruled out" satisfied a mandate to say data IS incomplete.

**Defect 2 — a multiplier in front of a numeral.** The binary operators were
covered; "score is twice 51" asserts 102, which no fact contains.

**Defect 1 — "Choose bitcoin."** The residual limit decision 9 recorded last
round, in the one vocabulary this monitor actually discusses. Narrowed with the
instrument names; NOT closed. The panel is fail-closed and will keep finding
members of an open set, so this is what stops the PR converging rather than
merely improving. The allowlist redesign is the owner's call.

MUTATION AUDIT — five, all red, baseline 508 recovered: Y1 the caller-clean
guard drops, Y2 the post-phrase negation check drops, Y3 bidi controls pass
through, Y4 leading multipliers allowed, Y5 instrument names removed.

## PR #100 round 40 — a fix removed rather than repaired a third time

SOTA-B and SOTA-C approve. SOTA-A refuted (high) with three; all reproduced.

**Defect 1 — the cleanliness guard cannot see everything.** Round 39's guard
committed only when the caller's session was clean on entry. It misses work
FLUSHED before `compose()` was entered, and Core DML that never appears in
`session.new` at all. Two failed attempts at the same guard is evidence about
the APPROACH: there is no reliable way to ask a shared Session "is anything
here not mine". The commit is gone; docs/MESSAGE_ENGINE.md decision 10 records
the cost (the lock is held for the model call) and the real fix (the engine
owning its own transactions, which is a caller-visible refactor).

The two tests asserting the round-32 behaviour are INVERTED rather than
deleted, so restoring the commit fails against reasoning rather than silence.

**Defect 2 — a technical failure consumed the content cap.** Ruling Q38 counts
an exhausted content attempt and a terminal technical failure as separate
things. Three gateway timeouts exhausted the content budget, the next compose
then wrote FALLBACK_USED as a further strike, and a five-strike breaker opened
after FOUR failures.

**Defect 3 — a database error replaced the promised message.** `reserve()`
flushes, and a flush can raise on lock contention — outside the gateway-only
try block, so an `OperationalError` propagated to the caller instead of the
evergreen text this function promises always to return. The fallback exists
for exactly the moments when something is already wrong.

MUTATION AUDIT — three, all red, baseline 512 recovered: Z1 the commit
returns, Z2 the reservation error is not caught, Z3 technical errors count
toward the content cap again.
