#!/bin/sh
# Nexus fetch service container entrypoint.
#
# 1. Start the bgutil PO provider server (loopback :4416) under a
#    supervisor loop: it restarts on crash, and its log is mirrored to
#    the container stdout with a [bgutil] prefix so Render logs show
#    both processes distinctly.
# 2. Wait (bounded: 30s) for the provider's GET /ping, then exec uvicorn
#    as PID 1. If the provider never becomes ready, uvicorn STILL
#    starts: non-YouTube traffic is unaffected, and YouTube requests
#    degrade to the honest needs_login outcome — never fake success.
#    The provider keeps being retried by the supervisor in the
#    background, and the service re-checks it per request (cached).
set -u

PORT="${PORT:-10000}"
POT_PORT=4416
BGUTIL_DIR=/opt/bgutil/server
LOG=/tmp/bgutil-provider.log

echo "[entrypoint] starting bgutil PO provider on 127.0.0.1:${POT_PORT}"
(
  cd "$BGUTIL_DIR" || exit 1
  while true; do
    echo "----- provider start $(date -u +%Y-%m-%dT%H:%M:%SZ) -----" >> "$LOG"
    node build/main.js >> "$LOG" 2>&1
    echo "----- provider exited rc=$? — restarting in 3s -----" >> "$LOG"
    sleep 3
  done
) &
tail -F "$LOG" 2>/dev/null | sed -u 's/^/[bgutil] /' &

i=0
while [ "$i" -lt 30 ]; do
  if curl -fsS --max-time 2 "http://127.0.0.1:${POT_PORT}/ping" >/dev/null 2>&1; then
    echo "[entrypoint] PO provider is up"
    break
  fi
  i=$((i + 1))
  sleep 1
done
if [ "$i" -ge 30 ]; then
  echo "[entrypoint] WARNING: PO provider not ready after 30s — starting DEGRADED (YouTube may return needs_login until the provider is up; other platforms unaffected)"
fi

echo "[entrypoint] starting fetch service on :${PORT}"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "${PORT}"
