#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "O build macOS precisa ser executado em um Mac."
  exit 1
fi

PYTHON_BIN="${CAMPEX_NODE_PYTHON:-python3}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SPEC_PATH="$PROJECT_DIR/packaging/windows/CAMPEX-Desktop.spec"
DIST_DIR="$PROJECT_DIR/dist"
PACKAGE_DIR="$DIST_DIR/CampexNode"
EXECUTABLE_PATH="$PACKAGE_DIR/CampexNode"
ARCHIVE_PATH="$DIST_DIR/CampexNode-macos-$(uname -m).tar.gz"

cd "$PROJECT_DIR"

"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r campex_node/requirements.txt

rm -rf "$PACKAGE_DIR" "$PROJECT_DIR/build/CAMPEX-Desktop" "$ARCHIVE_PATH"

"$PYTHON_BIN" -m PyInstaller --noconfirm --clean "$SPEC_PATH"

if [ ! -x "$EXECUTABLE_PATH" ]; then
  echo "Build finalizado, mas o executavel nao foi encontrado em $EXECUTABLE_PATH"
  exit 1
fi

mkdir -p "$PACKAGE_DIR/packaging/macos"
cp "$PROJECT_DIR/packaging/macos/install_launch_agent.sh" "$PACKAGE_DIR/packaging/macos/"
cp "$PROJECT_DIR/packaging/macos/status_launch_agent.sh" "$PACKAGE_DIR/packaging/macos/"
cp "$PROJECT_DIR/packaging/macos/uninstall_launch_agent.sh" "$PACKAGE_DIR/packaging/macos/"

cat > "$PACKAGE_DIR/LEIA-ME-macOS.txt" <<'README'
CAMPEX Node para macOS

Como usar:
1. Extraia este pacote em uma pasta local.
2. Execute ./CampexNode para abrir o painel local, ou instale como servico com:
   ./packaging/macos/install_launch_agent.sh
3. O painel local fica em http://127.0.0.1:8787.

Dados, logs e credenciais locais ficam no diretorio configurado por
CAMPEX_NODE_DATA_DIR ou, por padrao, em storage/campex_node quando instalado
pelo repositorio.

Se o macOS bloquear a primeira execucao por seguranca, abra Ajustes do Sistema
> Privacidade e Seguranca e permita a execucao do CAMPEX Node.
README

tar -czf "$ARCHIVE_PATH" -C "$DIST_DIR" CampexNode

echo "CAMPEX Node criado em: $EXECUTABLE_PATH"
echo "Pacote criado em: $ARCHIVE_PATH"
