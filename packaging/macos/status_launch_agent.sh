#!/usr/bin/env bash
set -euo pipefail

LABEL="${CAMPEX_NODE_LABEL:-com.campex.node}"
HOST="${CAMPEX_NODE_HOST:-127.0.0.1}"
PORT="${CAMPEX_NODE_PORT:-8787}"
PROJECT_DIR="${CAMPEX_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
DATA_DIR="${CAMPEX_NODE_DATA_DIR:-$PROJECT_DIR/storage/campex_node}"
LOG_DIR="$DATA_DIR/logs"

echo "LaunchAgent:"
if [ "$(uname -s)" = "Darwin" ]; then
  launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null || echo "  $LABEL nao esta carregado."
else
  echo "  Este comando de LaunchAgent so funciona no macOS."
fi

echo
echo "Health:"
if command -v curl >/dev/null 2>&1; then
  curl -fsS "http://$HOST:$PORT/api/health" || true
  echo
else
  echo "  curl nao encontrado. Abra http://$HOST:$PORT/api/health no navegador."
fi

echo
echo "Logs:"
echo "  $LOG_DIR/launchd.out.log"
echo "  $LOG_DIR/launchd.err.log"
echo "  $HOME/.local/share/campex/node/logs/campex-node-bootstrap.log"
