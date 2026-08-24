# Security policy — bubblegauge

*Produced by the 2026-07 due-diligence audit (findings B-06, C-33, C-34, A-38). This describes controls that exist in code, not aspirations.*

## Reporting

This is a single-maintainer, self-hosted research service. Report security issues privately to the maintainer (repository owner). There is no bug-bounty.

## Secrets and credentials

- **All secrets live in `.env` on the host only.** `.env` is gitignored and has **never** been committed (verified: full-history scan for every known credential fragment returns zero hits).
- **The admin API key fails closed.** The service refuses to authenticate (HTTP 503) while `ADMIN_API_KEY` is empty or equals the shipped placeholder `change-me-to-a-long-random-string` (`app/security.py`). Set a strong random value: `python -c "import secrets;print(secrets.token_urlsafe(32))"`.
- **Reads are public; only writes are keyed.** `POST /api/v1/admin/*` require `X-API-Key` (constant-time compare). Reads (`GET /api/v1/*`, `/`, `/healthz`) are public and rate-limited (60/min/IP).

### Credential rotation (REQUIRED — audit B-06)

Provider keys/tokens used during the original construction were disclosed in a development chat channel. **A secret shown to any third party is published, even if never committed.** Revoke the retired `ANTHROPIC_API_KEY`; rotate every still-active disclosed credential, then update `.env`:

`FRED_API_KEY` · `TIINGO_API_KEY` · `TWELVE_DATA_API_KEY` · `ALPHAVANTAGE_API_KEY` · `POLYGON_API_KEY` · `ADMIN_API_KEY` · `SIPGATE_TOKEN_ID` + `SIPGATE_TOKEN`.

`LLM_API_KEY` is the active inference credential. It is host-only configuration and must be rotated if it has been shared; unlike the legacy list above, this document has no evidence that its value was disclosed.

Historical container builds used an unfiltered repository context with `COPY . .`, so an existing image or build cache may contain the deploy host's `.env` and runtime `data/`. The synchronized `.dockerignore`/`.containerignore` files prevent new copies; they cannot cleanse old artifacts. Deploy a fixed image, purge prior bubblegauge images/build cache, then rotate every credential that may have been baked.

There is no vault/rotation automation (residual risk — `audit/06`). Rotate manually and record the date here:

- Last rotated: **NOT YET ROTATED — do this before serving production traffic.**

## AI / model data-use (C-34)

Runtime model calls go through an operator-configured OpenAI-compatible hosted gateway and produce the ≤300-char judgment and ≤160-char digest. The repository also contains a dormant future Stage-7/A-B non-P1 fragment-code selector, but the alert dispatcher does not invoke it. **No external free text reaches either the runtime prompts or the dormant selector's prompt:** inputs are computed indicator numbers/enums, preapproved codes, and—only in the digest—a bounded prior LLM judgment; never personal data, secrets, scraped text, or third-party prose (`app/engine/judgment.py`, `app/engine/sms_report.py`, `app/alerts/llm_selector.py`). The configured route is opaque: this service cannot attest which underlying provider/model served a request or that provider's retention/training policy. Disclosure impact remains low because the structural input containment does not depend on provider policy.

## Personal data (C-04/C-23)

The only personal datum processed is the operator's **own** SMS recipient number and an SEC-etiquette contact email (both in `.env`). No third-party personal data is collected or stored. The recipient number is masked before logging (`app/notify/sipgate.py:_mask_recipient`).

## Supply chain (B-04/C-03)

CI runs `pip-audit` (dependency CVEs, blocking) and `detect-secrets` (secret leaks, blocking) on every push. Dependency pinning by hash-lockfile is a tracked improvement (`audit/06`). Every dependency was resolved to a real registry entry during the audit — no hallucinated/typo-squatted package.

## Known open items

See `audit/06-residual-risk-register.md` for the full list with compensating controls and tripwires. The load-bearing open item is **branch protection**: CI must be marked a required status check so a red build cannot merge (B-35).
