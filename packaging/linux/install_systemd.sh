#!/usr/bin/env bash
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Execute com sudo para instalar o serviço systemd."
  exit 1
fi

PROJECT_DIR="${CAMPEX_PROJECT_DIR:-/opt/campex}"
SERVICE_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/campex-node.service"
SERVICE_DST="/etc/systemd/system/campex-node.service"

if [ ! -d "$PROJECT_DIR" ]; then
  echo "Diretório do projeto não encontrado: $PROJECT_DIR"
  echo "Defina CAMPEX_PROJECT_DIR=/caminho/do/projeto se necessário."
  exit 1
fi

if ! id campex >/dev/null 2>&1; then
  useradd --system --home /var/lib/campex-node --shell /usr/sbin/nologin campex
fi

mkdir -p /var/lib/campex-node
chown -R campex:campex /var/lib/campex-node
cp "$SERVICE_SRC" "$SERVICE_DST"
sed -i "s|/opt/campex|$PROJECT_DIR|g" "$SERVICE_DST"
systemctl daemon-reload
systemctl enable campex-node
systemctl restart campex-node
systemctl status campex-node --no-pager
