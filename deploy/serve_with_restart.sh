#!/usr/bin/env bash
# Crash-restart wrapper for the token-monitoring server.
#
# Run inside a screen session on arecibo:
#   screen -dmS token-monitoring bash deploy/serve_with_restart.sh
#
# Never uses the same screen name, port, or state dir as JMD (9002) /
# XEOL (9003) / CAI (9001) / Califone (9008) — they cohabit on arecibo.
#
# Override defaults with environment variables:
#   PORT=9004 HOST=0.0.0.0 bash deploy/serve_with_restart.sh
#
# Optional per-deploy env file (arecibo-side, NEVER committed):
#   ~/.token_monitoring/env
#
# TLS: uses the shared InCommon cert already deployed on arecibo for
# arecibo.xray.aps.anl.gov (same file JMD / Califone / XEOL use).
# Override via TM_SSL_KEYFILE / TM_SSL_CERTFILE.
#
# Uvicorn handles SIGTERM cleanly (lifespan shutdown → DB close), so
# `screen -X -S token-monitoring quit` gives a clean exit.

set -o pipefail

PORT=${PORT:-9004}
HOST=${HOST:-0.0.0.0}
LOG_LEVEL=${LOG_LEVEL:-info}

# PAM sign-in for the dashboard on by default in production.
export TM_PAM_ENABLED=${TM_PAM_ENABLED:-1}

# Shared InCommon cert on arecibo (same one JMD + Califone + XEOL use).
TM_SSL_KEYFILE=${TM_SSL_KEYFILE:-$HOME/.jmd/tls/key.pem}
TM_SSL_CERTFILE=${TM_SSL_CERTFILE:-$HOME/.jmd/tls/cert.pem}
export TM_SSL_KEYFILE TM_SSL_CERTFILE

for envfile in "$HOME/.token_monitoring/env" ".env"; do
    if [ -f "$envfile" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$envfile"
        set +a
    fi
done

cd "$(dirname "$0")/.." || { echo "Cannot cd to repo root"; exit 1; }

if [ ! -x .venv/bin/token-monitoring ]; then
    echo "expected .venv/bin/token-monitoring — run: python -m venv .venv && .venv/bin/pip install -e ."
    exit 1
fi

if [ ! -r "$TM_SSL_KEYFILE" ] || [ ! -r "$TM_SSL_CERTFILE" ]; then
    echo "TLS cert missing or unreadable: keyfile=$TM_SSL_KEYFILE certfile=$TM_SSL_CERTFILE"
    echo "Set TM_SSL_KEYFILE / TM_SSL_CERTFILE, or symlink into ~/.jmd/tls/."
    exit 1
fi

while true; do
    printf '%s  Starting token-monitoring on https://%s:%s (pam=%s)\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$HOST" "$PORT" "${TM_PAM_ENABLED}"
    .venv/bin/token-monitoring serve \
        --host "$HOST" --port "$PORT" --log-level "$LOG_LEVEL" \
        --ssl-keyfile "$TM_SSL_KEYFILE" --ssl-certfile "$TM_SSL_CERTFILE"
    printf '%s  Server exited (status %s). Restarting in 5 s...\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$?"
    sleep 5
done
