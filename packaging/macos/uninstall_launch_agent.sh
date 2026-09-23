#!/usr/bin/env bash
set -euo pipefail
PLIST_PATH="$HOME/Library/LaunchAgents/com.campex.node.plist"
launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"
echo "CAMPEX Node removido do LaunchAgent. Dados locais não foram apagados."
