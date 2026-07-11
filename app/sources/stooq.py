"""Stooq daily CSV loader — DEMOTED to optional experimental provider (v3.1).

FINDING (operator-verified on the live server, July 11 2026): Stooq's CSV
endpoint (https://stooq.com/q/d/l/?s=spy.us&i=d) returns HTTP 200 with a
~796-byte HTML body containing a JavaScript SHA-256 proof-of-work (PoW)
anti-bot challenge (Anubis-style). The page embeds a challenge string `c` and
difficulty `d=4`; the browser brute-forces a nonce `n` such that
SHA-256(c + n) in hex starts with d zeros, POSTs (c, n) to /__verify with
same-origin credentials, receives a session cookie, and reloads — only then
is CSV served. A JS-less HTTP client can never satisfy this, so it always
receives the challenge instead of data. This was the root cause of the
persistent N=0 from Stooq.

Stooq is therefore OFF the automatic chain: STOOQ_ENABLED=false by default.

ToS caveat (verbatim per spec): Solving the PoW is technically feasible and
the challenge is explicitly designed to be solvable by a browser; however,
automated circumvention of an anti-bot gate may violate Stooq's Terms of
Service. This path is the operator's choice and is disabled by default
(STOOQ_ENABLED=false). No headless browser is used — too heavyweight for a
slim container; dropping Stooq entirely is fully supported and is the
recommended default.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
from pathlib import Path

import httpx

from app.logging_conf import get_logger
from app.sources import SourceError

log = get_logger(__name__)

BASE = "https://stooq.com/q/d/l/"
POLITE_DELAY_S = 2.0
POW_NONCE_CEILING = 5_000_000  # safety ceiling; abort runaway search
POW_MAX_CYCLES = 3             # give up after 3 challenge cycles
COOKIE_TTL_S = 24 * 3600       # reuse the /__verify session cookie for 24 h

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

LIMIT_MARKERS = ("exceeded the daily hits limit", "przekroczono dzienny limit")
NO_DATA_MARKERS = ("no data", "brak danych")

_last_request: list[float] = [0.0]


class StooqLimitError(SourceError):
    """Stooq's per-IP daily download limit was hit — retrying is pointless today."""


class SourceUnavailable(SourceError):
    """Stooq answered with a notice/empty body instead of CSV."""


class StooqPoWChallenge(SourceUnavailable):
    """Stooq served its JavaScript proof-of-work anti-bot challenge page.

    Detection spec: body starts with '<!DOCTYPE html' OR contains '__verify'.
    Never parse such a body as CSV; count Stooq as provider-down."""


def detect_pow(body: str) -> bool:
    return body.lstrip()[:14].lower().startswith("<!doctype html") or "__verify" in body


def _parse_csv(text: str, symbol: str) -> list[tuple[str, float]]:
    if detect_pow(text):
        raise StooqPoWChallenge(
            f"stooq {symbol}: JS proof-of-work challenge page served instead of CSV")
    head = text[:200].strip()
    lowered = head.lower()
    if any(m in lowered for m in LIMIT_MARKERS):
        raise StooqLimitError(f"stooq {symbol}: daily hits limit exceeded")
    reader = csv.DictReader(io.StringIO(text))
    field_map = {name.strip().lower(): name for name in (reader.fieldnames or [])}
    date_key, close_key = field_map.get("date"), field_map.get("close")
    rows: list[tuple[str, float]] = []
    if date_key and close_key:
        for row in reader:
            try:
                rows.append((row[date_key], float(row[close_key])))
            except (KeyError, TypeError, ValueError):
                continue
    if len(rows) < 50:
        if any(m in lowered for m in NO_DATA_MARKERS):
            raise SourceUnavailable(f"stooq {symbol}: no data for symbol")
        raise SourceUnavailable(
            f"stooq {symbol}: insufficient rows ({len(rows)}); body starts: {head[:80]!r}")
    return rows


# ---------------------------------------------------------------------------
# Optional polite PoW solver (STOOQ_ENABLED=true only; see ToS caveat above)
# ---------------------------------------------------------------------------

def solve_pow(challenge: str, difficulty: int = 4) -> int | None:
    """Brute-force the hashcash nonce: SHA-256(c + n) hex starts with d zeros."""
    target = "0" * difficulty
    n = 0
    while n <= POW_NONCE_CEILING:
        if hashlib.sha256((challenge + str(n)).encode()).hexdigest().startswith(target):
            return n
        n += 1
    return None


def _cookie_path() -> Path:
    from app.config import get_settings

    db_url = get_settings().db_url
    base = Path(db_url.replace("sqlite:///", "/", 1)).parent if db_url.startswith("sqlite:///") \
        else Path("/tmp")
    return base / "stooq_cookies.json"


def _load_cookies(client: httpx.Client) -> None:
    try:
        path = _cookie_path()
        if not path.exists() or time.time() - path.stat().st_mtime > COOKIE_TTL_S:
            return
        for name, value in json.loads(path.read_text()).items():
            client.cookies.set(name, value, domain="stooq.com")
    except Exception:
        pass


def _save_cookies(client: httpx.Client) -> None:
    try:
        _cookie_path().write_text(json.dumps(dict(client.cookies)))
    except Exception:
        pass


def fetch_with_pow(symbol: str) -> list[tuple[str, float]]:
    """Fetch CSV, solving the PoW challenge if presented (max 3 cycles).

    Reuses the /__verify session cookie from disk for 24 h so at most one
    solve per day happens, not one per request (politeness)."""
    wait = POLITE_DELAY_S - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        _load_cookies(client)
        for _cycle in range(POW_MAX_CYCLES):
            resp = client.get(BASE, params={"s": symbol, "i": "d"})
            _last_request[0] = time.monotonic()
            body = resp.content.decode("utf-8-sig", errors="replace")
            if not detect_pow(body):
                return _parse_csv(body, symbol)
            m = re.search(r'const c="([^"]+)"', body)
            d = re.search(r"\bd=(\d+)", body)
            if not m:
                raise StooqPoWChallenge(f"stooq {symbol}: challenge page without parseable c=")
            nonce = solve_pow(m.group(1), int(d.group(1)) if d else 4)
            if nonce is None:
                raise StooqPoWChallenge(f"stooq {symbol}: PoW nonce search hit safety ceiling")
            verify = client.post(
                "https://stooq.com/__verify",
                data={"c": m.group(1), "n": nonce},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if not verify.is_success:
                raise StooqPoWChallenge(f"stooq {symbol}: /__verify rejected the solved nonce")
            _save_cookies(client)  # session cookie persists 24 h
    raise StooqPoWChallenge(f"stooq {symbol}: still challenged after {POW_MAX_CYCLES} cycles")


def total_return_pct(closes: list[tuple[str, float]], trading_days: int) -> float:
    """Total return over the trailing `trading_days`, in percent."""
    if len(closes) <= trading_days:
        raise SourceError("not enough history for total return window")
    start, end = closes[-trading_days - 1][1], closes[-1][1]
    return (end / start - 1.0) * 100.0
