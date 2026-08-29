# CONTENT_API — UI content as a served resource

**Directive.** The frontend carries no explanations, references, descriptions, or
scientific content of its own — only buttons, titles, interaction text, labels,
and dashboard-specific introductions may live in the page. Everything grounded
(scientific text, numbers, metrics, catalogues) is served by the API, so a change
in metric understanding is an API-side edit that updates every frontend at once.
The page is deliberately shallow; information is a resource.

## 1 · Inventory of static frontend content (audit of `app/routers/status.html`)

The status page was already hydration-based (it renders `/api/v1/status`), but
carried static content in the file. Classification of every text node:

### KEEP — interaction text, titles, labels (allowed in the page)

| Where (pre-change lines) | What |
|---|---|
| 6, 79, 81 | `<title>`, `<h1>bubblegauge</h1>`, `Refresh` button (also pinned by `tests/test_status.py`) |
| 93–153 | Section headings (`Headline`, `Science audit`, `API reference`, …) — titles |
| 193–197, 201, 225–228, 241–243, 269–270, 310–321, 324, 339, 351, 371–372 | Label/prefix glue around API values (`IQR`, `Judgment: `, `Basis: `, `w=`, `Scientific sources:` …) |
| 247, 273, 293, 357 | Table column headers mirroring API field names |
| 163, 209–219, 252, 280, 303-badge-usage | Status/badge vocabulary `ok/failed/error/warn/info/unknown/all` — triply load-bearing (text, CSS class suffix, tab filter): code, not content |
| 220 | `Nothing flagged at this level.` — pure UI empty state, no science |
| 381 | `Failed to load status: ` — the fetch-failure path can never be API-served |

### STATIC-SCI — moved out of the page into the content API

| Pre-change lines | Content | Now served at |
|---|---|---|
| 84–90 | Full disclaimer (was the 3rd hand-synced copy of `references.DISCLAIMER`) | `content/dashboard` block `disclaimer_full` (canonical import, single source) |
| 97–99 | Science-audit intro (“…Scientific correctness is the leading design aspect.”) | block `intro.science_audit` |
| 106–107 | Sources intro (“…scientific/official basis of each source.”) | block `intro.sources` |
| 113–116 | Feed-sources intro (endpoint + CNN Fear & Greed + non-scoring claim) | block `intro.feed_sources` |
| 129–130 | Legs intro (literature-basis claim) | block `intro.legs` |
| 136–137 | API-section intro | block `intro.api` |
| 139–143 | Doc links (Swagger/ReDoc/openapi/status JSON/methodology JSON) | block `api.doc_links` |
| 173–184 | `const ENDPOINTS` — endpoint catalogue with purposes | block `api.endpoints` |
| 189 | No-snapshot empty state carrying the recompute schedule (was the page’s own copy of the cron fact) | block `empty.snapshot`, text derived server-side from `recompute_slots.RECOMPUTE_SLOT_HOURS` |
| 268, 291 | Empty states carrying recompute-mechanics claims | blocks `empty.feed`, `empty.providers` |
| 303 | `GROUND_BADGE` grounding-taxonomy→severity map | block `taxonomy.grounding` |
| 377–379 | Footer `· Research, not advice.` tag | block `advice_tag` |

### DYNAMIC — live-data-customized slots (placeholders in this PR)

Served by `GET /api/v1/content/dynamic` with **hard, regex-checkable
contracts**. Placeholders satisfy their own contracts; a later stage replaces
them with generated text under the same contracts.

| Slot | Purpose | max_len | Regex |
|---|---|---|---|
| `headline_note` | One-line interpretation of the current score/band for the status page | 200 | `^[\x20-\x7E]{1,200}$` |
| `audit_note` | One-line summary of the current science-audit state | 160 | `^[\x20-\x7E]{1,160}$` |
| `dashboard_regime_note` | Context line for the companion dashboard relating current regime metrics | 240 | `^[\x20-\x7E]{1,240}$` |

