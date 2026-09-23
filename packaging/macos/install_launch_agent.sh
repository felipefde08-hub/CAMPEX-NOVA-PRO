#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON_BIN="${CAMPEX_NODE_PYTHON:-$PROJECT_DIR/.venv/bin/python}"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.campex.node.plist"
LOG_DIR="$PROJECT_DIR/storage/campex_node/logs"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Python não encontrado em $PYTHON_BIN"
  echo "Defina CAMPEX_NODE_PYTHON=/caminho/para/python ou crie .venv."
  exit 1
fi

mkdir -p "$PLIST_DIR" "$LOG_DIR"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.campex.node</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>-m</string>
    <string>campex_node.main</string>
    <string>--app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8787</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$PROJECT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CAMPEX_NODE_DATA_DIR</key>
    <string>$PROJECT_DIR/storage/campex_node</string>
  </dict>
</dict>
</plist>
PLIST

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"
launchctl start com.campex.node || true

echo "CAMPEX Node instalado como LaunchAgent."
echo "Painel local: http://127.0.0.1:8787"
echo "Logs: $LOG_DIR"
