#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$ROOT/workspace/runtime"
OPENGUI_PID_FILE="$RUN_DIR/opengui-server.pid"
OPENGUI_SUPERVISOR_PID_FILE="$RUN_DIR/opengui-supervisor.pid"
OPENGUI_LAUNCH_LABEL="com.openzlagent.opengui.backend"
OPENGUI_SCREEN_SESSION="openzlagent-opengui"

check_url() {
  local label="$1"
  local url="$2"
  if curl -fsS "$url" >/dev/null 2>&1; then
    printf '[ok]   %s %s\n' "$label" "$url"
  else
    printf '[miss] %s %s\n' "$label" "$url"
  fi
}

printf 'OpenZLAgent status\n'
printf '\nServices:\n'
check_url "ZLAgent API" "http://localhost:8020/api/health"
check_url "OpenGUI API" "http://localhost:7777/docs"

printf '\nOpenGUI supervisor PID:\n'
launch_pid=""
screen_pid=""
if command -v screen >/dev/null 2>&1; then
  screen_pid="$(screen -ls 2>/dev/null | awk -v session=".${OPENGUI_SCREEN_SESSION}" '$1 ~ session { split($1, parts, "."); print parts[1]; exit }' || true)"
fi
if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
  launch_pid="$(launchctl print "gui/$(id -u)/${OPENGUI_LAUNCH_LABEL}" 2>/dev/null | awk '/pid =/ { print $3; exit }' || true)"
fi
if [ -n "$screen_pid" ]; then
  printf '[ok]   screen pid %s (%s)\n' "$screen_pid" "$OPENGUI_SCREEN_SESSION"
elif [ -n "$launch_pid" ]; then
  printf '[ok]   launchctl pid %s\n' "$launch_pid"
elif [ -f "$OPENGUI_SUPERVISOR_PID_FILE" ]; then
  pid="$(cat "$OPENGUI_SUPERVISOR_PID_FILE" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
    printf '[ok]   pid %s\n' "$pid"
  else
    printf '[miss] stale pid file: %s\n' "$pid"
  fi
else
  printf '[miss] no pid file\n'
fi

printf '\nOpenGUI listener PID:\n'
if [ -f "$OPENGUI_PID_FILE" ]; then
  pid="$(cat "$OPENGUI_PID_FILE" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
    printf '[ok]   pid %s\n' "$pid"
  else
    printf '[miss] stale pid file: %s\n' "$pid"
  fi
else
  printf '[miss] no pid file\n'
fi

printf '\nDocker Compose:\n'
(
  cd "$ROOT"
  docker compose ps
)

printf '\nOpenGUI devices:\n'
if curl -fsS "http://localhost:7777/api/remote-control/devices" 2>/dev/null; then
  printf '\n'
else
  printf '[miss] OpenGUI device endpoint unavailable\n'
fi
