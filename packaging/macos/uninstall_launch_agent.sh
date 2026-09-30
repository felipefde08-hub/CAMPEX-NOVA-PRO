#!/usr/bin/env bash
set -euo pipefail

LABEL="${CAMPEX_NODE_LABEL:-com.campex.node}"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "$(uname -s)" = "Darwin" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
  launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
fi

rm -f "$PLIST_PATH"
echo "CAMPEX Node removido do LaunchAgent. Dados locais nao foram apagados."
