#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Este instalador e somente para macOS."
  exit 1
fi

PROJECT_DIR="${CAMPEX_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
HOST="${CAMPEX_NODE_HOST:-127.0.0.1}"
PORT="${CAMPEX_NODE_PORT:-8787}"
LABEL="${CAMPEX_NODE_LABEL:-com.campex.node}"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
DATA_DIR="${CAMPEX_NODE_DATA_DIR:-$PROJECT_DIR/storage/campex_node}"
LOG_DIR="$DATA_DIR/logs"
PACKAGE_EXECUTABLE="$PROJECT_DIR/CampexNode"
DEFAULT_EXECUTABLE="$PROJECT_DIR/dist/CampexNode/CampexNode"
EXECUTABLE="${CAMPEX_NODE_EXECUTABLE:-}"
PYTHON_BIN="${CAMPEX_NODE_PYTHON:-$PROJECT_DIR/.venv/bin/python}"
REQUIREMENTS_PATH="$PROJECT_DIR/campex_node/requirements.txt"

xml_escape() {
  printf '%s' "$1" | sed \
    -e 's/&/\&amp;/g' \
    -e 's/</\&lt;/g' \
    -e 's/>/\&gt;/g'
}

ensure_python_runtime() {
  if [ -x "$PYTHON_BIN" ]; then
    return
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 nao encontrado. Instale pelo Homebrew ou pelo instalador oficial do Python."
    echo "Depois rode novamente: ./packaging/macos/install_launch_agent.sh"
    exit 1
  fi

  echo "Criando ambiente Python em $PROJECT_DIR/.venv"
  python3 -m venv "$PROJECT_DIR/.venv"
}

install_python_dependencies() {
  if [ "${CAMPEX_SKIP_DEP_INSTALL:-0}" = "1" ]; then
    return
  fi

  "$PYTHON_BIN" -m pip install --upgrade pip
  "$PYTHON_BIN" -m pip install -r "$REQUIREMENTS_PATH"
}

if [ -z "$EXECUTABLE" ] && [ -x "$PACKAGE_EXECUTABLE" ]; then
  EXECUTABLE="$PACKAGE_EXECUTABLE"
fi

if [ -z "$EXECUTABLE" ] && [ -x "$DEFAULT_EXECUTABLE" ]; then
  EXECUTABLE="$DEFAULT_EXECUTABLE"
fi

if [ -n "$EXECUTABLE" ]; then
  if [ ! -x "$EXECUTABLE" ]; then
    echo "Executavel nao encontrado ou sem permissao de execucao: $EXECUTABLE"
    exit 1
  fi
  PROGRAM_ARGUMENTS=$(cat <<ARGS
    <string>$(xml_escape "$EXECUTABLE")</string>
    <string>--no-browser</string>
    <string>--host</string>
    <string>$(xml_escape "$HOST")</string>
    <string>--port</string>
    <string>$(xml_escape "$PORT")</string>
ARGS
)
else
  ensure_python_runtime
  install_python_dependencies
  PROGRAM_ARGUMENTS=$(cat <<ARGS
    <string>$(xml_escape "$PYTHON_BIN")</string>
    <string>-m</string>
    <string>campex_node.desktop_launcher</string>
    <string>--no-browser</string>
    <string>--host</string>
    <string>$(xml_escape "$HOST")</string>
    <string>--port</string>
    <string>$(xml_escape "$PORT")</string>
ARGS
)
fi

mkdir -p "$PLIST_DIR" "$LOG_DIR"

PROJECT_DIR_XML="$(xml_escape "$PROJECT_DIR")"
DATA_DIR_XML="$(xml_escape "$DATA_DIR")"
LOG_DIR_XML="$(xml_escape "$LOG_DIR")"
LABEL_XML="$(xml_escape "$LABEL")"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL_XML</string>
  <key>ProgramArguments</key>
  <array>
$PROGRAM_ARGUMENTS
  </array>
  <key>WorkingDirectory</key>
  <string>$PROJECT_DIR_XML</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR_XML/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR_XML/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CAMPEX_NODE_DATA_DIR</key>
    <string>$DATA_DIR_XML</string>
  </dict>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || launchctl load "$PLIST_PATH"
launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || launchctl start "$LABEL" || true

echo "CAMPEX Node instalado como LaunchAgent."
echo "Painel local: http://$HOST:$PORT"
echo "Logs: $LOG_DIR"
