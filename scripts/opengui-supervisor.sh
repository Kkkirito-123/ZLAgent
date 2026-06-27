#!/usr/bin/env bash
set -uo pipefail

: "${OPENGUI_BACKEND_DIR:?OPENGUI_BACKEND_DIR is required}"
: "${OPENGUI_NODE_BIN:?OPENGUI_NODE_BIN is required}"

if [ -n "${OPENGUI_LOG_FILE:-}" ]; then
  mkdir -p "$(dirname "$OPENGUI_LOG_FILE")"
  exec >>"$OPENGUI_LOG_FILE" 2>&1
fi

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

child_pid=""

terminate() {
  echo "[$(timestamp)] OpenGUI supervisor stopping"
  if [ -n "$child_pid" ] && kill -0 "$child_pid" >/dev/null 2>&1; then
    kill "$child_pid" >/dev/null 2>&1 || true
    wait "$child_pid" >/dev/null 2>&1 || true
  fi
  exit 0
}

trap terminate INT TERM

export NODE_ENV="${NODE_ENV:-production}"
export PORT="${PORT:-7777}"
export PATH="$(dirname "$OPENGUI_NODE_BIN"):$PATH"

cd "$OPENGUI_BACKEND_DIR" || exit 1

while true; do
  echo "[$(timestamp)] Starting OpenGUI backend from $OPENGUI_BACKEND_DIR"
  "$OPENGUI_NODE_BIN" dist/main.js &
  child_pid="$!"
  wait "$child_pid"
  exit_code="$?"
  child_pid=""
  echo "[$(timestamp)] OpenGUI backend exited with code $exit_code; restarting in 2 seconds"
  sleep 2 &
  sleep_pid="$!"
  wait "$sleep_pid" >/dev/null 2>&1 || true
done
