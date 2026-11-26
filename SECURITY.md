# Security policy — bubblegauge

*Produced by the 2026-07 due-diligence audit (findings B-06, C-33, C-34, A-38). This describes controls that exist in code, not aspirations.*

## Reporting

This is a single-maintainer, self-hosted research service. Report security issues privately to the maintainer (repository owner). There is no bug-bounty.

## Secrets and credentials

- **All secrets live in `.env` on the host only.** `.env` is gitignored and has **never** been committed (verified: full-history scan for every known credential fragment returns zero hits).
- **The admin API key fails closed.** The service refuses to authenticate (HTTP 503) while `ADMIN_API_KEY` is empty or equals the shipped placeholder `change-me-to-a-long-random-string` (`app/security.py`). Set a strong random value: `python -c "import secrets;print(secrets.token_urlsafe(32))"`.
- **The scoring reads are public; the alert reads and the writes are keyed.** `POST /api/v1/admin/*` require `X-API-Key` (constant-time compare), and so does every alert read, `GET /api/v1/alerts/*`: operator-only, under `ADMIN_API_KEY` and its fail-closed guard (owner decision D3a). The other reads (`GET /api/v1/*`, `/`, `/healthz`) are public and rate-limited (60/min/IP).

### Credential rotation (REQUIRED — audit B-06)

Provider keys/tokens used during the original construction were disclosed in a development chat channel. **A secret shown to any third party is published, even if never committed.** Revoke the retired `ANTHROPIC_API_KEY`; rotate every still-active disclosed credential, then update `.env`:

`FRED_API_KEY` · `TIINGO_API_KEY` · `TWELVE_DATA_API_KEY` · `ALPHAVANTAGE_API_KEY` · `POLYGON_API_KEY` · `ADMIN_API_KEY` · `SIPGATE_TOKEN_ID` + `SIPGATE_TOKEN`.

`FRED_API_KEY` was disclosed a second way, so rotate it whatever became of the chat disclosure: on 2026-07-15 a FRED 504 wrote it into `source_health.note` (row 223), the unauthenticated `GET /readyz` returned that note from 18:11 to 22:25 UTC, and the row held it until migration 0027 passed the stored notes through the gather's redaction. Every copy of the database taken before that migration ran still holds it.

`LLM_API_KEY` is the active inference credential. It is host-only configuration and must be rotated if it has been shared; unlike the legacy list above, this document has no evidence that its value was disclosed.

Historical container builds used an unfiltered repository context with `COPY . .`, so an existing image or build cache may contain the deploy host's `.env` and runtime `data/`. The synchronized `.dockerignore`/`.containerignore` files prevent new copies; they cannot cleanse old artifacts. Deploy a fixed image, purge prior bubblegauge images/build cache, then rotate every credential that may have been baked.

There is no vault/rotation automation (residual risk — `audit/06`). Rotate manually and record the date here:

- Last rotated: **NOT YET ROTATED — do this before serving production traffic.**

## AI / model data-use (C-34)

Runtime model calls go through an operator-configured OpenAI-compatible hosted gateway and produce the ≤300-char judgment, with the message engine on the daily digest (a deterministic template with the engine off), and after a release the area codes of the deploy note. **No external free text reaches the prompts of the judgment and the digest:** inputs are computed indicator numbers/enums, the owner's prompt library and repository-authored references, and—only in the digest—a bounded prior LLM judgment; never personal data, secrets, scraped text, or third-party prose (`app/engine/judgment.py`, `app/message_engine/composer.py`). The configured route is opaque: this service cannot attest which underlying provider/model served a request or that provider's retention/training policy. For the judgment and the digest, disclosure impact remains low because the structural input containment does not depend on provider policy.

The deploy note's prompt is the one exception, outside that containment: it carries this repository's merged commit titles and descriptions and the changed paths. The model answers with area codes from a closed list and the note renders their fixed phrases, so no word it writes reaches the message, which goes only to the owner's own iMessage recipient (`app/services/deploy_note.py`, AGENTS.md rule 1).

## Personal data (C-04/C-23)

The only personal data processed are the operator's **own** recipients - the SMS number and the iMessage handle - and an SEC-etiquette contact email (all in `.env`; the iMessage recipient also in the host notices' credentials file, `docs/AUTO_DEPLOY.md`). No third-party personal data is collected or stored. Both recipients are masked before logging (`app/notify/sipgate.py:_mask_recipient`, `app/notify/imessage.py:_mask_recipient`).

## Supply chain (B-04/C-03)

CI runs `pip-audit` (dependency CVEs, blocking) and `detect-secrets` (secret leaks, blocking) on every pull request and every push to `main`. Dependencies are pinned by hash: the image installs `requirements.lock` and CI `requirements-dev.lock`, both with `--require-hashes` (`tests/test_dependency_pins.py` holds them to `pyproject.toml` and to each other). Every dependency was resolved to a real registry entry during the audit — no hallucinated/typo-squatted package.

## Known open items

See `audit/06-residual-risk-register.md` for the list with compensating controls and tripwires. Of the three STOP-SHIP items of the 2026-07 audit, one is open: the **credential rotation** above (B-06). The other two are in place (read 2026-11-15): a ruleset on `main` with no bypass actors requires the status checks `test (3.12)`, `image` and `independent-verify` - the cross-vendor review panel - on an up-to-date branch, so a pull request with a red check does not merge. Rows A-01/B-35 and A-39 of the register say what remains of each; above all, Code Owner review is not enabled, by decision (`.github/CODEOWNERS`, enforcement status).
