#!/usr/bin/env bash
set -e

# ============================================
# OpenGUI Client — build and install script
# ============================================

cd "$(dirname "$0")"

FORCE_REBUILD=0
for arg in "$@"; do
  case "$arg" in
    --rebuild)
      FORCE_REBUILD=1
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  ./start.sh           Configure adb, install/launch the existing debug APK
  ./start.sh --rebuild Rebuild the debug APK before installing

If Android reports INSTALL_FAILED_UPDATE_INCOMPATIBLE, the script keeps the
already-installed OpenGUI app and launches it. To replace it completely, run:
  adb uninstall com.coremate.opengui
  ./start.sh --rebuild
EOF
      exit 0
      ;;
    *)
      echo "[✗] Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

setup_java() {
  local candidates=()

  if [ -n "${JAVA_HOME:-}" ]; then
    candidates+=("$JAVA_HOME")
  fi

  if command -v /usr/libexec/java_home >/dev/null 2>&1; then
    local mac_java_home
    mac_java_home="$(/usr/libexec/java_home -v 17 2>/dev/null || true)"
    if [ -n "$mac_java_home" ]; then
      candidates+=("$mac_java_home")
    fi
  fi

  candidates+=(
    "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"
    "/usr/local/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"
    "/Applications/Android Studio.app/Contents/jbr/Contents/Home"
    "/Applications/Android Studio.app/Contents/jre/Contents/Home"
  )

  local home
  for home in "${candidates[@]}"; do
    if [ -x "$home/bin/java" ] && "$home/bin/java" -version >/dev/null 2>&1; then
      export JAVA_HOME="$home"
      export PATH="$JAVA_HOME/bin:$PATH"
      info "Using Java: $JAVA_HOME"
      return 0
    fi
  done

  if command -v java >/dev/null 2>&1 && java -version >/dev/null 2>&1; then
    info "Using Java from PATH"
    return 0
  fi

  error "Java 17+ is required. Install it with: brew install openjdk@17"
}

# --------------------------------------------------
# 1. Check prerequisites
# --------------------------------------------------
command -v adb >/dev/null 2>&1 || error "adb is required. Please install Android SDK Platform Tools."
setup_java

# Check device connection
DEVICE_COUNT=$(adb devices | grep -c "device$" || true)
if [ "$DEVICE_COUNT" -eq 0 ]; then
  error "No Android device detected. Connect a phone over USB and enable USB debugging."
fi
info "Detected ${DEVICE_COUNT} device(s)"

# --------------------------------------------------
# 2. adb port forwarding
# --------------------------------------------------
adb reverse tcp:7777 tcp:7777 >/dev/null 2>&1
info "Port forwarding configured (tcp:7777)"

# --------------------------------------------------
# 3. Build APK
# --------------------------------------------------
APK_PATH="app/build/outputs/apk/debug/app-debug.apk"
PACKAGE="com.coremate.opengui"
if [ "$FORCE_REBUILD" -eq 1 ] || [ ! -f "$APK_PATH" ]; then
  warn "Building APK ..."
  ./gradlew assembleDebug -q
else
  info "Using existing APK: $APK_PATH"
fi

if [ ! -f "$APK_PATH" ]; then
  error "APK build failed. $APK_PATH was not found."
fi
info "APK build completed"

# --------------------------------------------------
# 4. Install on device
# --------------------------------------------------
warn "Installing on device ..."
set +e
INSTALL_OUTPUT="$(adb install -r "$APK_PATH" 2>&1)"
INSTALL_CODE=$?
set -e

if [ "$INSTALL_CODE" -eq 0 ]; then
  info "Install completed"
elif echo "$INSTALL_OUTPUT" | grep -q "INSTALL_FAILED_UPDATE_INCOMPATIBLE"; then
  warn "Installed OpenGUI uses a different signature; keeping the existing app."
  if ! adb shell pm path "$PACKAGE" >/dev/null 2>&1; then
    echo "$INSTALL_OUTPUT"
    error "Install failed and no existing $PACKAGE app was found."
  fi
else
  echo "$INSTALL_OUTPUT"
  error "Install failed"
fi

# --------------------------------------------------
# 5. Launch app
# --------------------------------------------------
adb shell am start -n "$PACKAGE/.login.SplashActivity" >/dev/null 2>&1
info "App launched"

echo ""
echo "  Make sure the server is running: cd ../server && ./start.sh"
echo ""
