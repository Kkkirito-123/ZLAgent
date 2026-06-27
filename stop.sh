#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$ROOT/workspace/runtime"
OPENGUI_PID_FILE="$RUN_DIR/opengui-server.pid"
OPENGUI_SUPERVISOR_PID_FILE="$RUN_DIR/opengui-supervisor.pid"
OPENGUI_LAUNCH_PLIST="$RUN_DIR/com.openzlagent.opengui.backend.plist"
OPENGUI_LAUNCH_LABELS=("com.openzlagent.opengui.backend" "com.opengui.backend")
OPENGUI_SCREEN_SESSION="openzlagent-opengui"

info() { printf '[+] %s\n' "$1"; }
warn() { printf '[!] %s\n' "$1"; }

stop_opengui_backend() {
  if command -v screen >/dev/null 2>&1; then
    if screen -ls 2>/dev/null | grep -q "[.]${OPENGUI_SCREEN_SESSION}[[:space:]]"; then
      info "Stopping OpenGUI screen session ${OPENGUI_SCREEN_SESSION}"
      screen -S "$OPENGUI_SCREEN_SESSION" -X quit >/dev/null 2>&1 || true
      sleep 2
    fi
  fi

  if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
    local uid
    uid="$(id -u)"
    local label
    for label in "${OPENGUI_LAUNCH_LABELS[@]}"; do
      if launchctl print "gui/${uid}/${label}" >/dev/null 2>&1; then
        info "Stopping OpenGUI LaunchAgent ${label}"
        launchctl bootout "gui/${uid}/${label}" >/dev/null 2>&1 || true
      fi
    done
  fi

  if [ -f "$OPENGUI_SUPERVISOR_PID_FILE" ]; then
    local supervisor_pid
    supervisor_pid="$(cat "$OPENGUI_SUPERVISOR_PID_FILE" 2>/dev/null || true)"
    if [ -n "$supervisor_pid" ] && kill -0 "$supervisor_pid" >/dev/null 2>&1; then
      info "Stopping OpenGUI supervisor process $supervisor_pid"
      pkill -TERM -P "$supervisor_pid" >/dev/null 2>&1 || true
      kill "$supervisor_pid" >/dev/null 2>&1 || true
      sleep 2
      pkill -KILL -P "$supervisor_pid" >/dev/null 2>&1 || true
      kill -9 "$supervisor_pid" >/dev/null 2>&1 || true
    fi
    rm -f "$OPENGUI_SUPERVISOR_PID_FILE"
  fi

  if [ -f "$OPENGUI_PID_FILE" ]; then
    local pid
    pid="$(cat "$OPENGUI_PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
      info "Stopping OpenGUI backend process $pid"
      kill "$pid" >/dev/null 2>&1 || true
      sleep 2
      if kill -0 "$pid" >/dev/null 2>&1; then
        warn "OpenGUI backend still running; forcing stop"
        kill -9 "$pid" >/dev/null 2>&1 || true
      fi
    fi
    rm -f "$OPENGUI_PID_FILE"
  fi

  local listener_pid
  for listener_pid in $(lsof -tiTCP:7777 -sTCP:LISTEN 2>/dev/null || true); do
    local cwd
    cwd="$(lsof -a -p "$listener_pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
    case "$cwd" in
      "$ROOT"/OpenGUI-main/*)
        info "Stopping lingering OpenGUI listener $listener_pid"
        kill "$listener_pid" >/dev/null 2>&1 || true
        ;;
    esac
  done
  rm -f "$OPENGUI_LAUNCH_PLIST"
}

info "Stopping ZLAgent Docker Compose services"
(
  cd "$ROOT"
  docker compose down
)

stop_opengui_backend

cat <<'EOF'

Stopped OpenZLAgent application services.

OpenGUI database containers may still exist if they were started by OpenGUI.
To stop those too:
  docker rm -f opengui-postgres opengui-redis

Persistent data volumes are not removed.

EOF
