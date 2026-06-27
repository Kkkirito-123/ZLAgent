#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT/workspace/logs"
RUN_DIR="$ROOT/workspace/runtime"
OPENGUI_DIR="$ROOT/OpenGUI-main/server"
OPENGUI_CLIENT_DIR="$ROOT/OpenGUI-main/client"
OPENGUI_PID_FILE="$RUN_DIR/opengui-server.pid"
OPENGUI_SUPERVISOR_PID_FILE="$RUN_DIR/opengui-supervisor.pid"
OPENGUI_SUPERVISOR_SCRIPT="$ROOT/scripts/opengui-supervisor.sh"
OPENGUI_LOG="$LOG_DIR/opengui-server.log"
OPENGUI_LAUNCH_LABEL="com.openzlagent.opengui.backend"
OPENGUI_LAUNCH_PLIST="$RUN_DIR/${OPENGUI_LAUNCH_LABEL}.plist"
OPENGUI_SCREEN_SESSION="openzlagent-opengui"

WITH_PHONE=0
for arg in "$@"; do
  case "$arg" in
    --with-phone|--phone|--install-phone)
      WITH_PHONE=1
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  ./start.sh              Start ZLAgent + OpenGUI backend
  ./start.sh --with-phone Start backends, then build/install/launch Android client

Notes:
  - OpenGUI backend runs on http://localhost:7777
  - ZLAgent runs on http://localhost:8020
  - Android phone setup still requires USB debugging, Accessibility, and overlay permissions.
EOF
      exit 0
      ;;
    *)
      echo "[x] Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

info() { printf '[+] %s\n' "$1"; }
warn() { printf '[!] %s\n' "$1"; }
die() { printf '[x] %s\n' "$1" >&2; exit 1; }

mkdir -p "$LOG_DIR" "$RUN_DIR"

command -v docker >/dev/null 2>&1 || die "Docker is required."

ensure_env_file() {
  if [ ! -f "$ROOT/.env" ]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    warn "Created .env from .env.example. Fill OPENAI_API_KEY / OPENAI_MODEL before serious use."
  fi
  ensure_env_default "ZLAGENT_OPENGUI_ENABLED" "true"
  ensure_env_default "ZLAGENT_OPENGUI_BASE_URL" "http://host.docker.internal:7777"
  ensure_env_default "ZLAGENT_OPENGUI_TIMEOUT_SECONDS" "15"
}

ensure_env_default() {
  local key="$1"
  local value="$2"
  if ! grep -qE "^${key}=" "$ROOT/.env"; then
    printf '%s=%s\n' "$key" "$value" >> "$ROOT/.env"
  fi
}

