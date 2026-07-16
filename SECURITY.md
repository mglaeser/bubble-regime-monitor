# Security policy — bubblegauge

*Produced by the 2026-07 due-diligence audit (findings B-06, C-33, C-34, A-38). This describes controls that exist in code, not aspirations.*

## Reporting

This is a single-maintainer, self-hosted research service. Report security issues privately to the maintainer (repository owner). There is no bug-bounty.

## Secrets and credentials

- **All secrets live in `.env` on the host only.** `.env` is gitignored and has **never** been committed (verified: full-history scan for every known credential fragment returns zero hits).
- **The admin API key fails closed.** The service refuses to authenticate (HTTP 503) while `ADMIN_API_KEY` is empty or equals the shipped placeholder `change-me-to-a-long-random-string` (`app/security.py`). Set a strong random value: `python -c "import secrets;print(secrets.token_urlsafe(32))"`.
- **Reads are public; only writes are keyed.** `POST /api/v1/admin/*` require `X-API-Key` (constant-time compare). Reads (`GET /api/v1/*`, `/`, `/healthz`) are public and rate-limited (60/min/IP).

### Credential rotation (REQUIRED — audit B-06)

Every provider key/token used by this service was disclosed in a development chat channel during construction. **A secret shown to any third party is published, even if never committed.** Rotate all of them — revoke and reissue at each provider, then update `.env`:

`ANTHROPIC_API_KEY` · `FRED_API_KEY` · `TIINGO_API_KEY` · `TWELVE_DATA_API_KEY` · `ALPHAVANTAGE_API_KEY` · `POLYGON_API_KEY` · `ADMIN_API_KEY` · `SIPGATE_TOKEN_ID` + `SIPGATE_TOKEN`.

There is no vault/rotation automation (residual risk — `audit/06`). Rotate manually and record the date here:

- Last rotated: **NOT YET ROTATED — do this before serving production traffic.**

## AI / model data-use (C-34)

The only model call is a numbers-in / short-text-out request to the Anthropic hosted API (the ≤300-char "judgment call" and ≤160-char SMS). **The prompt contains only computed indicator numbers and enum states — no personal data, no secrets, no third-party data** (`app/engine/judgment.py`). Anthropic's commercial API does not train on API inputs by default; because the prompt carries nothing sensitive, the exposure is nil regardless.

## Personal data (C-04/C-23)

The only personal datum processed is the operator's **own** SMS recipient number and an SEC-etiquette contact email (both in `.env`). No third-party personal data is collected or stored. The recipient number is masked before logging (`app/notify/sipgate.py:_mask_recipient`).

## Supply chain (B-04/C-03)

CI runs `pip-audit` (dependency CVEs, blocking) and `detect-secrets` (secret leaks, blocking) on every push. Dependency pinning by hash-lockfile is a tracked improvement (`audit/06`). Every dependency was resolved to a real registry entry during the audit — no hallucinated/typo-squatted package.

## Known open items

See `audit/06-residual-risk-register.md` for the full list with compensating controls and tripwires. The load-bearing open item is **branch protection**: CI must be marked a required status check so a red build cannot merge (B-35).