Already-dynamic, already-API-served (no change): `snapshot.judgment_call`,
`service.recompute_schedule`, the worked `/score` example, docs links inside
`/api/v1/status`, and every metric/series value.

## 2 · Endpoints

```
GET /api/v1/content/dashboard   static blocks {slug: {kind, text|items|entries}}
GET /api/v1/content/dynamic     dynamic slots {slug: {text, source, updated_at, purpose, as_of, constraints}}
```

v1 completeness is the full code-anchored manifest (`app/content_manifest.py`):
the served block set must EQUAL the manifest — every listed slug present with
its declared kind, and no undeclared block — or the artifact degrades whole to
built-ins at version 0. Extend the manifest in the same PR that adds content;
an artifact block with no manifest entry is treated as injected content.

Static blocks may carry `as_of: "YYYY-MM"` — mandatory for editorial whose
copy asserts calendar recency ("today", "right now", "this cycle"): the claim
is frozen at authoring time and clients must be able to date it. Blocks whose
"now" is live-referential (copy riding the live payload it explains) carry no
stamp; the reviewed allowlist lives in `test_dated_editorial_carries_as_of`.
```
```

- Standard `{data, meta}` envelope, `require_read_access` + 60/min rate limit —
  the `/api/v1/status` pattern. `Cache-Control: public, max-age=300` / `max-age=60`.
- Registry: `app/content_registry.py`. Scientific text is **imported** from
  `app/references.py` (the declared single source of truth), never re-typed;
  the schedule string is derived from `app/engine/recompute_slots.py`
  (`schedule_display()`), which also replaces the literal in
  `app/services/status.py`.
- `source` on a dynamic slot is `placeholder` in this PR; the generation stage
  will serve `generated` / `fallback` under the identical shape.

## 3 · Decisions & trade-offs

1. **The static-disclaimer rule is deliberately reversed.** v3.6.0 kept the
   disclaimer static-on-page so it survives a fetch failure. Under the
   shallow-frontend directive it now hydrates from the content API like all
   scientific text. Mitigation — the **disclaimer gate**, enforced in
   `load()`: grounded values render *only after* the disclaimer text is
   actually on screen; if the content fetch fails (network error **or**
   non-2xx — `fetchJson` throws on `!r.ok`), the page renders
   "data display disabled" and never fetches/renders the status document, so
   no claim is ever shown un-disclaimed. The disclaimer also remains on
   `README`, `/docs`, and `/api/v1/meta/methodology`.
   The v3.6.0 rule that machine payloads carry **no per-response advice tag**
   is untouched — the content API serves the text as requested data, exactly
   like `meta/methodology` already does.
2. **Section headings stay; section intros move.** Headings are titles
   (allowed); intros are explanations (content).
3. **Empty states that state operational facts** (cron slots, rebuild
   mechanics) are content; the purely interactional ones (`Nothing flagged at
   this level.`, fetch-failure text) stay in the page — the failure path can
   never depend on the API.
4. **Render sequencing:** the endpoint table was rendered before the status
   fetch; it now renders when content arrives, with a neutral loading/failure
   fallback (`—`).

## 4 · Test contracts

- `tests/test_content_api.py` — envelope shape; every registry slug served
  and non-empty; `disclaimer_full` equals `references.DISCLAIMER` (markdown
  markers stripped); every catalogued endpoint path exists in the OpenAPI
  schema (or is a documented non-API path); every dynamic slot's regex
  compiles, its placeholder matches its own contract, `source == "placeholder"`;
  no unresolved `{…}` interpolation in served text.
- `tests/test_frontend_shallow.py` — reads `status.html` from disk and fails
  if scientific literals return (disclaimer phrases, `1929`, `CNN`,
  `02/06/10/14/18/22`, `Research, not advice`, `const ENDPOINTS`, indicator
  purposes, grounding-taxonomy labels); asserts the hydration targets and the
  content fetches exist. Complements the existing XSS/self-containment pins in
  `tests/test_status.py` (DOCTYPE, `textContent`-only, no external assets).