is_opengui_running() {
  curl -fsS "http://localhost:7777/docs" >/dev/null 2>&1
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local seconds="${3:-180}"
  local i
  for i in $(seq 1 "$seconds"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      info "$label is ready"
      return 0
    fi
    sleep 1
  done
  return 1
}

record_opengui_pid() {
  local pid
  pid="$(lsof -tiTCP:7777 -sTCP:LISTEN | head -1 || true)"
  if [ -n "$pid" ]; then
    echo "$pid" > "$OPENGUI_PID_FILE"
  else
    rm -f "$OPENGUI_PID_FILE"
  fi
}

record_opengui_supervisor_pid_from_launchctl() {
  if [ "$(uname -s)" != "Darwin" ] || ! command -v launchctl >/dev/null 2>&1; then
    return 0
  fi
  local uid pid
  uid="$(id -u)"
  pid="$(launchctl print "gui/${uid}/${OPENGUI_LAUNCH_LABEL}" 2>/dev/null | awk '/pid =/ { print $3; exit }' || true)"
  if [ -n "$pid" ]; then
    echo "$pid" > "$OPENGUI_SUPERVISOR_PID_FILE"
  fi
}

record_opengui_supervisor_pid_from_screen() {
  if ! command -v screen >/dev/null 2>&1; then
    return 0
  fi
  local pid
  pid="$(screen -ls 2>/dev/null | awk -v session=".${OPENGUI_SCREEN_SESSION}" '$1 ~ session { split($1, parts, "."); print parts[1]; exit }' || true)"
  if [ -n "$pid" ]; then
    echo "$pid" > "$OPENGUI_SUPERVISOR_PID_FILE"
  fi
}

find_node_bin() {
  local codex_node="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
  if [ -x "$codex_node" ]; then
    printf '%s\n' "$codex_node"
    return 0
  fi
  command -v node
}

can_use_launch_agent() {
  [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1
}

can_use_screen() {
  command -v screen >/dev/null 2>&1
}

write_opengui_launch_agent() {
  local node_bin="$1"
  local node_dir
  node_dir="$(dirname "$node_bin")"
  cat > "$OPENGUI_LAUNCH_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${OPENGUI_LAUNCH_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${OPENGUI_SUPERVISOR_SCRIPT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${OPENGUI_DIR}/apps/backend</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OPENGUI_BACKEND_DIR</key>
    <string>${OPENGUI_DIR}/apps/backend</string>
    <key>OPENGUI_NODE_BIN</key>
    <string>${node_bin}</string>
    <key>NODE_ENV</key>
    <string>production</string>
    <key>PORT</key>
    <string>7777</string>
    <key>PATH</key>
    <string>${node_dir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${OPENGUI_LOG}</string>
  <key>StandardErrorPath</key>
  <string>${OPENGUI_LOG}</string>
</dict>
</plist>
EOF
  plutil -lint "$OPENGUI_LAUNCH_PLIST" >/dev/null
}

start_opengui_supervisor() {
  local node_bin="$1"
  OPENGUI_BACKEND_DIR="$OPENGUI_DIR/apps/backend" \
  OPENGUI_NODE_BIN="$node_bin" \
    nohup /bin/bash "$OPENGUI_SUPERVISOR_SCRIPT" >>"$OPENGUI_LOG" 2>&1 < /dev/null &
  echo "$!" > "$OPENGUI_SUPERVISOR_PID_FILE"
}

start_opengui_screen_session() {
  local node_bin="$1"
  screen -S "$OPENGUI_SCREEN_SESSION" -X quit >/dev/null 2>&1 || true
  OPENGUI_BACKEND_DIR="$OPENGUI_DIR/apps/backend" \
  OPENGUI_NODE_BIN="$node_bin" \
  OPENGUI_LOG_FILE="$OPENGUI_LOG" \
    screen -dmS "$OPENGUI_SCREEN_SESSION" /bin/bash "$OPENGUI_SUPERVISOR_SCRIPT"
  record_opengui_supervisor_pid_from_screen
}

start_opengui_launch_agent() {
  local node_bin="$1"
  local uid
  uid="$(id -u)"
  write_opengui_launch_agent "$node_bin"
  launchctl bootout "gui/${uid}/${OPENGUI_LAUNCH_LABEL}" >/dev/null 2>&1 || true
  launchctl enable "gui/${uid}/${OPENGUI_LAUNCH_LABEL}" >/dev/null 2>&1 || true
  if launchctl bootstrap "gui/${uid}" "$OPENGUI_LAUNCH_PLIST" >/dev/null 2>&1; then
    launchctl kickstart -k "gui/${uid}/${OPENGUI_LAUNCH_LABEL}" >/dev/null 2>&1 || true
    record_opengui_supervisor_pid_from_launchctl
    return 0
  fi
  warn "macOS LaunchAgent start failed; falling back to direct background supervisor"
  start_opengui_supervisor "$node_bin"
}

start_opengui_backend() {
  [ -d "$OPENGUI_DIR" ] || die "OpenGUI server directory missing: $OPENGUI_DIR"
  if is_opengui_running; then
    info "OpenGUI backend is already running"
    record_opengui_pid
    record_opengui_supervisor_pid_from_screen
    record_opengui_supervisor_pid_from_launchctl
    return 0
  fi
  info "Preparing OpenGUI backend"
  (
    cd "$OPENGUI_DIR"
    OPENGUI_PREPARE_ONLY=1 ./start.sh
  ) >"$OPENGUI_LOG" 2>&1

  info "Starting OpenGUI backend in background"
  local node_bin
  node_bin="$(find_node_bin)" || die "Node.js 22+ is required. Please install it first."
  if can_use_screen; then
    start_opengui_screen_session "$node_bin"
  elif can_use_launch_agent; then
    start_opengui_launch_agent "$node_bin"
  else
    start_opengui_supervisor "$node_bin"
  fi
  if ! wait_for_url "http://localhost:7777/docs" "OpenGUI backend" 120; then
    warn "OpenGUI backend did not become ready yet. Log: $OPENGUI_LOG"
  fi
  record_opengui_pid
  return 0
}

start_zlagent() {
  info "Starting ZLAgent with Docker Compose"
  (
    cd "$ROOT"
    docker compose up -d --build zlagent
  )
  if wait_for_url "http://localhost:8020/api/health" "ZLAgent" 120; then
    return 0
  fi
  warn "ZLAgent health endpoint not ready yet. Check: docker compose logs -f zlagent"
}

start_phone_client() {
  [ -d "$OPENGUI_CLIENT_DIR" ] || die "OpenGUI client directory missing: $OPENGUI_CLIENT_DIR"
  info "Installing/launching OpenGUI Android client"
  (
    cd "$OPENGUI_CLIENT_DIR"
    exec ./start.sh
  )
}

ensure_env_file
start_opengui_backend
start_zlagent

if [ "$WITH_PHONE" -eq 1 ]; then
  start_phone_client
else
  info "Phone client not installed/launched. Use ./start.sh --with-phone when a USB-debuggable phone is connected."
fi

cat <<EOF

OpenZLAgent is starting.

URLs:
  ZLAgent:        http://localhost:8020
  OpenGUI:        http://localhost:7777

Useful commands:
  ./status.sh
  docker compose logs -f zlagent
  tail -f "$OPENGUI_LOG"
  docker compose run --rm weixin-login
  curl http://localhost:7777/api/remote-control/devices

EOF
