#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PACKAGE="com.coremate.opengui"

usage() {
  cat <<'EOF'
Usage:
  ./phone-wifi.sh ip
      Print this Mac's likely LAN URLs for OpenGUI.

  ./phone-wifi.sh pair <pair_ip:pair_port>
      Pair Android 11+ Wireless debugging. Enter the phone's pairing code when prompted.

  ./phone-wifi.sh connect <device_ip:adb_port>
      Connect an already-paired phone over Wi-Fi, configure adb reverse tcp:7777,
      launch OpenGUI, and show online OpenGUI devices.

  ./phone-wifi.sh devices
      Show adb devices and OpenGUI devices.

Phone path:
  Android Settings -> Developer options -> Wireless debugging
  1. Pair device with pairing code: use its IP:port with ./phone-wifi.sh pair
  2. Main Wireless debugging screen: use its IP:port with ./phone-wifi.sh connect

No-ADB LAN mode:
  Open the OpenGUI app on the phone -> Settings -> Server URL
  Set it to http://<Mac-LAN-IP>:7777, save, then restart the app.
EOF
}

die() {
  printf '[x] %s\n' "$1" >&2
  exit 1
}

info() {
  printf '[+] %s\n' "$1"
}

warn() {
  printf '[!] %s\n' "$1" >&2
}

require_adb() {
  command -v adb >/dev/null 2>&1 || die "adb is required. Install with: brew install android-platform-tools"
}

normalise_adb_target() {
  local target="$1"
  if [[ "$target" =~ ^([0-9]{1,3}\.){4}[0-9]+$ ]]; then
    target="${target%.*}:${target##*.}"
    warn "Interpreting target as $target"
  fi
  if [[ ! "$target" =~ ^[^:]+:[0-9]+$ ]]; then
    die "Target must be IP:port, e.g. 192.168.1.75:45029"
  fi
  printf '%s\n' "$target"
}

lan_ips() {
  ifconfig | awk '/inet / && $2 !~ /^127\./ && $2 !~ /^169\.254\./ {print $2}'
}

print_lan_urls() {
  local found=0
  while IFS= read -r ip; do
    found=1
    printf 'http://%s:7777\n' "$ip"
  done < <(lan_ips)
  if [ "$found" -eq 0 ]; then
    die "No LAN IP found. Make sure Wi-Fi or Ethernet is connected."
  fi
}

show_devices() {
  require_adb
  printf '\nADB devices:\n'
  adb devices
  printf '\nOpenGUI devices:\n'
  curl -fsS "http://localhost:7777/api/remote-control/devices" || true
  printf '\n'
}

connect_wifi() {
  local target="$1"
  require_adb
  info "Connecting adb to $target"
  adb connect "$target"
  info "Configuring adb reverse tcp:7777"
  adb reverse tcp:7777 tcp:7777
  info "Launching OpenGUI app"
  adb shell am start -n "$PACKAGE/.login.SplashActivity" >/dev/null 2>&1 || true
  sleep 3
  show_devices
}

cmd="${1:-}"
case "$cmd" in
  ip)
    print_lan_urls
    ;;
  pair)
    require_adb
    target="${2:-}"
    [ -n "$target" ] || die "Missing pair target, e.g. ./phone-wifi.sh pair 192.168.1.23:37145"
    target="$(normalise_adb_target "$target")"
    adb pair "$target"
    ;;
  connect)
    target="${2:-}"
    [ -n "$target" ] || die "Missing connect target, e.g. ./phone-wifi.sh connect 192.168.1.23:42315"
    target="$(normalise_adb_target "$target")"
    connect_wifi "$target"
    ;;
  devices)
    show_devices
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    die "Unknown command: $cmd"
    ;;
esac
