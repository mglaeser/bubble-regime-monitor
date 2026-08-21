#!/bin/sh
#
# notify-outage.sh — tell the operator the machinery is down, from OUTSIDE it.
#
# WHY THIS IS NOT PYTHON, AND NOT IN THE CONTAINER.
# Every other failure report in this repository is produced by the application:
# app/services/failure_alert.py sends "the recompute is failing" over the digest
# transport. That design cannot report the application being GONE, and the
# snapshot history contains exactly that outage — 2026-08-06 to 2026-08-20, 344
# hours, no notification, and the watchdog timer was not installed at all.
#
# So the dependency list here is deliberately short: POSIX sh and curl. No
# interpreter to fail to start, no container to be stopped, no database to be
# locked, no application import to raise. Each one of those is a thing that can
# be the broken thing, and a monitor must not share a failure mode with what it
# monitors.
#
# It is still not a complete answer, and pretending otherwise would be the same
# mistake: a dead host or a dead network silences this too. That needs
# off-machine monitoring, and no amount of local scheduling substitutes for it.
#
# Usage:  notify-outage.sh "<short reason>"
# Config: IMESSAGE_API_BASE_URL, IMESSAGE_API_KEY, IMESSAGE_RECIPIENT
#         (the same names the application uses; see app/config.py)
# Exit:   0 sent · 1 misconfigured · 2 the proxy refused it

set -eu

REASON="${1:-bubblegauge watchdog reported a failure}"

# Fail LOUDLY when unconfigured. A monitor that silently does nothing when a
# variable is missing reproduces the bug it was written to end — and this
# deployment has already been bitten once by a misspelled IMESSAG_ENABLED.
missing=""
[ -n "${IMESSAGE_API_BASE_URL:-}" ] || missing="$missing IMESSAGE_API_BASE_URL"
[ -n "${IMESSAGE_API_KEY:-}" ]      || missing="$missing IMESSAGE_API_KEY"
[ -n "${IMESSAGE_RECIPIENT:-}" ]    || missing="$missing IMESSAGE_RECIPIENT"
if [ -n "$missing" ]; then
    echo "notify-outage: refusing to run, unset:$missing" >&2
    exit 1
fi

BASE=$(printf '%s' "$IMESSAGE_API_BASE_URL" | sed 's:/*$::')

# THE KEY NEVER GOES IN argv. /proc/<pid>/cmdline is world-readable on a default
# Linux, so `-H "Authorization: Bearer $KEY"` hands the token to any local user
# who runs ps during the request. curl reads it from a config file instead, mode
# 0600, in a private temp file removed on every exit path. (The cross-vendor
# review panel refused the first version of this script for exactly this.)
case "$IMESSAGE_API_KEY" in
    *\"*|*\\*|*"
"*) echo "notify-outage: key contains a quote, backslash or newline; refusing" >&2
    exit 1 ;;
esac

# No predictable-path fallback. A name like /tmp/bg-notify.$$ is guessable, and
# /tmp is world-writable: an attacker who pre-creates that path as a symlink has
# the key written wherever they point it. mktemp is the only creation here
# because it is the only one that is atomic, O_EXCL and 0600. If it is missing,
# refuse — sending nothing is recoverable, leaking the credential is not.
umask 077
CFG=$(mktemp "${TMPDIR:-/tmp}/bg-notify.XXXXXX" 2>/dev/null) || {
    echo "notify-outage: mktemp unavailable; refusing to write the key to a predictable path" >&2
    exit 1
}
chmod 600 "$CFG" 2>/dev/null || true
trap 'rm -f "$CFG"' EXIT INT TERM HUP
printf 'header = "Authorization: Bearer %s"\n' "$IMESSAGE_API_KEY" > "$CFG"

# Answer the operator's first question before they have to ask it: is the thing
# running at all? `podman ps` is cheap and cannot hang the way an exec can.
if command -v podman >/dev/null 2>&1 && \
   podman ps --filter "name=^bubblegauge$" --filter status=running \
             --format '{{.Names}}' 2>/dev/null | grep -q bubblegauge; then
    CONTAINER="container running"
else
    CONTAINER="container NOT running"
fi

HOST=$(hostname 2>/dev/null || echo "?")
WHEN=$(date -u '+%Y-%m-%d %H:%M UTC' 2>/dev/null || echo "?")
TEXT="bubblegauge: $REASON. $CONTAINER on $HOST at $WHEN. Alert delivery may be down."

# uuidgen is not guaranteed on a minimal host; the fallback stays inside the
# proxy's required charset [A-Za-z0-9._~-]{8,128}.
KEY=$(uuidgen 2>/dev/null | tr -d '-' || true)
[ -n "$KEY" ] || KEY=$(od -An -tx1 -N16 /dev/urandom 2>/dev/null | tr -d ' \n' || true)
[ -n "$KEY" ] || KEY=$(date -u +%Y%m%d%H%M%S)$$

# json-escape the text: a stray quote or backslash would otherwise produce a
# malformed body and the message would be lost at the moment it matters most.
esc() { printf '%s' "$1" | sed -e 's:\\:\\\\:g' -e 's:":\\":g' | tr -d '\000-\037'; }

BODY=$(printf '{"recipient":"%s","text":"%s","service":"imessage"}' \
        "$(esc "$IMESSAGE_RECIPIENT")" "$(esc "$TEXT")")

# --max-time bounds the whole attempt: a notifier that can hang is a notifier
# that does not notify. The key is passed via a header argument and never
# echoed; `set -x` is deliberately not used anywhere in this script.
# -q FIRST: without it curl reads ~/.curlrc before anything else, and an entry
# there (a proxy, an extra --url, a --write-out) would send this bearer token
# somewhere the operator never chose. A credential-bearing request must not
# inherit ambient configuration.
code=$(printf '%s' "$BODY" | curl -q -sS -o /dev/null -w '%{http_code}' \
        --max-time 20 --retry 2 --retry-delay 3 \
        -X POST "$BASE/api/messages" \
        --config "$CFG" \
        -H "Content-Type: application/json" \
        -H "Idempotency-Key: $KEY" \
        --data-binary @- 2>/dev/null) || code="000"

case "$code" in
    2*) echo "notify-outage: sent ($code)"; exit 0 ;;
    *)  echo "notify-outage: proxy refused or unreachable (http $code)" >&2; exit 2 ;;
esac
